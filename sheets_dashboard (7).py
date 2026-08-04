# -*- coding: utf-8 -*-
"""
============================================================================
 DASHBOARD GOOGLE SHEETS -- Bot BB-Touch + BBW + EMA50
============================================================================

Setup necesario (una sola vez):
  1. En Google Cloud Console: habilitar "Google Sheets API" y "Google
     Drive API", crear una Service Account, generar clave JSON.
  2. Crear una planilla nueva en Google Sheets con estas 9 hojas (tabs),
     con los encabezados de la fila 1 EXACTAMENTE como se listan abajo
     (se crean a mano una sola vez; el bot escribe desde la fila 2):

  "Operaciones Activas":
      Ticker | Tipo | Fecha Entrada | Precio Entrada | Acciones |
      Precio Actual | Fase | Stop Vigente | SL Fase A ($) | P&L $ |
      P&L % | Días en Posición

  "Historico Ordenes TOTAL":
      Ticker | Tipo | Fecha Entrada | Precio Entrada | Fecha Salida |
      Precio Salida | Acciones | SL Fase A ($) | Motivo Salida |
      Comisión Total ($) | Derechos Mercado ($) | P&L $ | P&L % |
      Días Holding

  "P&L Total":
      Fecha | Capital Inicial | Efectivo | Valor Posiciones |
      Capital Total | Retorno % | Operaciones Totales | Win Rate % |
      Max Drawdown % | Posiciones Activas

  "Historico CEDEAR", "Historico Merval Lider", "Historico Merval General":
      mismas columnas que "Historico Ordenes TOTAL" -- cada cierre se
      escribe en TOTAL y, además, en la hoja de su categoría (`tipo` del
      ticker en tickers_activos.csv), para tener el desglose sin
      depender de fórmulas QUERY/FILTER frágiles ante ediciones manuales.

  "Indicadores":
      Ticker | Tipo | Fecha | Precio Actual | RSI14 | BB Inferior |
      BB Media | BB Superior | BBW | EMA50 | Habilitado Compra |
      Señal Pendiente | % Movido desde Toque | Fecha Toque Banda |
      Días Pendiente | Esperando Vela Verde | Fecha Confirmación BBW |
      Días Esperando Vela Verde | Señal Confirmada | En Cooldown

  "Señales Pendientes Ejecución":
      Ticker | Tipo | Fecha Confirmación | Precio Confirmación |
      Stop Referencia | Días Transcurridos
      -- señales que confirmaron (BBW cruzó el umbral) pero no se
      pudieron comprar ese mismo día por falta de efectivo. Se
      reintentan en cada rutina_cierre() siguiente, hasta
      DIAS_MAXIMO_REINTENTO_EJECUCION (3 velas) o hasta que el precio
      caiga más de sl_inicial_pct desde el precio de confirmación --
      lo que pase primero invalida la señal. Ver docstring de
      bot_bb_touch_diario.py, sección "cola de reintento".

  "Ordenes Pendientes Confirmacion" (agregada 03/08/2026, fix crítico):
      Ticker | Tipo Operacion | Numero Operacion | Fecha Intento | Tipo |
      Acciones | Precio Estimado | Datos Extra
      -- órdenes de compra/venta que se enviaron a IOL pero no se pudo
      confirmar si se ejecutaron de verdad tras los reintentos (ver
      iol_client.py, _interpretar_respuesta_orden). Se resuelven en
      cada rutina_refresh() (13hs/16hs) consultando el número de
      operación real, SIN mandar una orden nueva -- evita el bug real
      detectado en producción donde el bot reportaba una posición como
      cerrada/abierta antes de que la orden se hubiera ejecutado de
      verdad.

  3. Compartir la planilla con el email de la Service Account (permiso
     Editor). Guardar el JSON de credenciales y el ID de la planilla
     como secrets de GitHub Actions (GOOGLE_SHEETS_CREDENTIALS_JSON,
     GOOGLE_SHEETS_ID).

GRÁFICOS: este módulo deja los datos de "P&L Total" listos en filas
crecientes (una por día) para que los gráficos se armen UNA VEZ a mano
en Sheets apuntando a esas columnas -- se actualizan solos porque el
rango crece. No se generan gráficos por API acá (agregaría llamadas de
bajo nivel a Sheets API v4 sin necesidad real).
============================================================================
"""

import os
import json
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def _num(valor, decimales=2, default=0.0):
    """Redondea un número para Sheets, pero antes lo sanea contra NaN/Infinity
    -- típico en tickers recién listados sin historial suficiente para
    calcular EMA50/RSI/BBW. Un solo valor NaN/inf en todo el lote rompe la
    escritura COMPLETA a Sheets (JSON no admite esos valores), así que
    hay que limpiarlos antes de armar cada fila, no después."""
    try:
        numero = float(valor)
    except (TypeError, ValueError):
        return default
    if numero != numero or numero in (float("inf"), float("-inf")):  # NaN != NaN
        return default
    return round(numero, decimales)


def _fecha_str_desde_valor(valor, con_hora: bool = True) -> str:
    """
    *** FIX CRÍTICO -- bug real en producción, 03/08/2026 ***
    Con `value_render_option='UNFORMATTED_VALUE'` (necesario para evitar
    el bug de números corrompidos por formato regional, ver
    bot_bb_touch_diario.py/_normalizar_valor_fecha_sheets), si Sheets
    auto-detectó una celda de texto tipo fecha y la convirtió en una
    fecha real, ese valor vuelve como NÚMERO SERIAL (días desde
    30/12/1899) en vez del texto "dd/mm/aaaa hh:mm" que este módulo
    espera para los chequeos de idempotencia (`.startswith(...)`). Sin
    esta normalización, esos chequeos comparan un string numérico
    contra una fecha y SIEMPRE dan False -- rompiendo el freno de
    idempotencia por completo (repetiría apertura/cierre en cada
    reintento del cron). Normaliza ambos casos al string esperado.
    """
    if valor is None or valor == "":
        return ""
    if isinstance(valor, (int, float)):
        fecha = datetime(1899, 12, 30) + timedelta(days=float(valor))
        return fecha.strftime("%d/%m/%Y %H:%M" if con_hora else "%d/%m/%Y")
    return str(valor)

# Nombre de hoja histórica por tipo de ticker -- si aparece un `tipo`
# nuevo que no está acá, el cierre igual se registra en TOTAL, pero se
# imprime un aviso en vez de fallar (así un typo en tickers_activos.csv
# no frena al bot).
HOJA_POR_TIPO = {
    "cedear": "Historico CEDEAR",
    "merval_lider": "Historico Merval Lider",
    "merval_general": "Historico Merval General",
}


def conectar_sheet():
    """Devuelve el objeto Spreadsheet de gspread, o None si falla (el
    bot no debe frenar sus operaciones por un problema del dashboard)."""
    if not GOOGLE_SHEETS_ID or not GOOGLE_SHEETS_CREDENTIALS_JSON:
        print("[sheets] GOOGLE_SHEETS_ID / GOOGLE_SHEETS_CREDENTIALS_JSON no configurados")
        return None
    try:
        info = json.loads(GOOGLE_SHEETS_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        client = gspread.authorize(creds)
        return client.open_by_key(GOOGLE_SHEETS_ID)
    except Exception as e:
        print(f"[sheets] error al conectar: {e}")
        return None


def _hoy_str() -> str:
    return datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y")


def _ahora_str() -> str:
    return datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y %H:%M")


# ============================================================================
# 1) OPERACIONES ACTIVAS -- se reescribe completa en cada corrida
# ============================================================================
def actualizar_operaciones_activas(sheet, posiciones: dict):
    """
    `posiciones`: dict ticker -> {
        "tipo": str, "fecha_entrada": str, "precio_entrada": float,
        "acciones": int, "precio_actual": float, "fase": "A" | "B",
        "stop_vigente": float, "sl_fase_a": float, "dias_en_posicion": int,
    }
    P&L $ / % se calculan acá mismo contra `precio_actual` (mark-to-market,
    no es el resultado final -- eso lo registra el histórico al cerrar).
    """
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Operaciones Activas")
        ws.clear()
        filas = [["Ticker", "Tipo", "Fecha Entrada", "Precio Entrada", "Acciones",
                   "Precio Actual", "Fase", "Stop Vigente", "SL Fase A ($)",
                   "P&L $", "P&L %", "Días en Posición"]]
        for ticker, p in sorted(posiciones.items()):
            precio_entrada = p.get("precio_entrada", 0)
            acciones = p.get("acciones", 0)
            precio_actual = p.get("precio_actual", 0)
            pnl_pesos = (precio_actual - precio_entrada) * acciones
            pnl_pct = 100 * (precio_actual - precio_entrada) / precio_entrada if precio_entrada else 0
            filas.append([
                ticker, p.get("tipo", ""), p.get("fecha_entrada", ""),
                _num(precio_entrada), acciones, _num(precio_actual),
                p.get("fase", ""), _num(p.get("stop_vigente", 0)),
                _num(p.get("sl_fase_a", 0)),
                _num(pnl_pesos), _num(pnl_pct),
                p.get("dias_en_posicion", 0),
            ])
        ws.update(range_name="A1", values=filas)
    except Exception as e:
        print(f"[sheets] error al actualizar Operaciones Activas: {e}")


# ============================================================================
# 2) HISTORICO ORDENES TOTAL + desglose por tipo (4/5/6) -- se agrega fila
# ============================================================================
def registrar_operacion_cerrada(sheet, ticker: str, tipo: str, fecha_entrada: str,
                                 precio_entrada: float, fecha_salida: str,
                                 precio_salida: float, acciones: int,
                                 sl_fase_a: float, motivo_salida: str,
                                 comision_total: float, ddm_total: float,
                                 pnl_pesos: float, pnl_pct: float, dias_holding: int):
    """Agrega la operación cerrada en 'Historico Ordenes TOTAL' y, además,
    en la hoja de su categoría (CEDEAR / Merval Lider / Merval General).
    `comision_total`/`ddm_total`: suma de la pata de compra + la pata de
    venta (ver calcular_pnl_con_desglose en bb_touch_ema50_estrategia.py)."""
    if sheet is None:
        return
    fila = [
        ticker, tipo, fecha_entrada, round(precio_entrada, 2), fecha_salida,
        round(precio_salida, 2), acciones, round(sl_fase_a, 2), motivo_salida,
        round(comision_total, 2), round(ddm_total, 2),
        round(pnl_pesos, 2), round(pnl_pct, 2), dias_holding,
    ]
    try:
        sheet.worksheet("Historico Ordenes TOTAL").append_row(fila)
    except Exception as e:
        print(f"[sheets] error al registrar en Historico TOTAL: {e}")

    nombre_hoja_tipo = HOJA_POR_TIPO.get(tipo)
    if nombre_hoja_tipo is None:
        print(f"[sheets] tipo '{tipo}' sin hoja histórica asociada -- solo quedó en TOTAL")
        return
    try:
        sheet.worksheet(nombre_hoja_tipo).append_row(fila)
    except Exception as e:
        print(f"[sheets] error al registrar en {nombre_hoja_tipo}: {e}")


# ============================================================================
# 3) P&L TOTAL (dashboard) -- una fila nueva por día
# ============================================================================
def dashboard_de_hoy_ya_registrado(sheet) -> bool:
    """Evita filas duplicadas si rutina_cierre reintenta varias veces en
    la misma ventana (16:27-16:50)."""
    if sheet is None:
        return False
    try:
        ws = sheet.worksheet("P&L Total")
        filas = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception:
        return False
    if not filas:
        return False
    return _fecha_str_desde_valor(filas[-1].get("Fecha", "")).startswith(_hoy_str())


def actualizar_dashboard_pnl(sheet, capital_inicial: float, efectivo: float,
                              valor_posiciones: float, operaciones_totales: int,
                              win_rate_pct: float, max_drawdown_pct: float,
                              posiciones_activas: int = 0):
    """`posiciones_activas`: cantidad de posiciones abiertas al momento
    del cierre de hoy (agregada 03/08/2026, a pedido del usuario) --
    columna nueva AL FINAL, como corresponde en una pestaña append_row
    (nunca en el medio, rompería la alineación de filas históricas)."""
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("P&L Total")
        capital_total = efectivo + valor_posiciones
        retorno_pct = 100 * (capital_total - capital_inicial) / capital_inicial if capital_inicial else 0
        ws.append_row([
            _ahora_str(), round(capital_inicial, 2), round(efectivo, 2),
            round(valor_posiciones, 2), round(capital_total, 2),
            round(retorno_pct, 2), operaciones_totales, round(win_rate_pct, 2),
            round(max_drawdown_pct, 2), posiciones_activas,
        ])
    except Exception as e:
        print(f"[sheets] error al actualizar P&L Total: {e}")


# ============================================================================
# 7) INDICADORES -- se reescribe completa 1 vez por día en apertura
# ============================================================================
def apertura_de_hoy_ya_registrada(sheet) -> bool:
    """Evita repetir rutina_apertura() (y sus mensajes de Telegram) en
    cada reintento del cron dentro de la ventana 10:27-11:00 -- si la
    primera corrida del día ya tuvo éxito, las siguientes se saltan."""
    if sheet is None:
        return False
    try:
        ws = sheet.worksheet("Indicadores")
        filas = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception:
        return False
    if not filas:
        return False
    fecha_hoy = datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y")
    return _fecha_str_desde_valor(filas[0].get("Fecha", "")).startswith(fecha_hoy)


def actualizar_indicadores(sheet, indicadores: dict):
    """
    `indicadores`: dict ticker -> {
        "tipo": str, "precio_actual": float, "rsi14": float,
        "bb_lower": float, "bb_mid": float, "bb_upper": float,
        "bbw": float, "ema50": float, "senal_pendiente": bool,
        "fecha_toque_banda": str | None, "dias_pendiente": int | None,
        "senal_confirmada": bool, "en_cooldown": bool,
    }
    `fecha_toque_banda`/`dias_pendiente` solo tienen valor cuando
    `senal_pendiente` es True -- de dónde salió el "reloj" de espera
    vigente (se reinicia con cada toque de banda nuevo, ver
    generar_senales_bb_touch_bbw en bb_touch_ema50_estrategia.py).
    """
    if sheet is None:
        return False
    try:
        ws = sheet.worksheet("Indicadores")
        ws.clear()
        filas = [["Ticker", "Tipo", "Fecha", "Precio Actual", "RSI14",
                   "BB Inferior", "BB Media", "BB Superior", "BBW", "EMA50", "Habilitado Compra",
                   "Señal Pendiente", "% Movido desde Toque", "Fecha Toque Banda", "Días Pendiente",
                   "Esperando Vela Verde", "Fecha Confirmación BBW", "Días Esperando Vela Verde",
                   "Señal Confirmada", "En Cooldown"]]
        ahora = _ahora_str()
        for ticker, ind in sorted(indicadores.items()):
            filas.append([
                ticker, ind.get("tipo", ""), ahora,
                _num(ind.get("precio_actual", 0)), _num(ind.get("rsi14", 0)),
                _num(ind.get("bb_lower", 0)), _num(ind.get("bb_mid", 0)),
                _num(ind.get("bb_upper", 0)), _num(ind.get("bbw", 0), decimales=3),
                _num(ind.get("ema50", 0)),
                ind.get("habilitado_compra", "NO"),
                "SI" if ind.get("senal_pendiente") else "no",
                ind.get("pct_movido_desde_toque") if ind.get("pct_movido_desde_toque") is not None else "",
                ind.get("fecha_toque_banda") or "",
                ind.get("dias_pendiente") if ind.get("dias_pendiente") is not None else "",
                "SI" if ind.get("esperando_vela_verde") else "no",
                ind.get("fecha_confirmacion_bbw") or "",
                ind.get("dias_esperando_vela_verde") if ind.get("dias_esperando_vela_verde") is not None else "",
                "SI" if ind.get("senal_confirmada") else "no",
                "SI" if ind.get("en_cooldown") else "no",
            ])
        ws.update(range_name="A1", values=filas)
        return True
    except Exception as e:
        print(f"[sheets] error al actualizar Indicadores: {e}")
        return False


# ============================================================================
# 8) SEÑALES PENDIENTES EJECUCIÓN -- cola de reintento por falta de capital
# ============================================================================
def leer_cola_senales_pendientes(sheet) -> list:
    """
    Devuelve la lista de señales confirmadas que todavía no se pudieron
    comprar (por falta de efectivo el día que confirmaron), en el mismo
    orden en que quedaron guardadas. Cada elemento:
    {ticker, tipo, fecha_confirmacion (str "YYYY-MM-DD"),
     precio_confirmacion (float), stop_referencia (float)}
    """
    if sheet is None:
        return []
    try:
        ws = sheet.worksheet("Señales Pendientes Ejecución")
        registros = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception as e:
        print(f"[sheets] no se pudo leer Señales Pendientes Ejecución: {e}")
        return []
    cola = []
    for r in registros:
        try:
            fecha_confirmacion_valor = r["Fecha Confirmación"]
            if isinstance(fecha_confirmacion_valor, (int, float)):
                fecha_confirmacion_valor = (
                    datetime(1899, 12, 30) + timedelta(days=float(fecha_confirmacion_valor))
                ).strftime("%Y-%m-%d")
            cola.append({
                "ticker": r["Ticker"],
                "tipo": r.get("Tipo", ""),
                "fecha_confirmacion": fecha_confirmacion_valor,
                "precio_confirmacion": float(r["Precio Confirmación"]),
                "stop_referencia": float(r["Stop Referencia"]),
            })
        except (KeyError, ValueError) as e:
            print(f"[sheets] fila inválida en Señales Pendientes Ejecución, se ignora: {e}")
    return cola


def actualizar_cola_senales_pendientes(sheet, cola: list, dias_transcurridos_por_ticker: dict = None):
    """Reescribe completa la pestaña con el estado actual de la cola de
    reintento -- mismo patrón que Operaciones Activas (se recalcula
    entera en cada corrida, no se hace append incremental)."""
    if sheet is None:
        return
    dias_transcurridos_por_ticker = dias_transcurridos_por_ticker or {}
    try:
        ws = sheet.worksheet("Señales Pendientes Ejecución")
        ws.clear()
        filas = [["Ticker", "Tipo", "Fecha Confirmación", "Precio Confirmación",
                   "Stop Referencia", "Días Transcurridos"]]
        for item in cola:
            filas.append([
                item["ticker"], item.get("tipo", ""), item["fecha_confirmacion"],
                _num(item["precio_confirmacion"]), _num(item["stop_referencia"]),
                dias_transcurridos_por_ticker.get(item["ticker"], ""),
            ])
        ws.update(range_name="A1", values=filas)
    except Exception as e:
        print(f"[sheets] error al actualizar Señales Pendientes Ejecución: {e}")


# ============================================================================
# 9) ÓRDENES PENDIENTES DE CONFIRMACIÓN -- fix crítico 03/08/2026
# ============================================================================
# Ver iol_client.py: _interpretar_respuesta_orden ahora devuelve
# `exito=False, pendiente=True` cuando una orden se envió pero no se
# pudo confirmar como ejecutada tras los reintentos -- en vez de
# reportarla como cerrada/abierta con un precio estimado (bug real:
# avisó "cerrado" con un PNL calculado sobre un precio que la orden
# todavía no había alcanzado). Esta pestaña persiste esas órdenes para
# que bot_bb_touch_diario.py las resuelva en el próximo refresh
# (13hs/16hs), sin volver a mandar una orden nueva para el mismo ticker.
def registrar_orden_pendiente(sheet, ticker: str, tipo_operacion: str, numero_operacion,
                               tipo: str, acciones: int, precio_estimado: float, datos_extra: dict):
    """`tipo_operacion`: "compra" o "venta". `datos_extra`: todo lo que
    hace falta para completar la operación una vez confirmada (para una
    venta: precio_entrada, fecha_entrada, sl_fase_a, motivo_salida; para
    una compra: no hace falta nada más, se guarda vacío)."""
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Ordenes Pendientes Confirmacion")
        ws.append_row([
            ticker, tipo_operacion, str(numero_operacion), _ahora_str(), tipo,
            acciones, _num(precio_estimado), json.dumps(datos_extra),
        ])
    except Exception as e:
        print(f"[sheets] error al registrar orden pendiente: {e}")


def leer_ordenes_pendientes(sheet) -> list:
    """Devuelve la lista de órdenes todavía sin confirmar. Cada
    elemento: {ticker, tipo_operacion, numero_operacion, fecha_intento,
    tipo, acciones, precio_estimado, datos_extra (dict)}."""
    if sheet is None:
        return []
    try:
        ws = sheet.worksheet("Ordenes Pendientes Confirmacion")
        registros = ws.get_all_records(value_render_option='UNFORMATTED_VALUE')
    except Exception as e:
        print(f"[sheets] no se pudo leer Ordenes Pendientes Confirmacion: {e}")
        return []
    ordenes = []
    for r in registros:
        try:
            datos_extra_raw = r.get("Datos Extra", "{}")
            datos_extra = json.loads(datos_extra_raw) if datos_extra_raw else {}
        except (json.JSONDecodeError, TypeError):
            datos_extra = {}
        ordenes.append({
            "ticker": r["Ticker"],
            "tipo_operacion": r.get("Tipo Operacion", ""),
            "numero_operacion": r.get("Numero Operacion", ""),
            "fecha_intento": _fecha_str_desde_valor(r.get("Fecha Intento", "")),
            "tipo": r.get("Tipo", ""),
            "acciones": int(r.get("Acciones", 0) or 0),
            "precio_estimado": float(r.get("Precio Estimado", 0) or 0),
            "datos_extra": datos_extra,
        })
    return ordenes


def actualizar_ordenes_pendientes(sheet, ordenes: list):
    """Reescribe completa la pestaña con la lista actualizada (mismo
    patrón que Operaciones Activas / cola de reintento -- se recalcula
    entera en cada corrida, no se hace append incremental)."""
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Ordenes Pendientes Confirmacion")
        ws.clear()
        filas = [["Ticker", "Tipo Operacion", "Numero Operacion", "Fecha Intento",
                   "Tipo", "Acciones", "Precio Estimado", "Datos Extra"]]
        for o in ordenes:
            filas.append([
                o["ticker"], o["tipo_operacion"], str(o["numero_operacion"]), o["fecha_intento"],
                o.get("tipo", ""), o["acciones"], _num(o["precio_estimado"]),
                json.dumps(o.get("datos_extra", {})),
            ])
        ws.update(range_name="A1", values=filas)
    except Exception as e:
        print(f"[sheets] error al actualizar Ordenes Pendientes Confirmacion: {e}")
