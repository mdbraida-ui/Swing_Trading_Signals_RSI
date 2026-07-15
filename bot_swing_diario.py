# -*- coding: utf-8 -*-
"""
============================================================================
 BOT SWING DIARIO - Orquestador (RSI+Bollinger, IOL, GitHub Actions)
============================================================================

ESTRATEGIA DE SALIDA: modo "progresivo" (validado con t-stat 2.33-2.77
según el corte de robustez, sobre la muestra de 5 años, n=85-94):

  SL inicial: 1% bajo el precio de entrada real (no un % del mínimo de
  la vela de señal).

  Fase A (todavía no superó la EMA21): cada día que cierra por encima
  de la entrada, el stop sube al mínimo de la vela de AYER (nunca baja).
  Cierre forzado a los 15 días de holding si nunca llegó a superar la
  EMA21.

  Fase B (ya superó la EMA21 en algún cierre): el stop pasa a seguir la
  EMA21 (solo sube). El día que recién cruza no se vende contra ese
  nuevo nivel -- como el cambio de nivel ocurre en rutina_cierre
  (después del cierre del mercado), automáticamente solo empieza a regir
  desde el monitoreo del día SIGUIENTE, nunca el mismo día del cruce
  (evita "vender" por un cruce de paso hacia arriba en vez de un
  retroceso genuino). Sin límite de tiempo una vez en Fase B.

  NO usa take-profit fijo -- se descartó, el trailing de Fase B es la
  única forma de salida en ganancia.

CONFIRMADO: la API de IOL no tiene endpoint de orden condicional /
stop-loss / alarma -- el bot vigila el precio y vende a mercado él
mismo (ver iol_client.py).

Arquitectura (3 rutinas, cada una un job CORTO de GitHub Actions):

  1. rutina_apertura()   -> corre 1 vez, ~10:30
     Evalúa señales nuevas (vela de ayer ya cerrada), compra a mercado,
     guarda el SL inicial en Sheets. También registra el RSI14/BB de
     TODO el universo en la pestaña "Indicadores" (no solo lo que
     termina comprando), 1 vez por día.

  2. rutina_monitoreo()  -> corre cada 5 minutos, de ~10:35 a ~16:55
     Vende a mercado apenas el precio toca el stop vigente.

  3. rutina_cierre()     -> corre 1 vez, ~17:00
     Actualiza el trailing (Fase A o B según corresponda) y fuerza el
     cierre a los 15 días si sigue en Fase A.

==============================================================================
 IOLClient -- ver iol_client.py para la interfaz completa
==============================================================================
"""

import os
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def hoy_argentina() -> date:
    """GitHub Actions corre en UTC -- date.today() sin esto tomaría la
    fecha del servidor. En la práctica nuestros horarios (10:30-17:00
    ART) caen lejos de la medianoche, así que no debería causar
    diferencias reales, pero se hace explícito para no depender de esa
    coincidencia."""
    return datetime.now(TZ_ARGENTINA).date()

from swing_pullback_ema21 import (
    descargar_datos_diarios, calcular_indicadores, generar_senales_rsi_bb,
    sizing_fijo, costo_compra, costo_venta,
    RIESGO_POR_OPERACION, TICKERS_SWING,
)
import telegram_notifier as tg
import sheets_dashboard as sheets

# --- Configuración ---
CAPITAL_TOTAL_CUENTA = 100_000.0
TOPE_MAXIMO_POSICION = 100_000.0
COOLDOWN_DIAS = 3
RSI_UMBRAL = 30.0
SL_INICIAL_PCT = 0.01          # 1% bajo el precio de entrada
DIAS_MAXIMO_FASE_A = 15        # cierre forzado si nunca superó la EMA21
TICKERS_UNIVERSO = [t for t in TICKERS_SWING if t != "ECOG.BA"]


# ============================================================================
# UTILIDADES DE ESTADO
# ============================================================================
def obtener_tickers_en_cooldown(sheet) -> dict:
    """Reconstruye el cooldown leyendo el historial de stop_loss REAL
    (no trailing) en la hoja 'Operaciones' -- sin esto no hay forma de
    saber, entre corridas stateless, qué tickers están en enfriamiento."""
    if sheet is None:
        return {}
    try:
        ws = sheet.worksheet("Operaciones")
        filas = ws.get_all_records()
    except Exception as e:
        print(f"[cooldown] no se pudo leer Operaciones: {e}")
        return {}

    hoy = hoy_argentina()
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
    niveles de Stop vigentes por ticker (IOL no los guarda).
    Devuelve {ticker: {"stop_original": float, "stop_vigente": float,
    "supero_ema21": bool, "fecha_entrada": str, "precio_entrada": float}}"""
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
            "supero_ema21": str(r.get("Supero EMA21", "no")).strip().upper() == "SI",
            "fecha_entrada": r["Fecha Entrada"],
            "precio_entrada": float(r["Precio Entrada"]),
        }
        for r in registros
    }


def leer_estadisticas_operaciones(sheet) -> tuple:
    """Devuelve (operaciones_totales, win_rate_pct) leyendo todo el
    historial de la hoja 'Operaciones' -- para alimentar la fila de
    'Resumen' sin tener que llevar un contador aparte."""
    if sheet is None:
        return 0, 0.0
    try:
        ws = sheet.worksheet("Operaciones")
        filas = ws.get_all_records()
    except Exception as e:
        print(f"[resumen] no se pudo leer Operaciones: {e}")
        return 0, 0.0
    if not filas:
        return 0, 0.0
    ganadoras = sum(1 for f in filas if float(f.get("PnL $", 0) or 0) > 0)
    return len(filas), round(100 * ganadoras / len(filas), 1)


def calcular_pnl(precio_entrada, precio_salida, cantidad):
    monto_entrada = precio_entrada * cantidad
    monto_salida = precio_salida * cantidad
    costos = costo_compra(monto_entrada) + costo_venta(monto_salida, intradia=False)
    pnl_pesos = (monto_salida - monto_entrada) - costos
    pnl_pct = 100 * pnl_pesos / monto_entrada if monto_entrada else 0
    return pnl_pesos, pnl_pct


def _armar_fila_dashboard(nivel: dict, pos: dict, dias_en_posicion: int = 0) -> dict:
    """Arma una fila para 'Posiciones Abiertas' a partir de un nivel
    guardado y la posición real de IOL -- evita repetir esto 3 veces."""
    return {
        "fecha_entrada": nivel.get("fecha_entrada", ""),
        "precio_entrada": nivel.get("precio_entrada", pos.get("precio_promedio", 0)),
        "acciones": pos.get("cantidad", 0),
        "stop_original": nivel.get("stop_original", nivel.get("stop_vigente", 0)),
        "stop_vigente": nivel.get("stop_vigente", 0),
        "supero_ema21": nivel.get("supero_ema21", False),
        "dias_en_posicion": dias_en_posicion,
    }


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
    hoy = hoy_argentina()
    aperturas_realizadas = []
    indicadores_snapshot = {}  # TODO el universo, no solo lo que compra
                                # -- alimenta la pestaña "Indicadores"

    for ticker in TICKERS_UNIVERSO:
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
        indicadores_snapshot[ticker] = {
            "close": df["Close"].iloc[-1],
            "rsi14": ayer["RSI14"],
            "bb_lower": ayer["BB_lower"],
            "bb_mid": ayer["BB_mid"],
            "bb_upper": ayer["BB_upper"],
            "senal_confirmada": bool(ayer["senal_confirmada"]),
        }

        if ticker in posiciones_actuales:
            continue
        if ticker in cooldown and cooldown[ticker] >= hoy:
            continue
        if efectivo <= 0:
            continue  # sin plata para comprar, pero se sigue recorriendo
                       # el resto del universo para el snapshot completo

        if not ayer["senal_confirmada"]:
            continue

        precio_referencia = df["Close"].iloc[-1]
        stop_inicial_estimado = precio_referencia * (1 - SL_INICIAL_PCT)

        acciones = sizing_fijo(efectivo, RIESGO_POR_OPERACION, precio_referencia, stop_inicial_estimado)
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

        # SL final calculado sobre el precio REAL ejecutado (puede
        # diferir un poco del precio_referencia usado para sizear).
        stop_inicial = precio_ejecutado * (1 - SL_INICIAL_PCT)

        niveles_guardados[ticker] = {
            "stop_original": stop_inicial,
            "stop_vigente": stop_inicial,
            "supero_ema21": False,
            "fecha_entrada": hoy.strftime("%Y-%m-%d"),
            "precio_entrada": precio_ejecutado,
        }

        tg.notificar_apertura_posicion(
            ticker, hoy.strftime("%d/%m/%Y"), precio_ejecutado, stop_inicial,
            acciones=acciones,
        )
        aperturas_realizadas.append(ticker)

    # --- Actualizar dashboard con niveles frescos ---
    try:
        posiciones_actualizadas = iol_client.consultar_posiciones()
        filas_dashboard = {
            ticker: _armar_fila_dashboard(niveles_guardados.get(ticker, {}), pos)
            for ticker, pos in posiciones_actualizadas.items()
        }
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[apertura] no se pudo actualizar dashboard: {e}")

    # --- Registrar RSI/BB de todo el universo (1 vez por día) ---
    try:
        sheets.actualizar_indicadores(sheet, indicadores_snapshot)
    except Exception as e:
        print(f"[apertura] no se pudo actualizar indicadores: {e}")

    resumen = f"Aperturas: {len(aperturas_realizadas)} ({', '.join(aperturas_realizadas) or 'ninguna'})"
    print(f"[apertura] {resumen}")
    tg.notificar_bot_desconectado(rutina="apertura 10:30", resumen=resumen)


# ============================================================================
# RUTINA 2: MONITOREO (cada 5 min, ~10:35 a ~16:55) -- corre y sale rápido
# ============================================================================
def rutina_monitoreo(iol_client):
    """
    NO manda notificación de conectado/desconectado en cada corrida (son
    ~78 por día, sería spam) -- solo notifica cuando efectivamente cierra
    algo.
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
    hoy = hoy_argentina()

    for ticker, pos in posiciones.items():
        nivel = niveles.get(ticker)
        if nivel is None:
            tg.notificar_error(
                f"monitoreo {ticker}",
                "Hay posición abierta en IOL sin nivel de stop registrado en Sheets. Revisar manualmente."
            )
            continue

        try:
            precio_actual = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[monitoreo] {ticker}: error al obtener precio ({e})")
            continue

        if precio_actual > nivel["stop_vigente"]:
            continue  # no tocó el stop, nada que hacer

        # Distingue el motivo para el registro y el cooldown (que solo
        # aplica a un stop_loss real, nunca a un trailing que ya iba
        # ganando).
        if nivel.get("supero_ema21", False):
            motivo = "trailing_stop_ema21"
        elif nivel["stop_vigente"] != nivel.get("stop_original", nivel["stop_vigente"]):
            motivo = "trailing_stop_dia_anterior"
        else:
            motivo = "stop_loss"

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
        filas_dashboard = {
            ticker: _armar_fila_dashboard(niveles.get(ticker, {}), pos)
            for ticker, pos in posiciones_restantes.items()
        }
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[monitoreo] no se pudo actualizar dashboard: {e}")


# ============================================================================
# RUTINA 3: CIERRE (~17:00, una vez por día) -- trailing de 2 fases + forzados
# ============================================================================
def rutina_cierre(iol_client):
    tg.notificar_bot_conectado(rutina="cierre 17:00")
    sheet = sheets.conectar_sheet()
    hoy = hoy_argentina()

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
        low_hoy = df["Low"].iloc[-1]
        ema21_hoy = df["EMA21"].iloc[-1]

        fecha_entrada = datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()
        dias_en_posicion = (hoy - fecha_entrada).days

        if not nivel.get("supero_ema21", False):
            # --- FASE A ---
            if cierre_hoy > ema21_hoy:
                # Recién hoy cruza la EMA21 -> pasa a Fase B. Como este
                # cambio recién se aplica desde el monitoreo de MAÑANA
                # (rutina_cierre corre después del cierre del mercado),
                # nunca se chequea salida contra este nivel el mismo día
                # del cruce -- a diferencia del backtest, acá no hace
                # falta ningún flag especial para evitarlo, la
                # arquitectura (cierre -> aplica mañana) ya lo garantiza.
                nivel["supero_ema21"] = True
                if ema21_hoy > nivel["stop_vigente"]:
                    stop_anterior = nivel["stop_vigente"]
                    nivel["stop_vigente"] = ema21_hoy
                    tg.notificar_trailing_actualizado(ticker, stop_anterior, ema21_hoy)
            elif cierre_hoy > nivel["precio_entrada"]:
                if low_hoy > nivel["stop_vigente"]:
                    stop_anterior = nivel["stop_vigente"]
                    nivel["stop_vigente"] = low_hoy
                    tg.notificar_trailing_actualizado(ticker, stop_anterior, low_hoy)

            # --- Cierre forzado a los 15 días, SOLO si sigue en Fase A ---
            if not nivel["supero_ema21"] and dias_en_posicion >= DIAS_MAXIMO_FASE_A:
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
                        "cierre_forzado_fase_a", pnl_pesos, pnl_pct, dias_en_posicion,
                    )
                    tg.notificar_cierre_posicion(
                        ticker, hoy.strftime("%d/%m/%Y"), precio_salida,
                        "cierre_forzado_fase_a", pnl_pesos, pnl_pct,
                    )
                    forzados.append(ticker)
                    del niveles[ticker]
                continue

        else:
            # --- FASE B: sin límite de tiempo, solo sigue a la EMA21 ---
            if ema21_hoy <= cierre_hoy and ema21_hoy > nivel["stop_vigente"]:
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
            filas_dashboard[ticker] = _armar_fila_dashboard(
                nivel, pos, dias_en_posicion=(hoy - fecha_entrada).days
            )
        sheets.actualizar_posiciones_abiertas(sheet, filas_dashboard)
    except Exception as e:
        print(f"[cierre] no se pudo actualizar dashboard: {e}")

    # --- Registrar una fila de "Resumen" con el estado de hoy ---
    try:
        efectivo_actual = iol_client.consultar_saldo()
        valor_posiciones_abiertas = sum(
            pos.get("ultimo_precio", 0) * pos.get("cantidad", 0)
            for pos in posiciones.values()
        )
        operaciones_totales, win_rate_pct = leer_estadisticas_operaciones(sheet)
        sheets.actualizar_resumen(
            sheet, capital_inicial=CAPITAL_TOTAL_CUENTA,
            efectivo_disponible=efectivo_actual,
            valor_posiciones_abiertas=valor_posiciones_abiertas,
            operaciones_totales=operaciones_totales,
            win_rate_pct=win_rate_pct,
        )
    except Exception as e:
        print(f"[cierre] no se pudo actualizar resumen: {e}")

    resumen = f"Cierres forzados: {len(forzados)} | Posiciones vigentes: {len(niveles)}"
    print(f"[cierre] {resumen}")
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
