# -*- coding: utf-8 -*-
"""
============================================================================
 BOT BB-TOUCH DIARIO -- Orquestador (BB-touch+BBW+EMA50, IOL, GitHub Actions)
============================================================================

Ver bb_touch_ema50_estrategia.py para las reglas completas de la
estrategia. Resumen operativo de las 4 rutinas:

  1. rutina_apertura()          -> ~10:30, una vez por día
     Notifica "bot conectado" y actualiza la pestaña "Indicadores" con
     RSI14/BB/BBW/EMA50 de TODO el universo. No compra ni vende --
     entradas y Fase B se manejan en rutina_cierre (ver docstring del
     módulo de estrategia: la señal se confirma con el CIERRE del día,
     así que no hay forma de comprar "de verdad" a las 10:30 sin
     adelantar información que no existe todavía).

  1B. rutina_refresh()          -> ~13:00 y ~16:00 ART, dos veces por
     día (agregada 29/07/2026, a pedido del usuario). Recalcula
     Indicadores y Operaciones Activas (mark-to-market) con datos
     frescos, SIN comprar ni vender nada -- pensada para que el
     usuario pueda ir controlando la planilla/gráficos durante el día
     y decidir por su cuenta si algo le preocupa, antes de la ventana
     de cierre real. No manda mensaje de Telegram (evita ruido
     duplicado -- el usuario revisa Sheets cuando quiere).

  2. rutina_monitoreo_fase_a()  -> cada 10 min, SOLO tickers en Fase A
     Vende a mercado si el precio en vivo toca el SL 10%. Fase B no
     necesita este chequeo intradía: su única condición de salida es un
     CIERRE por debajo de EMA50, que solo se puede evaluar una vez, al
     final del día (rutina_cierre) -- monitorearla cada 10 min sería
     trabajo de más sin ningún efecto real.

  3. rutina_cierre()            -> ~16:45 ART, una vez por día (movido
     de 16:27 a 16:45 el 29/07/2026, a pedido del usuario -- más cerca
     del cierre real de mercado, 17:00).
     (a) Fase A que no tocó stop en el día: evalúa transición a Fase B.
     (b) Fase B: evalúa cierre-bajo-EMA50 (con protección de día de
         transición -- el día que recién cruza no se evalúa salida).
     (c1) Cola de reintento: señales confirmadas en días anteriores que
         no se pudieron comprar por falta de efectivo -- se reintentan
         ANTES que las señales nuevas del día (tienen prioridad, por
         haber confirmado primero). Se invalidan si pasan más de
         DIAS_MAXIMO_REINTENTO_EJECUCION velas sin ejecutarse, o si el
         precio ya cayó más de SL_INICIAL_PCT desde el precio de
         confirmación original -- lo que pase primero. Persisten entre
         corridas en la pestaña "Señales Pendientes Ejecución" de
         Sheets (única fuente de estado, el bot no guarda nada en
         memoria entre ejecuciones).
     (c2) Entradas nuevas: señal BB-touch+BBW confirmada HOY, compra
         usando el precio en vivo de esta ventana como proxy del
         cierre. Si confirma pero no alcanza el capital, se ENCOLA para
         reintentar en (c1) los próximos días, en vez de perderse.
     (d) Actualiza Sheets completo (Operaciones Activas, cola de
         reintento, Indicadores no acá -- eso es en apertura, P&L Total
         1 vez por día).

Como con el bot RSI/BB anterior: todas las rutinas son "seguras de
reintentar" (posición ya abierta -> se salta; ya cerrada -> no hace
nada), así que un disparador cada 5 min que decide según la hora real no
genera compras/ventas duplicadas aunque GitHub demore un tick puntual.
============================================================================
"""

import os
import pandas as pd
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")

from bb_touch_ema50_estrategia import (
    leer_planilla_tickers, descargar_datos_diarios, calcular_indicadores,
    generar_senales_bb_touch_bbw, calcular_acciones_por_capital_objetivo,
    calcular_pnl, calcular_pnl_con_desglose, costo_venta, forzar_cierre_de_hoy,
    BBW_UMBRAL, SL_INICIAL_PCT, COOLDOWN_DIAS, TOPE_MAXIMO_POSICION,
    DIAS_MAXIMO_REINTENTO_EJECUCION, PCT_MOVIDO_MAXIMO,
)
import telegram_notifier as tg
import sheets_dashboard as sheets

RUTA_TICKERS_CSV = "tickers_activos.csv"
CAPITAL_INICIAL_CUENTA = 100_000.0


def hoy_argentina() -> date:
    return datetime.now(TZ_ARGENTINA).date()


def _normalizar_valor_fecha_sheets(valor):
    """
    *** FIX CRÍTICO -- bug real en producción, 03/08/2026 ***
    `get_all_records()` sin `value_render_option='UNFORMATTED_VALUE'`
    devuelve los números YA FORMATEADOS según la configuración regional
    de la planilla (coma decimal en Argentina) -- el conversor automático
    de gspread no siempre interpreta bien ese formato, y puede devolver
    un número completamente distinto al real (confirmado: un stop de
    $68.872,50 se leyó como $688.725 -- 10x más alto -- disparando una
    venta prematura con solo ~1.9% de caída en vez del 10% real).

    Se cambiaron todas las lecturas de Sheets a UNFORMATTED_VALUE (ver
    los distintos `get_all_records(value_render_option=...)` del
    proyecto) para traer los números crudos, sin pasar por el
    formateo/parseo de locale. Efecto secundario a cubrir: si Sheets
    auto-detectó una celda de texto tipo fecha ("2026-07-31") y la
    convirtió en un valor de fecha real, con UNFORMATTED_VALUE esa
    celda vuelve como NÚMERO SERIAL (días desde el 30/12/1899, con
    parte fraccionaria si tiene hora) en vez de texto -- esta función
    normaliza cualquiera de los dos casos (texto ya en el formato
    esperado, o serial numérico) a un datetime. None si está vacío.
    """
    if valor is None or valor == "":
        return None
    if isinstance(valor, (int, float)):
        return datetime(1899, 12, 30) + timedelta(days=float(valor))
    if isinstance(valor, str):
        for fmt in ("%Y-%m-%d", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
            try:
                return datetime.strptime(valor, fmt)
            except ValueError:
                continue
    return None


def extraer_info_pendiente(df, hoy: date) -> tuple:
    """A partir del df ya procesado por generar_senales_bb_touch_bbw,
    devuelve (fecha_toque_str, dias_pendiente, pct_movido_desde_toque)
    del último renglón -- (None, None, None) si esa fila no está en
    estado pendiente (fecha_toque_vigente vacío). `pct_movido_desde_
    toque` compara el precio ACTUAL (última fila del df, ya forzado
    con el precio en vivo de IOL vía forzar_cierre_de_hoy) contra el
    precio del día del toque -- mismo cálculo que ya usa internamente
    el filtro de movimiento máximo, ahora expuesto para el dashboard."""
    ultimo = df.iloc[-1]
    fecha_toque = ultimo.get("fecha_toque_vigente")
    if fecha_toque is None or pd.isna(fecha_toque):
        return None, None, None
    fecha_toque_date = fecha_toque.date() if hasattr(fecha_toque, "date") else fecha_toque
    dias_pendiente = (hoy - fecha_toque_date).days

    precio_toque = ultimo.get("precio_toque_vigente")
    precio_actual = ultimo.get("Close")
    if precio_toque and not pd.isna(precio_toque) and precio_toque > 0:
        pct_movido = round(100 * (precio_actual - precio_toque) / precio_toque, 1)
    else:
        pct_movido = None

    return fecha_toque_date.strftime("%Y-%m-%d"), dias_pendiente, pct_movido


def extraer_info_vela_verde(df, hoy: date) -> tuple:
    """A partir del df ya procesado por generar_senales_bb_touch_bbw,
    devuelve (esperando_vela_verde: bool, fecha_confirmacion_bbw_str,
    dias_esperando) del último renglón -- (False, None, None) si esa
    fila no está en estado "esperando vela verde" (BBW ya confirmó,
    pasó el filtro de movimiento, pero todavía no apareció el primer
    día con Close>Open y Close>Close_anterior)."""
    ultimo = df.iloc[-1]
    esperando = bool(ultimo.get("esperando_vela_verde", False))
    if not esperando:
        return False, None, None
    fecha_confirmacion = ultimo.get("fecha_confirmacion_bbw_vigente")
    if fecha_confirmacion is None or pd.isna(fecha_confirmacion):
        return True, None, None
    fecha_confirmacion_date = (
        fecha_confirmacion.date() if hasattr(fecha_confirmacion, "date") else fecha_confirmacion
    )
    dias_esperando = (hoy - fecha_confirmacion_date).days
    return True, fecha_confirmacion_date.strftime("%Y-%m-%d"), dias_esperando


def _encolar_senal_no_ejecutada(cola_actualizada: list, tickers_en_cola: set,
                                 dias_transcurridos_por_ticker: dict,
                                 ticker: str, tipo: str, hoy: date, precio_referencia: float):
    """
    Agrega a la cola de reintento una señal que confirmó HOY pero no
    llegó a comprarse -- ya sea por falta de capital, o (caso agregado
    tras el error real de TSLA.BA del 28/07/2026) porque la orden fue
    RECHAZADA por IOL por un motivo técnico (ej. "alteración mínima
    permitida" -- ver iol_client.py, _REDONDEOS_PRECIO). Antes, un
    rechazo de orden hacía perder la señal del día sin ningún registro
    ni reintento -- ahora entra a la misma cola de 3 días que ya existe
    para el caso de falta de capital, dándole a IOL otra chance mañana
    (por ejemplo, si el fix de redondeo de precio se actualiza, o si el
    rechazo fue transitorio).
    """
    if ticker in tickers_en_cola:
        return  # ya está en la cola por otro motivo esta misma corrida
    stop_referencia = precio_referencia * (1 - SL_INICIAL_PCT)
    cola_actualizada.append({
        "ticker": ticker, "tipo": tipo,
        "fecha_confirmacion": hoy.strftime("%Y-%m-%d"),
        "precio_confirmacion": precio_referencia,
        "stop_referencia": stop_referencia,
    })
    tickers_en_cola.add(ticker)
    dias_transcurridos_por_ticker[ticker] = 0


# ============================================================================
# UTILIDADES DE ESTADO (reconstruido desde Sheets, sin memoria persistente)
# ============================================================================
def leer_planilla_activa() -> list:
    """Solo tickers con activo=SI -- usar para decisiones de COMPRA."""
    try:
        return leer_planilla_tickers(RUTA_TICKERS_CSV, solo_activos=True)
    except Exception as e:
        tg.notificar_error("lectura tickers_activos.csv", str(e))
        return []


def leer_planilla_completa() -> list:
    """TODOS los tickers de la planilla, sin importar el flag `activo`
    -- usar para calcular/mostrar Indicadores. Un ticker en NO se sigue
    monitoreando (para tener el dato a mano si en algún momento se
    reactiva), simplemente se ignora al decidir compras."""
    try:
        return leer_planilla_tickers(RUTA_TICKERS_CSV, solo_activos=False)
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
        filas = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
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
            fecha_salida_dt = _normalizar_valor_fecha_sheets(fila["Fecha Salida"])
            if fecha_salida_dt is None:
                continue
            fecha_salida = fecha_salida_dt.date()
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
        registros = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception as e:
        print(f"[niveles] no se pudo leer Operaciones Activas: {e}")
        return {}
    niveles = {}
    for r in registros:
        ticker = r["Ticker"]
        fecha_entrada_dt = _normalizar_valor_fecha_sheets(r["Fecha Entrada"])
        stop_vigente_valor = float(r["Stop Vigente"])
        # .get() con default solo se usa si la CLAVE no existe -- si la
        # celda existe pero está vacía (""), .get() igual devuelve "" y
        # float("") explota. Hay que chequear el valor, no solo la
        # presencia de la clave (bug real detectado en producción,
        # 03/08/2026, con una fila que tenía "SL Fase A ($)" en blanco).
        sl_fase_a_crudo = r.get("SL Fase A ($)", "")
        sl_fase_a_valor = sl_fase_a_crudo if sl_fase_a_crudo not in (None, "") else stop_vigente_valor
        niveles[ticker] = {
            "tipo": r.get("Tipo", ""),
            "fecha_entrada": fecha_entrada_dt.strftime("%Y-%m-%d") if fecha_entrada_dt else r["Fecha Entrada"],
            "precio_entrada": float(r["Precio Entrada"]),
            "acciones": int(r["Acciones"]),
            "fase": r.get("Fase", "A"),
            "stop_vigente": stop_vigente_valor,
            "sl_fase_a": float(sl_fase_a_valor),
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
        filas = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception as e:
        print(f"[stats] no se pudo leer Historico Ordenes TOTAL: {e}")
        return 0, 0.0, 0.0
    if not filas:
        return 0, 0.0, 0.0
    ganadoras = sum(1 for f in filas if float(f.get("P&L $", 0) or 0) > 0)
    win_rate = round(100 * ganadoras / len(filas), 1)

    try:
        ws_pnl = sheet.worksheet("P&L Total")
        filas_pnl = ws_pnl.get_all_records(value_render_option='UNFORMATTED_VALUE')
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
    True si se cerró con éxito CONFIRMADO (para que el llamador la saque
    de `niveles`). Si la orden queda pendiente de confirmar (ver
    iol_client.py, _interpretar_respuesta_orden), se registra en la cola
    "Ordenes Pendientes Confirmacion" para resolverla en el próximo
    refresh, y se devuelve False -- la posición SIGUE en `niveles` tal
    cual estaba, no se toca hasta confirmar."""
    try:
        orden_venta = iol_client.vender_mercado(ticker, nivel["acciones"])
    except Exception as e:
        tg.notificar_error(f"venta {ticker}", str(e))
        return False

    if orden_venta.get("pendiente"):
        sheets.registrar_orden_pendiente(
            sheet, ticker, "venta", orden_venta.get("numero_operacion"),
            nivel.get("tipo", ""), nivel["acciones"], orden_venta.get("precio_estimado", 0),
            datos_extra={
                "precio_entrada": nivel["precio_entrada"], "fecha_entrada": nivel["fecha_entrada"],
                "sl_fase_a": nivel.get("sl_fase_a", 0), "motivo_salida": motivo,
            },
        )
        tg.notificar_orden_pendiente(ticker, "venta")
        return False

    if not orden_venta.get("exito"):
        tg.notificar_error(f"venta {ticker}", f"Orden rechazada: {orden_venta}")
        return False

    precio_salida = orden_venta.get("precio_ejecutado") or precio_salida_fallback
    hoy = hoy_argentina()
    desglose = calcular_pnl_con_desglose(nivel["precio_entrada"], precio_salida, nivel["acciones"])
    pnl_pesos, pnl_pct = desglose["pnl_pesos"], desglose["pnl_pct"]
    dias_holding = (hoy - datetime.strptime(nivel["fecha_entrada"], "%Y-%m-%d").date()).days

    sheets.registrar_operacion_cerrada(
        sheet, ticker, nivel.get("tipo", ""), nivel["fecha_entrada"], nivel["precio_entrada"],
        hoy.strftime("%Y-%m-%d"), precio_salida, nivel["acciones"], nivel.get("sl_fase_a", 0),
        motivo, desglose["comision_total"], desglose["ddm_total"], pnl_pesos, pnl_pct, dias_holding,
    )
    tg.notificar_cierre_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_salida, motivo, pnl_pesos, pnl_pct)
    return True


def _calcular_snapshot_indicadores(iol_client, tickers_planilla: list, cooldown: dict, hoy: date,
                                    prefijo_log: str = "indicadores") -> dict:
    """
    Loop común de cálculo de indicadores para TODO el universo -- lo
    usan rutina_apertura, rutina_cierre (recalculo final) y
    rutina_refresh (nueva, ver más abajo). Antes estaba duplicado 2
    veces casi idéntico; factorizado acá para que los 3 lugares
    siempre calculen exactamente lo mismo, sin riesgo de que se
    desincronicen entre sí con el tiempo.
    """
    indicadores_snapshot = {}
    for fila in tickers_planilla:
        ticker, tipo, activo = fila["ticker"], fila["tipo"], fila["activo"]
        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            precio_en_vivo = iol_client.obtener_precio(ticker)
            df = forzar_cierre_de_hoy(df, precio_en_vivo, hoy)
            df = calcular_indicadores(df)
            df = generar_senales_bb_touch_bbw(df, bbw_umbral=BBW_UMBRAL, pct_movido_maximo=PCT_MOVIDO_MAXIMO)
        except Exception as e:
            print(f"[{prefijo_log}] {ticker}: error al descargar/calcular ({e})")
            continue
        if df.empty:
            continue

        ultimo = df.iloc[-1]
        senal_confirmada_hoy = bool(ultimo["senal_confirmada"])
        fecha_toque, dias_pendiente, pct_movido_desde_toque = extraer_info_pendiente(df, hoy)
        esperando_vela_verde, fecha_confirmacion_bbw, dias_esperando_vela_verde = extraer_info_vela_verde(df, hoy)
        senal_pendiente = fecha_toque is not None and not senal_confirmada_hoy

        indicadores_snapshot[ticker] = {
            "tipo": tipo,
            "precio_actual": ultimo["Close"],
            "rsi14": ultimo["RSI14"],
            "bb_lower": ultimo["BB_lower"],
            "bb_mid": ultimo["BB_mid"],
            "bb_upper": ultimo["BB_upper"],
            "bbw": ultimo["BBW"],
            "ema50": ultimo["EMA50"],
            "habilitado_compra": activo,
            "senal_pendiente": senal_pendiente,
            "fecha_toque_banda": fecha_toque,
            "pct_movido_desde_toque": pct_movido_desde_toque,
            "esperando_vela_verde": esperando_vela_verde,
            "fecha_confirmacion_bbw": fecha_confirmacion_bbw,
            "dias_esperando_vela_verde": dias_esperando_vela_verde,
            "dias_pendiente": dias_pendiente,
            "senal_confirmada": senal_confirmada_hoy,
            "en_cooldown": ticker in cooldown and cooldown[ticker] >= hoy,
        }
    return indicadores_snapshot


# ============================================================================
# RUTINA 1: APERTURA (~10:30, una vez por día) -- solo notifica + Indicadores
# ============================================================================
def rutina_apertura(iol_client):
    sheet = sheets.conectar_sheet()

    if sheets.apertura_de_hoy_ya_registrada(sheet):
        print("[apertura] ya se corrió hoy -- se salta (evita repetir mensajes de Telegram)")
        return

    tg.notificar_bot_conectado("apertura")

    tickers_planilla = leer_planilla_completa()
    cooldown = obtener_tickers_en_cooldown(sheet)
    niveles = leer_niveles_guardados(sheet)
    hoy = hoy_argentina()

    indicadores_snapshot = _calcular_snapshot_indicadores(iol_client, tickers_planilla, cooldown, hoy,
                                                           prefijo_log="apertura")

    exito_indicadores = False
    try:
        exito_indicadores = sheets.actualizar_indicadores(sheet, indicadores_snapshot)
        if not exito_indicadores:
            print("[apertura] Indicadores NO se actualizó (ver error de [sheets] arriba)")
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

    estado_indicadores = "actualizados" if exito_indicadores else "NO se pudieron escribir (ver error arriba)"
    print(f"[apertura] Indicadores {estado_indicadores} -- {len(indicadores_snapshot)} tickers calculados. "
          f"Posiciones abiertas: {len(niveles)}.")


# ============================================================================
# RUTINA 1B: REFRESH (~13:00 y ~16:00 ART, dos veces por día) -- solo
#            actualiza Sheets (Indicadores + Operaciones Activas), NO
#            compra ni vende nada. Pensada para que el usuario pueda ir
#            controlando visualmente (gráficos/planilla) cómo viene el
#            día, y actuar por su cuenta si ve algo que le preocupa,
#            antes de la ventana de cierre real (~16:45).
# ============================================================================
def resolver_ordenes_pendientes(iol_client, sheet):
    """
    *** FIX CRÍTICO -- bug real en producción, 03/08/2026 ***
    Recorre "Ordenes Pendientes Confirmacion" (órdenes de compra/venta
    que se enviaron pero no se pudo confirmar como ejecutadas en el
    momento) y consulta el estado REAL en IOL para cada una, SIN mandar
    ninguna orden nueva. Se llama al principio de rutina_refresh()
    (13hs/16hs) -- da tiempo de sobra para que una orden que tardó en
    ejecutarse ya se haya resuelto en el mercado.

    Para cada orden:
      - Si ya está "terminada" (ejecutada, confirmada con precio real):
        completa la acción real (cierra la posición en Sheets si era una
        venta, o la abre si era una compra) con el precio CONFIRMADO, y
        notifica por Telegram con el resultado real -- recién ACÁ se
        considera "cerrada"/"abierta" de verdad, no antes.
      - Si terminó rechazada/cancelada: notifica el error y la
        descarta (no se reintenta sola -- si corresponde reintentar, es
        una decisión aparte, no automática).
      - Si sigue sin resolverse: se avisa que sigue pendiente y se deja
        en la cola para el próximo refresh.
    """
    ordenes = sheets.leer_ordenes_pendientes(sheet)
    if not ordenes:
        return

    ordenes_que_siguen_pendientes = []
    hoy = hoy_argentina()

    for orden in ordenes:
        ticker = orden["ticker"]
        tipo_operacion = orden["tipo_operacion"]
        numero_operacion = orden["numero_operacion"]

        try:
            detalle = iol_client.consultar_detalle_operacion(numero_operacion)
        except Exception as e:
            print(f"[refresh] {ticker}: error al consultar orden pendiente {numero_operacion} ({e})")
            ordenes_que_siguen_pendientes.append(orden)
            continue

        estado = str(detalle.get("estadoActual", "")).lower()
        cantidad = detalle.get("cantidad") or 0
        monto_operacion = detalle.get("montoOperacion")

        if estado == "terminada" and cantidad and monto_operacion:
            precio_real = monto_operacion / cantidad
            datos_extra = orden.get("datos_extra", {})

            if tipo_operacion == "venta":
                pnl_pesos, pnl_pct = calcular_pnl(
                    datos_extra.get("precio_entrada", precio_real), precio_real, orden["acciones"]
                )
                desglose = calcular_pnl_con_desglose(
                    datos_extra.get("precio_entrada", precio_real), precio_real, orden["acciones"]
                )
                sheets.registrar_operacion_cerrada(
                    sheet, ticker, orden.get("tipo", ""), datos_extra.get("fecha_entrada", ""),
                    datos_extra.get("precio_entrada", precio_real), hoy.strftime("%Y-%m-%d"), precio_real,
                    orden["acciones"], datos_extra.get("sl_fase_a", 0), datos_extra.get("motivo_salida", "stop_loss_10pct"),
                    desglose["comision_total"], desglose["ddm_total"], desglose["pnl_pesos"], desglose["pnl_pct"],
                    (hoy - datetime.strptime(datos_extra.get("fecha_entrada", hoy.strftime("%Y-%m-%d")), "%Y-%m-%d").date()).days,
                )
                tg.notificar_cierre_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_real,
                                              datos_extra.get("motivo_salida", "stop_loss_10pct"),
                                              desglose["pnl_pesos"], desglose["pnl_pct"])
                print(f"[refresh] {ticker}: venta pendiente CONFIRMADA a ${precio_real:,.2f}")
            else:  # compra
                sl_fase_a = precio_real * (1 - SL_INICIAL_PCT)
                niveles_actuales = leer_niveles_guardados(sheet)
                niveles_actuales[ticker] = {
                    "tipo": orden.get("tipo", ""), "fecha_entrada": hoy.strftime("%Y-%m-%d"),
                    "precio_entrada": precio_real, "acciones": orden["acciones"],
                    "fase": "A", "stop_vigente": sl_fase_a, "sl_fase_a": sl_fase_a,
                }
                _reescribir_operaciones_activas(iol_client, sheet, niveles_actuales)
                tg.notificar_apertura_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_real,
                                                sl_fase_a, acciones=orden["acciones"])
                print(f"[refresh] {ticker}: compra pendiente CONFIRMADA a ${precio_real:,.2f}")
            continue  # se resolvió -- no vuelve a la cola

        if "rechaz" in estado or "cancel" in estado:
            tg.notificar_orden_pendiente_resuelta_error(ticker, tipo_operacion, f"estado: {estado}")
            print(f"[refresh] {ticker}: orden pendiente terminó {estado} -- se descarta")
            continue  # se resolvió (mal) -- tampoco vuelve a la cola

        # sigue sin resolverse
        tg.notificar_orden_sigue_pendiente(ticker, tipo_operacion)
        ordenes_que_siguen_pendientes.append(orden)

    sheets.actualizar_ordenes_pendientes(sheet, ordenes_que_siguen_pendientes)


def rutina_refresh(iol_client):
    sheet = sheets.conectar_sheet()

    resolver_ordenes_pendientes(iol_client, sheet)

    tickers_planilla = leer_planilla_completa()
    cooldown = obtener_tickers_en_cooldown(sheet)
    niveles = leer_niveles_guardados(sheet)
    hoy = hoy_argentina()

    # Operaciones Activas -- mark-to-market con precio en vivo de las
    # posiciones que ya están abiertas (si hay alguna).
    _reescribir_operaciones_activas(iol_client, sheet, niveles)

    # Indicadores -- mismo cálculo completo que apertura/cierre, así el
    # usuario ve durante el día exactamente los mismos datos que el bot
    # usa para decidir, no una versión resumida aparte.
    indicadores_snapshot = _calcular_snapshot_indicadores(iol_client, tickers_planilla, cooldown, hoy,
                                                           prefijo_log="refresh")
    exito_indicadores = sheets.actualizar_indicadores(sheet, indicadores_snapshot)

    estado_indicadores = "actualizados" if exito_indicadores else "NO se pudieron escribir (ver error arriba)"
    print(f"[refresh] Indicadores {estado_indicadores} -- {len(indicadores_snapshot)} tickers calculados. "
          f"Posiciones abiertas: {len(niveles)}.")

    # A propósito NO manda mensaje de Telegram -- el objetivo es que el
    # usuario revise la planilla/gráficos por su cuenta cuando quiera,
    # no generar dos notificaciones más por día. Si en algún momento se
    # quiere un aviso liviano acá, es un agregado simple más adelante.


# ============================================================================
# RUTINA 2: MONITOREO FASE A (cada 10 min) -- solo SL 10% intradía
# ============================================================================
def _tickers_con_orden_pendiente(sheet, tipo_operacion: str = None) -> set:
    """Devuelve el set de tickers que ya tienen una orden (compra o
    venta) pendiente de confirmar -- para NO mandar una orden nueva
    sobre el mismo ticker mientras la anterior no se resolvió (evita
    duplicar órdenes). Si `tipo_operacion` se especifica ("compra" o
    "venta"), filtra solo ese tipo."""
    ordenes = sheets.leer_ordenes_pendientes(sheet)
    return {
        o["ticker"] for o in ordenes
        if tipo_operacion is None or o["tipo_operacion"] == tipo_operacion
    }


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
    tickers_con_venta_pendiente = _tickers_con_orden_pendiente(sheet, tipo_operacion="venta")
    tickers_fase_a = [
        t for t, n in niveles.items()
        if n.get("fase", "A") == "A" and t in posiciones_iol and t not in tickers_con_venta_pendiente
    ]
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

    if sheets.dashboard_de_hoy_ya_registrado(sheet):
        print("[cierre] ya se corrió hoy -- se salta (evita repetir mensajes de Telegram)")
        return

    try:
        posiciones_iol = iol_client.consultar_posiciones()
    except Exception as e:
        tg.notificar_error("cierre - consulta posiciones", str(e))
        posiciones_iol = {}

    niveles = leer_niveles_guardados(sheet)
    cooldown = obtener_tickers_en_cooldown(sheet)
    tickers_planilla_completa = leer_planilla_completa()  # para Indicadores (todos)
    tickers_planilla = leer_planilla_activa()              # para decidir compras (solo activo=SI)
    tickers_con_venta_pendiente = _tickers_con_orden_pendiente(sheet, tipo_operacion="venta")

    # ------------------------------------------------------------------
    # (a) + (b): evaluar posiciones abiertas -- transición A->B y salida
    #     por cierre-bajo-EMA50 en Fase B. Se recorre SOLO lo que sigue
    #     abierto en IOL (lo que ya vendió monitoreo_fase_a no está acá).
    # ------------------------------------------------------------------
    for ticker in list(niveles.keys()):
        if ticker not in posiciones_iol:
            continue  # ya no está en cartera (lo cerró monitoreo_fase_a hoy)
        if ticker in tickers_con_venta_pendiente:
            continue  # ya hay una venta en curso sin confirmar -- no duplicar la orden
        nivel = niveles[ticker]
        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            precio_en_vivo = iol_client.obtener_precio(ticker)
            df = forzar_cierre_de_hoy(df, precio_en_vivo, hoy)
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
    # ------------------------------------------------------------------
    # (c1) Cola de reintento -- señales confirmadas en días anteriores
    #      que no se pudieron comprar por falta de efectivo. Se procesan
    #      ANTES que las señales nuevas del día (tienen prioridad sobre
    #      el capital disponible, por haber confirmado primero). Se
    #      invalidan si pasan más de DIAS_MAXIMO_REINTENTO_EJECUCION
    #      velas sin ejecutarse, o si el precio ya cayó más de
    #      SL_INICIAL_PCT desde el precio de confirmación original --
    #      lo que pase primero.
    # ------------------------------------------------------------------
    try:
        efectivo = iol_client.consultar_saldo()
    except Exception as e:
        tg.notificar_error("cierre - consulta saldo", str(e))
        efectivo = 0.0

    cola_pendientes = sheets.leer_cola_senales_pendientes(sheet)
    cola_actualizada = []
    dias_transcurridos_por_ticker = {}

    for item in cola_pendientes:
        ticker = item["ticker"]
        if ticker in niveles:
            continue  # ya se abrió por otra vía -- se descarta de la cola
        if ticker in cooldown and cooldown[ticker] >= hoy:
            continue  # entró en cooldown mientras esperaba -- se descarta

        try:
            fecha_confirmacion = datetime.strptime(item["fecha_confirmacion"], "%Y-%m-%d").date()
        except Exception:
            continue  # fecha corrupta en la hoja -- se descarta, no rompe el resto
        dias_transcurridos = (hoy - fecha_confirmacion).days

        if dias_transcurridos > DIAS_MAXIMO_REINTENTO_EJECUCION:
            print(f"[cierre] {ticker}: señal pendiente invalidada -- superó "
                  f"{DIAS_MAXIMO_REINTENTO_EJECUCION} días sin ejecutarse")
            continue

        try:
            precio_en_vivo = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[cierre] {ticker}: error al obtener precio para reintento ({e})")
            cola_actualizada.append(item)  # error transitorio -- se reintenta el próximo cierre
            dias_transcurridos_por_ticker[ticker] = dias_transcurridos
            continue

        if precio_en_vivo < item["stop_referencia"]:
            print(f"[cierre] {ticker}: señal pendiente invalidada -- precio (${precio_en_vivo:,.2f}) "
                  f"cayó por debajo del stop de referencia (${item['stop_referencia']:,.2f})")
            continue

        if efectivo <= 0:
            cola_actualizada.append(item)
            dias_transcurridos_por_ticker[ticker] = dias_transcurridos
            continue

        acciones = calcular_acciones_por_capital_objetivo(efectivo, precio_en_vivo, TOPE_MAXIMO_POSICION)
        if acciones <= 0:
            cola_actualizada.append(item)  # sigue sin alcanzar el capital -- reintenta después
            dias_transcurridos_por_ticker[ticker] = dias_transcurridos
            continue

        try:
            orden_compra = iol_client.comprar_mercado(ticker, acciones)
        except Exception as e:
            tg.notificar_error(f"compra (reintento) {ticker}", str(e))
            cola_actualizada.append(item)
            dias_transcurridos_por_ticker[ticker] = dias_transcurridos
            continue

        if orden_compra.get("pendiente"):
            sheets.registrar_orden_pendiente(
                sheet, ticker, "compra", orden_compra.get("numero_operacion"),
                item.get("tipo", ""), acciones, orden_compra.get("precio_estimado", 0),
                datos_extra={},
            )
            tg.notificar_orden_pendiente(ticker, "compra")
            # la señal SALE de la cola de reintento -- ya no es "sin capital", está
            # en curso en la cola de confirmación (resolver_ordenes_pendientes se
            # encarga de completarla o descartarla, no vuelve a esta cola).
            continue

        if not orden_compra.get("exito"):
            tg.notificar_error(f"compra (reintento) {ticker}", f"Orden rechazada: {orden_compra}")
            cola_actualizada.append(item)
            dias_transcurridos_por_ticker[ticker] = dias_transcurridos
            continue

        precio_ejecutado = orden_compra.get("precio_ejecutado") or precio_en_vivo
        efectivo -= precio_ejecutado * acciones
        sl_fase_a = precio_ejecutado * (1 - SL_INICIAL_PCT)
        niveles[ticker] = {
            "tipo": item.get("tipo", ""),
            "fecha_entrada": hoy.strftime("%Y-%m-%d"),
            "precio_entrada": precio_ejecutado,
            "acciones": acciones,
            "fase": "A",
            "stop_vigente": sl_fase_a,
            "sl_fase_a": sl_fase_a,
        }
        tg.notificar_apertura_posicion(ticker, hoy.strftime("%d/%m/%Y"), precio_ejecutado,
                                        sl_fase_a, acciones=acciones)
        print(f"[cierre] {ticker}: comprado desde la cola de reintento "
              f"(confirmó el {item['fecha_confirmacion']}, {dias_transcurridos} días después)")

    # ------------------------------------------------------------------
    # (c2) Entradas nuevas -- capital compartido, reparto en orden de
    #      planilla, precio en vivo de esta ventana como proxy de cierre.
    #      Si confirma pero no alcanza el capital, se ENCOLA para
    #      reintentar en los próximos cierres (ver bloque (c1) arriba),
    #      en vez de perderse.
    # ------------------------------------------------------------------
    tickers_en_cola = {item["ticker"] for item in cola_actualizada}
    tickers_con_compra_pendiente = _tickers_con_orden_pendiente(sheet, tipo_operacion="compra")

    for fila in tickers_planilla:
        ticker, tipo = fila["ticker"], fila["tipo"]
        if ticker in niveles:
            continue  # ya hay posición abierta
        if ticker in cooldown and cooldown[ticker] >= hoy:
            continue
        if ticker in tickers_en_cola:
            continue  # ya está en la cola de reintento, se procesa en (c1)
        if ticker in tickers_con_compra_pendiente:
            continue  # ya hay una compra en curso sin confirmar -- no duplicar la orden

        try:
            precio_en_vivo = iol_client.obtener_precio(ticker)
        except Exception as e:
            print(f"[cierre] {ticker}: error al obtener precio en vivo ({e})")
            continue

        try:
            df = descargar_datos_diarios(ticker, periodo="6mo")
            df = forzar_cierre_de_hoy(df, precio_en_vivo, hoy)
            df = calcular_indicadores(df)
            df = generar_senales_bb_touch_bbw(df, bbw_umbral=BBW_UMBRAL, pct_movido_maximo=PCT_MOVIDO_MAXIMO)
        except Exception as e:
            print(f"[cierre] {ticker}: error al descargar/calcular señal ({e})")
            continue
        if df.empty or not bool(df["senal_confirmada"].iloc[-1]):
            continue

        precio_entrada_aprox = precio_en_vivo

        acciones = calcular_acciones_por_capital_objetivo(efectivo, precio_entrada_aprox, TOPE_MAXIMO_POSICION) if efectivo > 0 else 0
        if acciones <= 0:
            # confirmó hoy, pero no hay capital suficiente -- se encola
            # para reintentar en los próximos cierres, en vez de perderse.
            stop_referencia = precio_entrada_aprox * (1 - SL_INICIAL_PCT)
            cola_actualizada.append({
                "ticker": ticker, "tipo": tipo,
                "fecha_confirmacion": hoy.strftime("%Y-%m-%d"),
                "precio_confirmacion": precio_entrada_aprox,
                "stop_referencia": stop_referencia,
            })
            tickers_en_cola.add(ticker)
            dias_transcurridos_por_ticker[ticker] = 0
            print(f"[cierre] {ticker}: señal confirmada pero sin capital disponible -- "
                  f"se encola para reintentar (hasta {DIAS_MAXIMO_REINTENTO_EJECUCION} días)")
            continue

        try:
            orden_compra = iol_client.comprar_mercado(ticker, acciones)
        except Exception as e:
            tg.notificar_error(f"compra {ticker}", str(e))
            _encolar_senal_no_ejecutada(cola_actualizada, tickers_en_cola, dias_transcurridos_por_ticker,
                                         ticker, tipo, hoy, precio_entrada_aprox)
            continue

        if orden_compra.get("pendiente"):
            sheets.registrar_orden_pendiente(
                sheet, ticker, "compra", orden_compra.get("numero_operacion"),
                tipo, acciones, orden_compra.get("precio_estimado", 0),
                datos_extra={},
            )
            tg.notificar_orden_pendiente(ticker, "compra")
            continue

        if not orden_compra.get("exito"):
            tg.notificar_error(f"compra {ticker}", f"Orden rechazada: {orden_compra}")
            _encolar_senal_no_ejecutada(cola_actualizada, tickers_en_cola, dias_transcurridos_por_ticker,
                                         ticker, tipo, hoy, precio_entrada_aprox)
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
    #     sola vez por día pese a los reintentos de la ventana, e
    #     Indicadores de nuevo -- ahora con el precio de CIERRE real del
    #     día (a diferencia del snapshot de apertura, que solo tenía el
    #     cierre de ayer disponible).
    # ------------------------------------------------------------------
    _reescribir_operaciones_activas(iol_client, sheet, niveles)

    # recalcular días transcurridos finales para los ítems que quedaron
    # en la cola (los que ya estaban de antes, no los recién agregados
    # que ya tienen 0 asignado arriba)
    for item in cola_actualizada:
        if item["ticker"] not in dias_transcurridos_por_ticker:
            try:
                fecha_conf = datetime.strptime(item["fecha_confirmacion"], "%Y-%m-%d").date()
                dias_transcurridos_por_ticker[item["ticker"]] = (hoy - fecha_conf).days
            except Exception:
                dias_transcurridos_por_ticker[item["ticker"]] = ""
    sheets.actualizar_cola_senales_pendientes(sheet, cola_actualizada, dias_transcurridos_por_ticker)

    try:
        indicadores_cierre = _calcular_snapshot_indicadores(iol_client, tickers_planilla_completa, cooldown, hoy,
                                                              prefijo_log="cierre")
        exito_indicadores_cierre = sheets.actualizar_indicadores(sheet, indicadores_cierre)
        if not exito_indicadores_cierre:
            print("[cierre] Indicadores NO se actualizó (ver error de [sheets] arriba)")
    except Exception as e:
        print(f"[cierre] no se pudo actualizar Indicadores de cierre: {e}")

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
# En la práctica, apertura/cierre/refresh se disparan en el minuto exacto
# vía un cron EXTERNO (cron-job.org, ver bot_bb_touch.yml) que llama a
# workflow_dispatch con MODO_RUTINA -- estas ventanas son el respaldo del
# cron interno de GitHub, por si el externo alguna vez falla.
VENTANA_APERTURA = (dtime(10, 27), dtime(11, 0))
VENTANA_REFRESH_1 = (dtime(13, 0), dtime(13, 15))   # refresh de mediodía, solo lectura
VENTANA_REFRESH_2 = (dtime(16, 0), dtime(16, 15))   # refresh de la tarde, solo lectura
VENTANA_CIERRE = (dtime(16, 45), dtime(16, 58))     # movido de 16:27 a 16:45 (29/07/2026)


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
        elif modo_manual == "refresh":
            rutina_refresh(iol_client)
        elif modo_manual == "monitoreo":
            rutina_monitoreo_fase_a(iol_client)
        elif modo_manual == "cierre":
            rutina_cierre(iol_client)
        else:
            raise ValueError(f"MODO_RUTINA debe ser 'apertura', 'refresh', 'monitoreo' o 'cierre', "
                              f"recibido: '{modo_manual}'")
    else:
        hora_actual = datetime.now(TZ_ARGENTINA).time()

        if VENTANA_APERTURA[0] <= hora_actual <= VENTANA_APERTURA[1]:
            rutina_apertura(iol_client)
        elif VENTANA_REFRESH_1[0] <= hora_actual <= VENTANA_REFRESH_1[1]:
            rutina_refresh(iol_client)
        elif VENTANA_REFRESH_2[0] <= hora_actual <= VENTANA_REFRESH_2[1]:
            rutina_refresh(iol_client)
        elif VENTANA_CIERRE[0] <= hora_actual <= VENTANA_CIERRE[1]:
            rutina_cierre(iol_client)
        elif _debe_correr_monitoreo(hora_actual):
            rutina_monitoreo_fase_a(iol_client)
        else:
            print(f"[monitoreo] throttle -- se salta esta corrida ({hora_actual.strftime('%H:%M')})")
