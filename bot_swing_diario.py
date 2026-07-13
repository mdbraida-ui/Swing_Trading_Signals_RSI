# -*- coding: utf-8 -*-
"""
============================================================================
 BOT SWING DIARIO - Orquestador (RSI+Bollinger, IOL, GitHub Actions)
============================================================================

CONFIRMADO: la API de IOL (revisando el Swagger completo: MiCuenta,
Operar, OperatoriaSimplificada, Titulos, Perfil, Notificacion) NO tiene
ningún endpoint de orden condicional / stop-loss / alarma. El Stop Loss
que ofrece IOL es una función de la plataforma web/app, no de la API.
Por lo tanto el bot tiene que vigilar el precio y mandar la venta a
mercado él mismo -- igual que el bot intraday.

Arquitectura (3 rutinas, cada una un job CORTO de GitHub Actions):

  1. rutina_apertura()   -> corre 1 vez, ~10:30
     Evalúa señales nuevas (vela de ayer ya cerrada), compra a mercado,
     calcula SL/TP inicial y los guarda en la hoja "Posiciones Abiertas"
     de Sheets (que pasa a ser la ÚNICA fuente de verdad de los niveles
     de SL/TP -- IOL no los registra en ningún lado).

  2. rutina_monitoreo()  -> corre cada 5 minutos, de ~10:35 a ~16:55
     Para cada posición abierta: consulta el precio actual, lo compara
     contra el SL/TP guardado en Sheets, y si se cruza, vende A MERCADO
     de inmediato. Un job de GitHub Actions dura ~6hs máximo -- por eso
     NO es un solo proceso corriendo toda la rueda, son ~78 jobs cortos
     independientes por día (uno cada 5 min), cada uno de segundos.

  3. rutina_cierre()     -> corre 1 vez, ~17:00
     Con el cierre del día ya定, actualiza el trailing stop (nuevo nivel
     = EMA21 de hoy, solo si es más alto que el anterior) y lo guarda en
     Sheets para que rutina_monitoreo() lo use mañana. También fuerza el
     cierre de posiciones que llegaron a MAX_DIAS_HOLDING.

==============================================================================
 ADAPTAR A TU IOLClient -- RESUELTO en iol_client.py
==============================================================================
La clase real está en iol_client.py (autenticación, saldo, posiciones,
cotización y compra/venta confirmados contra la documentación y ejemplos
reales de la API de IOL). ÚNICO PUNTO PENDIENTE DE VALIDAR: el JSON
exacto de /operar/Comprar y /operar/Vender no se pudo confirmar 100% sin
acceso logueado a la documentación interactiva -- probar primero en el
sandbox de IOL (api-sandbox.invertironline.com) antes de ir a producción.

Interfaz esperada (ya implementada en IOLClient):

  iol_client.consultar_saldo() -> float
      GET /api/v2/estadocuenta -> filtrar cuentas tipo
      'inversion_Argentina_Pesos', campo 'disponible'.

  iol_client.consultar_posiciones() -> dict
      GET /api/v2/portafolio/argentina -> { "GGAL.BA": {"cantidad": 15,
      "precio_promedio": 4500.50}, ... } (adaptar desde 'activos').

  iol_client.obtener_precio(ticker: str) -> float
      GET /api/v2/{mercado}/Titulos/{simbolo}/Cotizacion -> 'ultimoPrecio'

  iol_client.comprar_mercado(ticker: str, cantidad: int) -> dict
      POST /api/v2/operar/Comprar -> {"exito": bool, "precio_ejecutado": float}

  iol_client.vender_mercado(ticker: str, cantidad: int) -> dict
      POST /api/v2/operar/Vender -> {"exito": bool, "precio_ejecutado": float}

Si tu IOLClient ya tiene estos métodos con otros nombres, armá un
adaptador fino en vez de reescribir la lógica de abajo.
==============================================================================
"""

import os
from datetime import date, datetime, timedelta

from swing_pullback_ema21 import (
    descargar_datos_diarios, calcular_indicadores, generar_senales_rsi_bb,
    calcular_resistencia_previa, sizing_fijo, costo_compra, costo_venta,
    RIESGO_POR_OPERACION, RR_MINIMO, MAX_DIAS_HOLDING, TICKERS_SWING,
)
import telegram_notifier as tg
import sheets_dashboard as sheets

# --- Configuración ---
CAPITAL_TOTAL_CUENTA = 100_000.0
TOPE_MAXIMO_POSICION = 100_000.0
COOLDOWN_DIAS = 3
RSI_UMBRAL = 30.0
TICKERS_UNIVERSO = [t for t in TICKERS_SWING if t != "ECOG.BA"]


# ============================================================================
# UTILIDADES DE ESTADO
# ============================================================================
def obtener_tickers_en_cooldown(sheet) -> dict:
    """Reconstruye el cooldown leyendo el historial de stop_loss en la
    hoja 'Operaciones' -- sin esto no hay forma de saber, entre corridas
    stateless, qué tickers están en enfriamiento."""
    if sheet is None:
        return {}
    try:
        ws = sheet.worksheet("Operaciones")
        filas = ws.get_all_records()
    except Exception as e:
        print(f"[cooldown] no se pudo leer Operaciones: {e}")
        return {}

    hoy = date.today()
    cooldown = {}
    for fila in filas:
        if fila.get("Motivo Salida") != "stop_loss":
            continue
        try:
            fecha_salida = datetime.strptime(fila["Fecha Salida"], "%Y-%m-%d").date()
        except Exception:
            continue
        fecha_hasta = fecha_salida + timedelta(days=COOLDOWN_DIAS)
        if fecha_hasta >= hoy:
            ticker = fila["Ticker"]
            if ticker not in cooldown or fecha_hasta > cooldown[ticker]:
                cooldown[ticker] = fecha_hasta
    return cooldown


def leer_niveles_guardados(sheet) -> dict:
    """Lee la hoja 'Posiciones Abiertas': única fuente de verdad de los
    niveles de Stop/Take-Profit vigentes por ticker (IOL no los guarda).
    Devuelve {ticker: {"stop_vigente": float, "take_profit": float,
    "fecha_entrada": str, "precio_entrada": float}}"""
    if sheet is None:
        return {}
    try:
        ws = sheet.worksheet("Posiciones Abiertas")
        registros = ws.get_all_records()
    except Exception as e:
        print(f"[niveles] no se pudo leer Posiciones Abiertas: {e}")
        return {}
    return {
        r["Ticker"]: {
            "stop_original": float(r["Stop Original"]),
            "stop_vigente": float(r["Stop Vigente"]),
            "take_profit": float(r["Take Profit"]),
            "fecha_entrada": r["Fecha Entrada"],
            "precio_entrada": float(r["Precio Entrada"]),
        }
        for r in registros
    }


def calcular_pnl(precio_entrada, precio_salida, cantidad):
    monto_entrada = precio_entrada * cantidad
    monto_salida = precio_salida * cantidad
    costos = costo_compra(monto_entrada) + costo_venta(monto_salida, intradia=False)
    pnl_pesos = (monto_salida - monto_entrada) - costos
    pnl_pct = 100 * pnl_pesos / monto_entrada if monto_entrada else 0
    return pnl_pesos, pnl_pct


# ============================================================================
# RUTINA 1: APERTURA (~10:30, una vez por día)
# ============================================================================
def rutina_apertura(iol_client):
    tg.notificar_bot_conectado(rutina="apertura 10:30")
    sheet = sheets.conectar_sheet()

    try:
        posiciones_actuales = iol_client.consultar_posiciones()
        efectivo = iol_client.consultar_saldo()
    except Exception as e:
        tg.notificar_error("apertura - consulta IOL", str(e))
        tg.notificar_bot_desconectado(rutina="apertura 10:30", resumen="Abortado por error de conexión")
        return

    cooldown = obtener_tickers_en_cooldown(sheet)
    niveles_guardados = leer_niveles_guardados(sheet)
    hoy = date.today()
    aperturas_realizadas = []

    for ticker in TICKERS_UNIVERSO:
        if ticker in posiciones_actuales:
            continue
        if ticker in cooldown and cooldown[ticker] >= hoy:
            continue
        if efectivo <= 0:
            break

        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = calcular_indicadores(df)
            df = generar_senales_rsi_bb(df, rsi_umbral=RSI_UMBRAL)
        except Exception as e:
            print(f"[apertura] {ticker}: error al descargar/calcular ({e})")
            continue

        if len(df) < 2:
            continue

        ayer = df.iloc[-2]
        if not ayer["senal_confirmada"]:
            continue

        precio_referencia = df["Close"].iloc[-1]
        stop_inicial = ayer["Low"] * 0.995
        riesgo_por_accion = precio_referencia - stop_inicial
        if riesgo_por_accion <= 0 or riesgo_por_accion / precio_referencia < 0.005:
            continue

        idx_ayer = len(df) - 2
        resistencia = calcular_resistencia_previa(df, idx_ayer)
        tp_resistencia = resistencia if resistencia > precio_referencia else None
        tp_rr = precio_referencia + RR_MINIMO * riesgo_por_accion
        take_profit = max(tp_resistencia, tp_rr) if tp_resistencia else tp_rr

        acciones = sizing_fijo(efectivo, RIESGO_POR_OPERACION, precio_referencia, stop_inicial)
        acciones = min(acciones, int(efectivo // precio_referencia))
        acciones = min(acciones, int(TOPE_MAXIMO_POSICION // precio_referencia))
        if acciones <= 0:
            continue

        try:
            orden_compra = iol_client.comprar_mercado(ticker, acciones)
        except Exception as e:
            tg.notificar_error(f"compra {ticker}", str(e))
            continue

        if not orden_compra.get("exito"):
            tg.notificar_error(f"compra {ticker}", f"Orden rechazada: {orden_compra}")
            continue

        precio_ejecutado = orden_compra["precio_ejecutado"]
        efectivo -= precio_ejecutado * acciones

        niveles_guardados[ticker] = {
            "stop_original": stop_inicial,
            "stop_vigente": stop_inicial,
            "take_profit": take_profit,
            "fecha_entrada": hoy.strftime("%Y-%m-%d"),
            "precio_entrada": precio_ejecutado,
        }

        tg.notificar_apertura_posicion(
            ticker, hoy.strftime("%d/%m/%Y"), precio_ejecutado, stop_inicial,
            take_profit=take_profit, acciones=acciones,
        )
        aperturas_realizadas.append(ticker)

    # --- Actualizar dashboard con niveles frescos ---
    try:
        posiciones_actualizadas = iol_client.consultar_posiciones()
        filas_dashboard = {}
        for ticker, pos in posiciones_actualizadas.items():
            nivel = niveles_guardados.get(ticker, {})
            filas_dashboard[ticker] = {
                "fecha_entrada": nivel.get("fecha_entrada", ""),
                "precio_entrada": nivel.get("precio_entrada", pos.get("precio_promedio", 0)),
                "acciones": pos.get("cantidad", 0),
                "stop_original": nivel.get("stop_original", nivel.get("stop_vigente", 0)),
                "stop_vigente": nivel.get("stop_vigente", 0),
                "take_profit": nivel.get("take_profit", 0),
                "dias_en_posicion": 0,
            }
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[apertura] no se pudo actualizar dashboard: {e}")

    resumen = f"Aperturas: {len(aperturas_realizadas)} ({', '.join(aperturas_realizadas) or 'ninguna'})"
    tg.notificar_bot_desconectado(rutina="apertura 10:30", resumen=resumen)


# ============================================================================
# RUTINA 2: MONITOREO (cada 5 min, ~10:35 a ~16:55) -- corre y sale rápido
# ============================================================================
def rutina_monitoreo(iol_client):
    """
    NO manda notificación de conectado/desconectado en cada corrida (son
    ~78 por día, sería spam) -- solo notifica cuando efectivamente cierra
    algo. Diseñado para ser rápido: entra, chequea, sale.
    """
    sheet = sheets.conectar_sheet()

    try:
        posiciones = iol_client.consultar_posiciones()
    except Exception as e:
        print(f"[monitoreo] error al consultar posiciones: {e}")
        return

    if not posiciones:
        return  # nada abierto, no hay nada que monitorear

    niveles = leer_niveles_guardados(sheet)
    hoy = date.today()

    for ticker, pos in posiciones.items():
        nivel = niveles.get(ticker)
        if nivel is None:
            # Posición sin nivel guardado (no debería pasar en operación
            # normal) -- avisar en vez de ignorar silenciosamente.
            tg.notificar_error(
                f"monitoreo {ticker}",
                "Hay posición abierta en IOL sin SL/TP registrado en Sheets. Revisar manualmente."
            )
            continue

        try:
            precio_actual = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[monitoreo] {ticker}: error al obtener precio ({e})")
            continue

        motivo = None
        if precio_actual <= nivel["stop_vigente"]:
            # Se distingue stop_loss (nunca se movió) de trailing_stop
            # (ya fue ajustado hacia arriba al menos una vez) comparando
            # contra el stop original guardado -- necesario para que el
            # cooldown de 3 días solo aplique a stop_loss real, no a un
            # trailing que ya iba ganando.
            stop_original = nivel.get("stop_original", nivel["stop_vigente"])
            motivo = "stop_loss" if nivel["stop_vigente"] == stop_original else "trailing_stop"
        elif precio_actual >= nivel["take_profit"]:
            motivo = "take_profit"

        if motivo is None:
            continue

        cantidad = pos["cantidad"]
        try:
            orden_venta = iol_client.vender_mercado(ticker, cantidad)
        except Exception as e:
            tg.notificar_error(f"venta {ticker}", str(e))
            continue

        if not orden_venta.get("exito"):
            tg.notificar_error(f"venta {ticker}", f"Orden rechazada: {orden_venta}")
            continue

        precio_salida = orden_venta["precio_ejecutado"]
        pnl_pesos, pnl_pct = calcular_pnl(nivel["precio_entrada"], precio_salida, cantidad)

        sheets.registrar_operacion_cerrada(
            sheet, nivel["fecha_entrada"], hoy.strftime("%Y-%m-%d"), ticker,
            nivel["precio_entrada"], precio_salida, cantidad, motivo,
            pnl_pesos, pnl_pct, dias_holding=None,
        )
        tg.notificar_cierre_posicion(
            ticker, hoy.strftime("%d/%m/%Y"), precio_salida, motivo, pnl_pesos, pnl_pct,
        )

        del niveles[ticker]

    # Actualizar dashboard con lo que sigue abierto tras esta pasada
    try:
        posiciones_restantes = iol_client.consultar_posiciones()
        filas_dashboard = {}
        for ticker, pos in posiciones_restantes.items():
            nivel = niveles.get(ticker, {})
            filas_dashboard[ticker] = {
                "fecha_entrada": nivel.get("fecha_entrada", ""),
                "precio_entrada": nivel.get("precio_entrada", pos.get("precio_promedio", 0)),
                "acciones": pos.get("cantidad", 0),
                "stop_original": nivel.get("stop_original", nivel.get("stop_vigente", 0)),
                "stop_vigente": nivel.get("stop_vigente", 0),
                "take_profit": nivel.get("take_profit", 0),
                "dias_en_posicion": 0,
            }
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[monitoreo] no se pudo actualizar dashboard: {e}")


# ============================================================================
# RUTINA 3: CIERRE (~17:00, una vez por día) -- trailing stop + forzados
# ============================================================================
def rutina_cierre(iol_client):
    tg.notificar_bot_conectado(rutina="cierre 17:00")
    sheet = sheets.conectar_sheet()
    hoy = date.today()

    try:
        posiciones = iol_client.consultar_posiciones()
    except Exception as e:
        tg.notificar_error("cierre - consulta posiciones", str(e))
        posiciones = {}

    niveles = leer_niveles_guardados(sheet)
    forzados = []

    for ticker, pos in posiciones.items():
        nivel = niveles.get(ticker)
        if nivel is None:
            continue

        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = calcular_indicadores(df)
        except Exception as e:
            print(f"[cierre] {ticker}: error al descargar ({e})")
            continue

        cierre_hoy = df["Close"].iloc[-1]
        ema21_hoy = df["EMA21"].iloc[-1]

        fecha_entrada = datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()
        dias_en_posicion = (hoy - fecha_entrada).days

        # --- Cierre forzado a MAX_DIAS_HOLDING ---
        if dias_en_posicion >= MAX_DIAS_HOLDING:
            try:
                orden_venta = iol_client.vender_mercado(ticker, pos["cantidad"])
            except Exception as e:
                tg.notificar_error(f"cierre forzado {ticker}", str(e))
                continue
            if orden_venta.get("exito"):
                precio_salida = orden_venta["precio_ejecutado"]
                pnl_pesos, pnl_pct = calcular_pnl(nivel["precio_entrada"], precio_salida, pos["cantidad"])
                sheets.registrar_operacion_cerrada(
                    sheet, nivel["fecha_entrada"], hoy.strftime("%Y-%m-%d"), ticker,
                    nivel["precio_entrada"], precio_salida, pos["cantidad"],
                    "cierre_forzado_max_dias", pnl_pesos, pnl_pct, dias_en_posicion,
                )
                tg.notificar_cierre_posicion(
                    ticker, hoy.strftime("%d/%m/%Y"), precio_salida,
                    "cierre_forzado_max_dias", pnl_pesos, pnl_pct,
                )
                forzados.append(ticker)
                del niveles[ticker]
            continue

        # --- Trailing stop: sube el nivel guardado, nunca lo baja ---
        if cierre_hoy > nivel["precio_entrada"] and ema21_hoy > nivel["stop_vigente"]:
            stop_anterior = nivel["stop_vigente"]
            nivel["stop_vigente"] = ema21_hoy
            tg.notificar_trailing_actualizado(ticker, stop_anterior, ema21_hoy)

    # --- Guardar niveles actualizados para que monitoreo() los use mañana ---
    try:
        filas_dashboard = {}
        for ticker, pos in posiciones.items():
            if ticker not in niveles:
                continue
            nivel = niveles[ticker]
            fecha_entrada = datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()
            filas_dashboard[ticker] = {
                "fecha_entrada": nivel["fecha_entrada"],
                "precio_entrada": nivel["precio_entrada"],
                "acciones": pos["cantidad"],
                "stop_original": nivel.get("stop_original", nivel["stop_vigente"]),
                "stop_vigente": nivel["stop_vigente"],
                "take_profit": nivel["take_profit"],
                "dias_en_posicion": (hoy - fecha_entrada).days,
            }
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[cierre] no se pudo actualizar dashboard: {e}")

    resumen = f"Cierres forzados: {len(forzados)} | Posiciones vigentes: {len(niveles)}"
    tg.notificar_bot_desconectado(rutina="cierre 17:00", resumen=resumen)


# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================
if __name__ == "__main__":
    modo = os.environ.get("MODO_RUTINA", "").strip().lower()

    from iol_client import IOLClient
    iol_client = IOLClient(
        usuario=os.environ["IOL_USUARIO"],
        password=os.environ["IOL_PASSWORD"],
    )

    if modo == "apertura":
        rutina_apertura(iol_client)
    elif modo == "monitoreo":
        rutina_monitoreo(iol_client)
    elif modo == "cierre":
        rutina_cierre(iol_client)
    else:
        raise ValueError(f"MODO_RUTINA debe ser 'apertura', 'monitoreo' o 'cierre', recibido: '{modo}'")
