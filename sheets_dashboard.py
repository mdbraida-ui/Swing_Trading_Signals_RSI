# -*- coding: utf-8 -*-
"""
============================================================================
 DASHBOARD GOOGLE SHEETS - Bot Swing RSI+Bollinger
============================================================================

Setup necesario (una sola vez):
  1. En Google Cloud Console (console.cloud.google.com):
     - Crear un proyecto (o usar uno existente)
     - Habilitar "Google Sheets API" y "Google Drive API"
     - Crear una Service Account -> Generar clave JSON (se descarga un
       archivo, ej. "credenciales_sheets.json")
  2. Crear una planilla nueva en Google Sheets, con 3 hojas (tabs):
       "Operaciones"        -> historial de cierres (una fila por trade)
       "Posiciones Abiertas" -> snapshot de lo que está abierto ahora
       "Resumen"            -> métricas agregadas (capital, retorno, etc.)
  3. Compartir la planilla con el email de la Service Account (algo
     como xxxx@xxxx.iam.gserviceaccount.com, está en el JSON descargado)
     dándole permiso de Editor.
  4. Guardar el JSON de credenciales como secret en GitHub Actions
     (como texto completo, ej. GOOGLE_SHEETS_CREDENTIALS_JSON) y el ID
     de la planilla (está en la URL: .../d/<ESTE_ID>/edit) como otro
     secret (GOOGLE_SHEETS_ID).

Columnas esperadas en cada hoja (crear los encabezados una sola vez a
mano en la planilla, en la fila 1):

  Operaciones: Fecha Entrada | Fecha Salida | Ticker | Precio Entrada |
               Precio Salida | Acciones | Motivo Salida | PnL $ | PnL % |
               Días Holding

  Posiciones Abiertas: Ticker | Fecha Entrada | Precio Entrada | Acciones |
                        Stop Vigente | Take Profit | Días en Posición

  Resumen: Fecha Actualización | Capital Inicial | Efectivo Disponible |
           Valor Posiciones Abiertas | Capital Total | Retorno % |
           Operaciones Totales | Win Rate %
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


def conectar_sheet():
    """
    Conecta con la planilla usando las credenciales de Service Account.
    Devuelve el objeto Spreadsheet de gspread, o None si falla (el bot
    no debería frenar sus operaciones por un problema del dashboard).
    """
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


def registrar_operacion_cerrada(sheet, fecha_entrada: str, fecha_salida: str,
                                 ticker: str, precio_entrada: float,
                                 precio_salida: float, acciones: int,
                                 motivo_salida: str, pnl_pesos: float,
                                 pnl_pct: float, dias_holding: int):
    """Agrega una fila al historial de operaciones cerradas."""
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Operaciones")
        ws.append_row([
            fecha_entrada, fecha_salida, ticker,
            round(precio_entrada, 2), round(precio_salida, 2), acciones,
            motivo_salida, round(pnl_pesos, 2), round(pnl_pct, 2), dias_holding,
        ])
    except Exception as e:
        print(f"[sheets] error al registrar operación: {e}")


def actualizar_posiciones_abiertas(sheet, posiciones: dict):
    """
    Reescribe por completo la hoja "Posiciones Abiertas" con el estado
    actual (se llama después de cada rutina).
    `posiciones`: dict ticker -> {fecha_entrada, precio_entrada, acciones,
    stop_vigente, stop_original, take_profit, dias_en_posicion}

    `stop_original` (además de `stop_vigente`) es necesario para poder
    distinguir un cierre por stop_loss real (nunca se movió el nivel) de
    un trailing_stop (el nivel ya subió al menos una vez) -- el cooldown
    de 3 días solo debe aplicar al primer caso.
    """
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Posiciones Abiertas")
        ws.clear()
        filas = [["Ticker", "Fecha Entrada", "Precio Entrada", "Acciones",
                   "Stop Original", "Stop Vigente", "Take Profit", "Días en Posición"]]
        for ticker, p in posiciones.items():
            stop_original = p.get("stop_original", p.get("stop_vigente", 0))
            filas.append([
                ticker, p.get("fecha_entrada", ""),
                round(p.get("precio_entrada", 0), 2), p.get("acciones", 0),
                round(stop_original, 2), round(p.get("stop_vigente", 0), 2),
                round(p.get("take_profit", 0), 2), p.get("dias_en_posicion", 0),
            ])
        ws.update(filas)
    except Exception as e:
        print(f"[sheets] error al actualizar posiciones abiertas: {e}")


def actualizar_resumen(sheet, capital_inicial: float, efectivo_disponible: float,
                        valor_posiciones_abiertas: float, operaciones_totales: int,
                        win_rate_pct: float):
    """Agrega una fila nueva al resumen (se llama al final de cada rutina,
    para tener una serie histórica de cómo evoluciona la cuenta día a día,
    no solo el último estado)."""
    if sheet is None:
        return
    try:
        ws = sheet.worksheet("Resumen")
        capital_total = efectivo_disponible + valor_posiciones_abiertas
        retorno_pct = 100 * (capital_total - capital_inicial) / capital_inicial if capital_inicial else 0
        ws.append_row([
            datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y %H:%M"),
            round(capital_inicial, 2), round(efectivo_disponible, 2),
            round(valor_posiciones_abiertas, 2), round(capital_total, 2),
            round(retorno_pct, 2), operaciones_totales, round(win_rate_pct, 2),
        ])
    except Exception as e:
        print(f"[sheets] error al actualizar resumen: {e}")
