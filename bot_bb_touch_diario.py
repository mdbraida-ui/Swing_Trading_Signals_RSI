# -*- coding: utf-8 -*-
"""
============================================================================
 BOT BB-TOUCH DIARIO -- Orquestador (BB-touch+BBW+EMA50, IOL, GitHub Actions)
============================================================================

Ver bb_touch_ema50_estrategia.py para las reglas completas de la
estrategia. Resumen operativo de las 3 rutinas:

  1. rutina_apertura()          -> ~10:30, una vez por día
     Notifica "bot conectado" y actualiza la pestaña "Indicadores" con
     RSI14/BB/BBW/EMA50 de TODO el universo. No compra ni vende --
     entradas y Fase B se manejan en rutina_cierre (ver docstring del
     módulo de estrategia: la señal se confirma con el CIERRE del día,
     así que no hay forma de comprar "de verdad" a las 10:30 sin
     adelantar información que no existe todavía).

  2. rutina_monitoreo_fase_a()  -> cada 10 min, SOLO tickers en Fase A
     Vende a mercado si el precio en vivo toca el SL 10%. Fase B no
     necesita este chequeo intradía: su única condición de salida es un
     CIERRE por debajo de EMA50, que solo se puede evaluar una vez, al
     final del día (rutina_cierre) -- monitorearla cada 10 min sería
     trabajo de más sin ningún efecto real.

  3. rutina_cierre()            -> ~16:27-16:50, una vez por día
     (a) Fase A que no tocó stop en el día: evalúa transición a Fase B.
     (b) Fase B: evalúa cierre-bajo-EMA50 (con protección de día de
         transición -- el día que recién cruza no se evalúa salida).
     (c) Entradas nuevas: señal BB-touch+BBW confirmada, compra usando
         el precio en vivo de esta ventana como proxy del cierre.
     (d) Actualiza Sheets completo (Operaciones Activas, Indicadores no
         acá -- eso es en apertura, P&L Total 1 vez por día).

Como con el bot RSI/BB anterior: todas las rutinas son "seguras de
reintentar" (posición ya abierta -> se salta; ya cerrada -> no hace
nada), así que un disparador cada 5 min que decide según la hora real no
genera compras/ventas duplicadas aunque GitHub demore un tick puntual.
============================================================================
"""

import os
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")

from bb_touch_ema50_estrategia import (
    leer_planilla_tickers, descargar_datos_diarios, calcular_indicadores,
    generar_senales_bb_touch_bbw, calcular_acciones_por_capital_objetivo,
    calcular_pnl, costo_venta,
    BBW_UMBRAL, SL_INICIAL_PCT, COOLDOWN_DIAS, TOPE_MAXIMO_POSICION,
)
import telegram_notifier as tg
import sheets_dashboard as sheets

RUTA_TICKERS_CSV = "tickers_activos.csv"
CAPITAL_INICIAL_CUENTA = 100_000.0


def hoy_argentina() -> date:
    return datetime.now(TZ_ARGENTINA).date()


# ============================================================================
# UTILIDADES DE ESTADO (reconstruido desde Sheets, sin memoria persistente)
# ============================================================================
def leer_planilla_activa() -> list:
    """Lista de dicts {ticker, tipo, activo, notas}, en orden de
    prioridad para reparto de efectivo."""
    try:
        return leer_planilla_tickers(RUTA_TICKERS_CSV, solo_activos=True)
    except Exception as e:
        tg.notificar_error("lectura tickers_activos.csv", str(e))
        return []


def obtener_tickers_en_cooldown(sheet) -> dict:
    """Reconstruye el cooldown leyendo el histórico TOTAL -- solo cuenta
    un motivo_salida que empiece con 'stop_loss' (Fase A genuina, nunca
    un cierre por EMA50 en Fase B)."""
    if sheet is None:
        return {}
    try:
        ws = sheet.worksheet("Historico Ordenes TOTAL")
        filas = ws.get_all_records()
    except Exception as e:
        print(f"[cooldown] no se pudo leer Historico Ordenes TOTAL: {e}")
        return {}

    hoy = hoy_argentina()
    cooldown = {}
    for fila in filas:
        motivo = str(fila.get("Motivo Salida", ""))
        if not motivo.startswith("stop_loss"):
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
    """Lee 'Operaciones Activas': única fuente de verdad de niveles y
    fase por ticker (IOL no los guarda).
    Devuelve {ticker: {tipo, fecha_entrada, precio_entrada, acciones,
    fase ('A'|'B'), stop_vigente, sl_fase_a, dia_cruce_fase_b}}"""
    if sheet is None:
        return {}
    try:
        ws = sheet.worksheet("Operaciones Activas")
        registros = ws.get_all_records()
    except Exception as e:
        print(f"[niveles] no se pudo leer Operaciones Activas: {e}")
        return {}
    niveles = {}
    for r in registros:
        ticker = r["Ticker"]
        niveles[ticker] = {
            "tipo": r.get("Tipo", ""),
            "fecha_entrada": r["Fecha Entrada"],
            "precio_entrada": float(r["Precio Entrada"]),
            "acciones": int(r["Acciones"]),
            "fase": r.get("Fase", "A"),
            "stop_vigente": float(r["Stop Vigente"]),
            "sl_fase_a": float(r.get("SL Fase A ($)", r["Stop Vigente"])),
            # el día de cruce a Fase B no se persiste en la hoja (no hace
            # falta): si "Fase" ya dice "B" y estamos evaluando en la
            # MISMA corrida de rutina_cierre en que ocurrió la
            # transición, el chequeo de salida se salta por construcción
            # (ver rutina_cierre, sección Fase B) -- no se necesita un
            # flag persistente porque la transición y el chequeo de
            # salida sieempre ocurren en el mismo, único paso diario.
        }
    return niveles


def leer_estadisticas_operaciones(sheet) -> tuple:
    """(operaciones_totales, win_rate_pct, max_drawdown_pct) desde el
    histórico TOTAL -- max_drawdown se aproxima con la curva de P&L Total
    ya registrada, no recalculando equity operación por operación."""
    if sheet is None:
        return 0, 0.0, 0.0
    try:
        ws = sheet.worksheet("Historico Ordenes TOTAL")
        filas = ws.get_all_records()
    except Exception as e:
        print(f"[stats] no se pudo leer Historico Ordenes TOTAL: {e}")
        return 0, 0.0, 0.0
    if not filas:
        return 0, 0.0, 0.0
    ganadoras = sum(1 for f in filas if float(f.get("P&L $", 0) or 0) > 0)
    win_rate = round(100 * ganadoras / len(filas), 1)

    try:
        ws_pnl = sheet.worksheet("P&L Total")
        filas_pnl = ws_pnl.get_all_records()
        capitales = [float(f["Capital Total"]) for f in filas_pnl if f.get("Capital Total")]
        if capitales:
            pico = capitales[0]
            max_dd = 0.0
            for c in capitales:
                pico = max(pico, c)
                max_dd = max(max_dd, 100 * (pico - c) / pico if pico else 0)
        else:
            max_dd = 0.0
    except Exception as e:
        print(f"[stats] no se pudo leer P&L Total para drawdown: {e}")
        max_dd = 0.0

    return len(filas), win_rate, round(max_dd, 2)


def _cerrar_posicion(iol_client, sheet, ticker: str, nivel: dict, motivo: str,
                      precio_salida_fallback: float = None) -> bool:
    """Vende a mercado y registra el cierre en Sheets + Telegram. Devuelve
    True si se cerró con éxito (para que el llamador la saque de
    `niveles`)."""
    try:
        orden_venta = iol_client.vender_mercado(ticker, nivel["acciones"])
    except Exception as e:
        tg.notificar_error(f"venta {ticker}", str(e))
        return False
    if not orden_venta.get("exito"):
        tg.notificar_error(f"venta {ticker}", f"Orden rechazada: {orden_venta}")
        return False

    precio_salida = orden_venta.get("precio_ejecutado") or precio_salida_fallback
    hoy = hoy_argentina()
    pnl_pesos, pnl_pct = calcular_pnl(nivel["precio_entrada"], precio_salida, nivel["acciones"])
    dias_holding = (hoy - datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()).days

    sheets.registrar_operacion_cerrada(
        sheet, ticker, nivel.get("tipo", ""), nivel["fecha_entrada"], nivel["precio_entrada"],
        hoy.strftime("%Y-%m-%d"), precio_salida, nivel["acciones"], nivel.get("sl_fase_a", 0),
        motivo, pnl_pesos, pnl_pct, dias_holding,
    )
    tg.notificar_cierre_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_salida, motivo, pnl_pesos, pnl_pct)
    return True


# ============================================================================
# RUTINA 1: APERTURA (~10:30, una vez por día) -- solo notifica + Indicadores
# ============================================================================
def rutina_apertura(iol_client):
    sheet = sheets.conectar_sheet()

    if sheets.apertura_de_hoy_ya_registrada(sheet):
        print("[apertura] ya se corrió hoy -- se salta (evita repetir mensajes de Telegram)")
        return

    tg.notificar_bot_conectado("apertura")

    tickers_planilla = leer_planilla_activa()
    cooldown = obtener_tickers_en_cooldown(sheet)
    niveles = leer_niveles_guardados(sheet)
    hoy = hoy_argentina()

    indicadores_snapshot = {}
    for fila in tickers_planilla:
        ticker, tipo = fila["ticker"], fila["tipo"]
        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = calcular_indicadores(df)
            df = generar_senales_bb_touch_bbw(df, bbw_umbral=BBW_UMBRAL)
        except Exception as e:
            print(f"[apertura] {ticker}: error al descargar/calcular ({e})")
            continue
        if df.empty:
            continue

        ultimo = df.iloc[-1]
        # "pendiente" a nivel snapshot: tocó banda inferior en algún
        # momento y todavía no confirmó -- se aproxima mirando si el
        # BBW de hoy sigue por debajo del umbral pero el precio ya tocó
        # la banda en las últimas ruedas (informativo, no crítico).
        toco_banda_reciente = bool((df["Low"].tail(20) <= df["BB_lower"].tail(20)).any())
        senal_confirmada_hoy = bool(ultimo["senal_confirmada"])

        indicadores_snapshot[ticker] = {
            "tipo": tipo,
            "precio_actual": ultimo["Close"],
            "rsi14": ultimo["RSI14"],
            "bb_lower": ultimo["BB_lower"],
            "bb_mid": ultimo["BB_mid"],
            "bb_upper": ultimo["BB_upper"],
            "bbw": ultimo["BBW"],
            "ema50": ultimo["EMA50"],
            "senal_pendiente": toco_banda_reciente and not senal_confirmada_hoy,
            "senal_confirmada": senal_confirmada_hoy,
            "en_cooldown": ticker in cooldown and cooldown[ticker] >= hoy,
        }

    try:
        sheets.actualizar_indicadores(sheet, indicadores_snapshot)
    except Exception as e:
        print(f"[apertura] no se pudo actualizar Indicadores: {e}")

    senales_pendientes = sum(1 for v in indicadores_snapshot.values() if v["senal_pendiente"])
    senales_confirmadas = sum(1 for v in indicadores_snapshot.values() if v["senal_confirmada"])
    tickers_en_cooldown = sum(1 for v in indicadores_snapshot.values() if v["en_cooldown"])

    tg.notificar_resumen_apertura(
        cantidad_tickers=len(indicadores_snapshot),
        senales_pendientes=senales_pendientes,
        senales_confirmadas=senales_confirmadas,
        en_cooldown=tickers_en_cooldown,
        posiciones_abiertas=len(niveles),
    )

    print(f"[apertura] Indicadores actualizados para {len(indicadores_snapshot)} tickers. "
          f"Posiciones abiertas: {len(niveles)}.")


# ============================================================================
# RUTINA 2: MONITOREO FASE A (cada 10 min) -- solo SL 10% intradía
# ============================================================================
def rutina_monitoreo_fase_a(iol_client):
    sheet = sheets.conectar_sheet()

    try:
        posiciones_iol = iol_client.consultar_posiciones()
    except Exception as e:
        print(f"[monitoreo] error al consultar posiciones: {e}")
        return
    if not posiciones_iol:
        return

    niveles = leer_niveles_guardados(sheet)
    tickers_fase_a = [t for t, n in niveles.items() if n.get("fase", "A") == "A" and t in posiciones_iol]
    if not tickers_fase_a:
        return  # nada en Fase A -- Fase B no se chequea acá (ver docstring del módulo)

    cambios = False
    for ticker in tickers_fase_a:
        nivel = niveles[ticker]
        try:
            precio_actual = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[monitoreo] {ticker}: error al obtener precio ({e})")
            continue

        if precio_actual > nivel["stop_vigente"]:
            continue  # no tocó el stop

        if _cerrar_posicion(iol_client, sheet, ticker, nivel, motivo="stop_loss_10pct",
                             precio_salida_fallback=precio_actual):
            del niveles[ticker]
            cambios = True

    if cambios:
        _reescribir_operaciones_activas(iol_client, sheet, niveles)


# ============================================================================
# RUTINA 3: CIERRE (~16:27-16:50, una vez por día)
# ============================================================================
def rutina_cierre(iol_client):
    sheet = sheets.conectar_sheet()
    hoy = hoy_argentina()

    try:
        posiciones_iol = iol_client.consultar_posiciones()
    except Exception as e:
        tg.notificar_error("cierre - consulta posiciones", str(e))
        posiciones_iol = {}

    niveles = leer_niveles_guardados(sheet)
    cooldown = obtener_tickers_en_cooldown(sheet)
    tickers_planilla = leer_planilla_activa()

    # ------------------------------------------------------------------
    # (a) + (b): evaluar posiciones abiertas -- transición A->B y salida
    #     por cierre-bajo-EMA50 en Fase B. Se recorre SOLO lo que sigue
    #     abierto en IOL (lo que ya vendió monitoreo_fase_a no está acá).
    # ------------------------------------------------------------------
    for ticker in list(niveles.keys()):
        if ticker not in posiciones_iol:
            continue  # ya no está en cartera (lo cerró monitoreo_fase_a hoy)
        nivel = niveles[ticker]
        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = calcular_indicadores(df)
        except Exception as e:
            print(f"[cierre] {ticker}: error al descargar ({e})")
            continue
        if df.empty:
            continue

        cierre_hoy = df["Close"].iloc[-1]
        ema50_hoy = df["EMA50"].iloc[-1]

        if nivel["fase"] == "A":
            if cierre_hoy > ema50_hoy:
                # Transición a Fase B -- recién ahora, en este mismo paso.
                # No se evalúa salida por EMA50 hoy (protección de día de
                # transición): el chequeo de "cierre < ema50" está en el
                # bloque `else` de abajo, que esta posición todavía no
                # visita en esta corrida porque ya se actualiza acá.
                stop_anterior = nivel["stop_vigente"]
                nivel["fase"] = "B"
                nivel["stop_vigente"] = ema50_hoy
                tg.notificar_trailing_actualizado(ticker, stop_anterior, ema50_hoy)
            # Fase A que no cruza: el SL sigue en sl_fase_a sin cambios
            # (esta estrategia no sube el stop en Fase A como sí hacía
            # el motor RSI/BB -- SL fijo 10% hasta cruzar a Fase B).
        else:
            # Fase B, y NO es el mismo día del cruce (si lo fuera, este
            # ticker habría entrado al `if` de arriba, no acá) -- se
            # evalúa la única condición de salida de Fase B.
            if cierre_hoy < ema50_hoy:
                if _cerrar_posicion(iol_client, sheet, ticker, nivel, motivo="cierre_bajo_ema50",
                                     precio_salida_fallback=cierre_hoy):
                    del niveles[ticker]
                    continue
            else:
                nivel["stop_vigente"] = ema50_hoy  # el nivel de referencia sigue a la EMA50

    # ------------------------------------------------------------------
    # (c) Entradas nuevas -- capital compartido, reparto en orden de
    #     planilla, precio en vivo de esta ventana como proxy de cierre.
    # ------------------------------------------------------------------
    try:
        efectivo = iol_client.consultar_saldo()
    except Exception as e:
        tg.notificar_error("cierre - consulta saldo", str(e))
        efectivo = 0.0

    for fila in tickers_planilla:
        ticker, tipo = fila["ticker"], fila["tipo"]
        if ticker in niveles:
            continue  # ya hay posición abierta
        if ticker in cooldown and cooldown[ticker] >= hoy:
            continue
        if efectivo <= 0:
            break

        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = calcular_indicadores(df)
            df = generar_senales_bb_touch_bbw(df, bbw_umbral=BBW_UMBRAL)
        except Exception as e:
            print(f"[cierre] {ticker}: error al descargar/calcular señal ({e})")
            continue
        if df.empty or not bool(df["senal_confirmada"].iloc[-1]):
            continue

        try:
            precio_entrada_aprox = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[cierre] {ticker}: error al obtener precio en vivo ({e})")
            continue

        acciones = calcular_acciones_por_capital_objetivo(efectivo, precio_entrada_aprox, TOPE_MAXIMO_POSICION)
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

        precio_ejecutado = orden_compra.get("precio_ejecutado") or precio_entrada_aprox
        efectivo -= precio_ejecutado * acciones
        sl_fase_a = precio_ejecutado * (1 - SL_INICIAL_PCT)

        niveles[ticker] = {
            "tipo": tipo,
            "fecha_entrada": hoy.strftime("%Y-%m-%d"),
            "precio_entrada": precio_ejecutado,
            "acciones": acciones,
            "fase": "A",
            "stop_vigente": sl_fase_a,
            "sl_fase_a": sl_fase_a,
        }
        tg.notificar_apertura_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_ejecutado,
                                        sl_fase_a, acciones=acciones)

    # ------------------------------------------------------------------
    # (d) Actualizar Sheets: Operaciones Activas siempre, P&L Total 1
    #     sola vez por día pese a los reintentos de la ventana.
    # ------------------------------------------------------------------
    _reescribir_operaciones_activas(iol_client, sheet, niveles)

    try:
        if not sheets.dashboard_de_hoy_ya_registrado(sheet):
            efectivo_final = iol_client.consultar_saldo()
            posiciones_finales = iol_client.consultar_posiciones()
            valor_posiciones = sum(
                pos.get("ultimo_precio", 0) * pos.get("cantidad", 0)
                for pos in posiciones_finales.values()
            )
            operaciones_totales, win_rate_pct, max_dd_pct = leer_estadisticas_operaciones(sheet)
            sheets.actualizar_dashboard_pnl(
                sheet, capital_inicial=CAPITAL_INICIAL_CUENTA, efectivo=efectivo_final,
                valor_posiciones=valor_posiciones, operaciones_totales=operaciones_totales,
                win_rate_pct=win_rate_pct, max_drawdown_pct=max_dd_pct,
            )
    except Exception as e:
        print(f"[cierre] no se pudo actualizar P&L Total: {e}")

    resumen = f"Posiciones vigentes tras el cierre: {len(niveles)}"
    print(f"[cierre] {resumen}")
    tg.notificar_bot_desconectado("cierre", resumen=resumen)


def _reescribir_operaciones_activas(iol_client, sheet, niveles: dict):
    """Arma el snapshot de Operaciones Activas con precio en vivo para
    cada posición vigente -- se llama al final de monitoreo (si hubo
    cambios) y siempre al final de cierre."""
    try:
        posiciones_iol = iol_client.consultar_posiciones()
    except Exception as e:
        print(f"[sheets] no se pudo consultar posiciones para Operaciones Activas: {e}")
        return

    hoy = hoy_argentina()
    filas = {}
    for ticker, nivel in niveles.items():
        precio_actual = posiciones_iol.get(ticker, {}).get("ultimo_precio", nivel["precio_entrada"])
        fecha_entrada = datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()
        filas[ticker] = {
            "tipo": nivel.get("tipo", ""),
            "fecha_entrada": nivel["fecha_entrada"],
            "precio_entrada": nivel["precio_entrada"],
            "acciones": nivel["acciones"],
            "precio_actual": precio_actual,
            "fase": nivel["fase"],
            "stop_vigente": nivel["stop_vigente"],
            "sl_fase_a": nivel.get("sl_fase_a", 0),
            "dias_en_posicion": (hoy - fecha_entrada).days,
        }
    sheets.actualizar_operaciones_activas(sheet, filas)


# ============================================================================
# PUNTO DE ENTRADA
# ============================================================================
# Mercado 10:30-17:00 ART. Ventanas con margen para absorber demoras de
# cola de GitHub Actions (documentado: hasta 1h45 en horarios "redondos").
VENTANA_APERTURA = (dtime(10, 27), dtime(11, 0))
VENTANA_CIERRE = (dtime(16, 27), dtime(16, 50))


def _debe_correr_monitoreo(hora_actual: dtime) -> bool:
    """El cron dispara cada 5 min (necesario para que apertura/cierre
    tengan reintentos densos ante demoras de GitHub), pero el pedido es
    que monitoreo Fase A corra cada ~10 min, no cada 5. Sin estado
    persistente entre corridas, se aproxima con un throttle liviano: solo
    corre si el minuto cae en la primera mitad de cada bloque de 10 (ej.
    corre en :02/:07 pero no en :12/:17 si el cron es cada 5 min offset).
    No es un cada-10-min exacto, pero evita duplicar el trabajo en cada
    disparo de 5 min sin necesitar guardar estado en ningún lado."""
    return (hora_actual.minute % 10) < 5


if __name__ == "__main__":
    from iol_client import IOLClient
    iol_client = IOLClient(
        usuario=os.environ["IOL_USUARIO"],
        password=os.environ["IOL_PASSWORD"],
    )

    modo_manual = os.environ.get("MODO_RUTINA", "").strip().lower()
    if modo_manual:
        if modo_manual == "apertura":
            rutina_apertura(iol_client)
        elif modo_manual == "monitoreo":
            rutina_monitoreo_fase_a(iol_client)
        elif modo_manual == "cierre":
            rutina_cierre(iol_client)
        else:
            raise ValueError(f"MODO_RUTINA debe ser 'apertura', 'monitoreo' o 'cierre', recibido: '{modo_manual}'")
    else:
        hora_actual = datetime.now(TZ_ARGENTINA).time()

        if VENTANA_APERTURA[0] <= hora_actual <= VENTANA_APERTURA[1]:
            rutina_apertura(iol_client)
        elif VENTANA_CIERRE[0] <= hora_actual <= VENTANA_CIERRE[1]:
            rutina_cierre(iol_client)
        elif _debe_correr_monitoreo(hora_actual):
            rutina_monitoreo_fase_a(iol_client)
        else:
            print(f"[monitoreo] throttle -- se salta esta corrida ({hora_actual.strftime('%H:%M')})")
