# -*- coding: utf-8 -*-
"""
============================================================================
 DASHBOARD GOOGLE SHEETS -- Bot BB-Touch + BBW + EMA50
============================================================================

Setup necesario (una sola vez):
  1. En Google Cloud Console: habilitar "Google Sheets API" y "Google
     Drive API", crear una Service Account, generar clave JSON.
  2. Crear una planilla nueva en Google Sheets con estas 7 hojas (tabs),
     con los encabezados de la fila 1 EXACTAMENTE como se listan abajo
     (se crean a mano una sola vez; el bot escribe desde la fila 2):

  "Operaciones Activas":
      Ticker | Tipo | Fecha Entrada | Precio Entrada | Acciones |
      Precio Actual | Fase | Stop Vigente | SL Fase A ($) | P&L $ |
      P&L % | Días en Posición

  "Historico Ordenes TOTAL":
      Ticker | Tipo | Fecha Entrada | Precio Entrada | Fecha Salida |
      Precio Salida | Acciones | SL Fase A ($) | Motivo Salida | P&L $ |
      P&L % | Días Holding

  "P&L Total":
      Fecha | Capital Inicial | Efectivo | Valor Posiciones |
      Capital Total | Retorno % | Operaciones Totales | Win Rate % |
      Max Drawdown %

  "Historico CEDEAR", "Historico Merval Lider", "Historico Merval General":
      mismas columnas que "Historico Ordenes TOTAL" -- cada cierre se
      escribe en TOTAL y, además, en la hoja de su categoría (`tipo` del
      ticker en tickers_activos.csv), para tener el desglose sin
      depender de fórmulas QUERY/FILTER frágiles ante ediciones manuales.

  "Indicadores":
      Ticker | Tipo | Fecha | Precio Actual | RSI14 | BB Inferior |
      BB Media | BB Superior | BBW | EMA50 | Señal Pendiente |
      Señal Confirmada | En Cooldown

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
from datetime import datetime
from zoneinfo import ZoneInfo

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

GOOGLE_SHEETS_ID = os.environ.get("GOOGLE_SHEETS_ID", "")
GOOGLE_SHEETS_CREDENTIALS_JSON = os.environ.get("GOOGLE_SHEETS_CREDENTIALS_JSON", "")
TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")

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
                round(precio_entrada, 2), acciones, round(precio_actual, 2),
                p.get("fase", ""), round(p.get("stop_vigente", 0), 2),
                round(p.get("sl_fase_a", 0), 2),
                round(pnl_pesos, 2), round(pnl_pct, 2),
                p.get("dias_en_posicion", 0),
            ])
        ws.update(filas)
    except Exception as e:
        print(f"[sheets] error al actualizar Operaciones Activas: {e}")


# ============================================================================
# 2) HISTORICO ORDENES TOTAL + desglose por tipo (4/5/6) -- se agrega fila
# ============================================================================
def registrar_operacion_cerrada(sheet, ticker: str, tipo: str, fecha_entrada: str,
                                 precio_entrada: float, fecha_salida: str,
                                 precio_salida: float, acciones: int,
                                 sl_fase_a: float, motivo_salida: str,
                                 pnl_pesos: float, pnl_pct: float, dias_holding: int):
    """Agrega la operación cerrada en 'Historico Ordenes TOTAL' y, además,
    en la hoja de su categoría (CEDEAR / Merval Lider / Merval General)."""
    if sheet is None:
        return
    fila = [
        ticker, tipo, fecha_entrada, round(precio_entrada, 2), fecha_salida,
        round(precio_salida, 2), acciones, round(sl_fase_a, 2), motivo_salida,
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
        filas = ws.get_all_records()
    except Exception:
        return False
    if not filas:
        return False
    return str(filas[-1].get("Fecha", "")).startswith(_hoy_str())


def actualizar_dashboard_pnl(sheet, capital_inicial: float, efectivo: float,
                              valor_posiciones: float, operaciones_totales: int,
                              win_rate_pct: float, max_drawdown_pct: float):
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
            round(max_drawdown_pct, 2),
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
        filas = ws.get_all_records()
    except Exception:
        return False
    if not filas:
        return False
    fecha_hoy = datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y")
    return str(filas[0].get("Fecha", "")).startswith(fecha_hoy)


def actualizar_indicadores(sheet, indicadores: dict):
    """
    `indicadores`: dict ticker -> {
        "tipo": str, "precio_actual": float, "rsi14": float,
        "bb_lower": float, "bb_mid": float, "bb_upper": float,
        "bbw": float, "ema50": float, "senal_pendiente": bool,
        "senal_confirmada": bool, "en_cooldown": bool,
    }
    """
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Indicadores")
        ws.clear()
        filas = [["Ticker", "Tipo", "Fecha", "Precio Actual", "RSI14",
                   "BB Inferior", "BB Media", "BB Superior", "BBW", "EMA50",
                   "Señal Pendiente", "Señal Confirmada", "En Cooldown"]]
        ahora = _ahora_str()
        for ticker, ind in sorted(indicadores.items()):
            filas.append([
                ticker, ind.get("tipo", ""), ahora,
                round(ind.get("precio_actual", 0), 2), round(ind.get("rsi14", 0), 2),
                round(ind.get("bb_lower", 0), 2), round(ind.get("bb_mid", 0), 2),
                round(ind.get("bb_upper", 0), 2), round(ind.get("bbw", 0), 3),
                round(ind.get("ema50", 0), 2),
                "SI" if ind.get("senal_pendiente") else "no",
                "SI" if ind.get("senal_confirmada") else "no",
                "SI" if ind.get("en_cooldown") else "no",
            ])
        ws.update(filas)
    except Exception as e:
        print(f"[sheets] error al actualizar Indicadores: {e}")
