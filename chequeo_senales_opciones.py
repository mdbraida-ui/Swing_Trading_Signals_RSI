# -*- coding: utf-8 -*-
"""
============================================================================
 CHEQUEO DE SEÑALES PARA OPCIONES -- ESCRIBE EN GOOGLE SHEETS
 Pensado para correr vía GitHub Actions + cron-job.org (mismo patrón
 que el bot diario de producción)
============================================================================

Corre UNA vez por invocación (no hace backtest histórico) -- chequea si
la señal de momentum por volumen se disparó en la ÚLTIMA vela de 1h
cerrada de GGAL.BA, y si es así, agrega una fila a la pestaña "Señales
de Opciones" de tu Google Sheet. Es informativo, no ejecuta ninguna
orden -- vos operás manualmente.

--------------------------------------------------------------------------
CÓMO INTEGRARLO A TU INFRAESTRUCTURA EXISTENTE
--------------------------------------------------------------------------
1) Agregá este archivo al repo (mismo lugar que bot_bb_touch_diario.py).
2) Sumá un job nuevo en tu workflow de GitHub Actions que corra este
   script -- mismo patrón que ya tenés para el bot diario, disparado
   por cron-job.org. Sugerencia de horario: cada 1h durante la rueda
   (11:00-17:00 ART), ya que la señal está pensada para velas de 1h.
3) Variables de entorno que necesita (agregalas como GitHub Secrets,
   igual que ya debés tener para el bot diario):
     - GOOGLE_SHEETS_CREDENTIALS_JSON: el JSON de la service account
       (el mismo que ya usás para `sheets_dashboard.py`, si es la
       misma cuenta de servicio).
     - GOOGLE_SHEET_ID: el ID de tu planilla (de la URL).
4) La pestaña "Señales de Opciones" se crea sola la primera vez que
   corre el script, con los encabezados -- no hace falta crearla a mano.

--------------------------------------------------------------------------
QUÉ CHEQUEA
--------------------------------------------------------------------------
Por ahora, solo momentum por volumen (la señal más validada del chat) --
`rompe_alcista`: Close rompe el máximo de las últimas 10 velas Y Volume
>= 1.5x su promedio de 20 velas. Se puede sumar el cruce EMA9/18 más
adelante con el mismo patrón (agregar otra función `chequear_*` y otra
llamada en `main()`).

Para evitar duplicar la misma señal en cada corrida mientras la
condición sigue siendo cierta en velas consecutivas (poco común con
este filtro, pero por las dudas), el script chequea si ya existe una
fila con la MISMA fecha_vela antes de escribir una nueva.
============================================================================
"""

import os
import json
import datetime
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

TICKER = "GGAL.BA"
N_MOMENTUM = 10
VENTANA_VOLUMEN = 20
MULTIPLICADOR_ENTRADA = 1.5
NOMBRE_PESTANA = "Señales de Opciones"
ENCABEZADOS = ["fecha_chequeo", "fecha_vela_senal", "ticker", "tipo_senal",
               "precio_close", "volumen", "volumen_promedio", "detalle"]


def conectar_sheets() -> gspread.Spreadsheet:
    """Usa las mismas credenciales de service account que el resto del
    proyecto -- via variable de entorno para no exponer el JSON en el
    repo (GitHub Secret)."""
    credenciales_json = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS_JSON"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credenciales = Credentials.from_service_account_info(credenciales_json, scopes=scopes)
    cliente = gspread.authorize(credenciales)
    return cliente.open_by_key(os.environ["GOOGLE_SHEETS_ID_OPCIONES"])


def obtener_o_crear_pestana(planilla: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        return planilla.worksheet(NOMBRE_PESTANA)
    except gspread.exceptions.WorksheetNotFound:
        pestana = planilla.add_worksheet(title=NOMBRE_PESTANA, rows=1000, cols=len(ENCABEZADOS))
        pestana.append_row(ENCABEZADOS)
        return pestana


def descargar_datos_recientes(ticker: str = TICKER, periodo: str = "30d") -> pd.DataFrame:
    """30 días de 1h alcanza de sobra para los rollings de N_MOMENTUM=10
    y VENTANA_VOLUMEN=20 -- no hace falta bajar los 730 días completos
    para un chequeo puntual de la última vela."""
    df = yf.download(ticker, period=periodo, interval="1h", progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"Sin datos 1h para {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def chequear_momentum_volumen(df: pd.DataFrame) -> dict:
    """Evalúa la condición SOLO en la última vela disponible -- misma
    lógica que `momentum_volumen_ggal.py`, pero sin recorrer todo el
    histórico (no hace falta para un chequeo en vivo)."""
    df = df.copy()
    df["rango_max"] = df["High"].rolling(N_MOMENTUM).max().shift(1)
    df["volumen_promedio"] = df["Volume"].rolling(VENTANA_VOLUMEN).mean().shift(1)

    ultima = df.iloc[-1]
    fecha_vela = df.index[-1]

    if pd.isna(ultima["rango_max"]) or pd.isna(ultima["volumen_promedio"]):
        return {"disparo": False, "motivo": "sin historia suficiente todavía"}

    volumen_confirma = ultima["Volume"] >= MULTIPLICADOR_ENTRADA * ultima["volumen_promedio"]
    rompe_alcista = (ultima["Close"] > ultima["rango_max"]) and volumen_confirma

    return {
        "disparo": bool(rompe_alcista),
        "fecha_vela": fecha_vela,
        "precio_close": float(ultima["Close"]),
        "volumen": float(ultima["Volume"]),
        "volumen_promedio": float(ultima["volumen_promedio"]),
        "rango_max": float(ultima["rango_max"]),
    }


def ya_existe_fila(pestana: gspread.Worksheet, fecha_vela_str: str, ticker: str) -> bool:
    """Evita duplicar la misma señal si el job corre más de una vez
    para la misma vela cerrada."""
    valores = pestana.get_all_values()
    for fila in valores[1:]:  # saltea el encabezado
        if len(fila) >= 3 and fila[1] == fecha_vela_str and fila[2] == ticker:
            return True
    return False


def main():
    df = descargar_datos_recientes()
    resultado = chequear_momentum_volumen(df)

    if not resultado.get("disparo"):
        print(f"Sin señal en la última vela ({resultado.get('fecha_vela', 'sin datos')}).")
        return

    fecha_vela_str = str(resultado["fecha_vela"])
    planilla = conectar_sheets()
    pestana = obtener_o_crear_pestana(planilla)

    if ya_existe_fila(pestana, fecha_vela_str, TICKER):
        print(f"Señal ya registrada para {fecha_vela_str}, no se duplica.")
        return

    fila = [
        str(datetime.datetime.now()),
        fecha_vela_str,
        TICKER,
        "momentum_volumen_CALL",
        round(resultado["precio_close"], 2),
        round(resultado["volumen"], 0),
        round(resultado["volumen_promedio"], 0),
        f"rompió máximo de {N_MOMENTUM} velas ({resultado['rango_max']:.2f}) con volumen "
        f"{resultado['volumen']/resultado['volumen_promedio']:.1f}x su promedio",
    ]
    pestana.append_row(fila)
    print(f"Señal registrada: {fila}")

    # Combina esta señal con el último IV Rank disponible y escribe la
    # recomendación en el Panel de Decisión -- ajustar `bucket_iv` al
    # bucket que corresponda seguir para este tipo de señal.
    actualizar_panel_decision(planilla, TICKER, senal_direccional="alcista", bucket_iv="CALL_ATM_30D")


if __name__ == "__main__":
    main()

# ============================================================================
# PANEL DE DECISIÓN -- combina señal direccional + IV Rank en una recomendación
# ============================================================================

NOMBRE_PESTANA_PANEL = "Panel de Decisión"
ENCABEZADOS_PANEL = ["fecha", "ticker", "senal_direccional", "bucket_iv_consultado",
                      "iv_rank_pct", "recomendacion", "detalle"]


def leer_ultimo_iv_rank(planilla, bucket: str):
    """Lee la última fila registrada para un bucket en 'Historial IV' y
    devuelve su iv_rank_pct (o None si no hay historial suficiente
    todavía, o la pestaña no existe aún)."""
    try:
        pestana_iv = planilla.worksheet("Historial IV")
    except gspread.exceptions.WorksheetNotFound:
        return None
    valores = pestana_iv.get_all_values()
    for fila in reversed(valores[1:]):  # de la más reciente hacia atrás
        if len(fila) >= 14 and fila[1] == bucket:
            try:
                return float(fila[13])
            except ValueError:
                return None
    return None


def recomendar_estructura(senal_direccional: str, iv_rank_pct) -> dict:
    """
    Aplica la matriz de decisión: dirección x IV Rank -> estructura
    sugerida. `iv_rank_pct` puede ser None (sin historial suficiente
    todavía) -- en ese caso se recomienda con menos confianza,
    aclarándolo en el detalle.
    """
    if iv_rank_pct is None:
        return {
            "recomendacion": f"Bull/Bear spread simple (1x1) -- {senal_direccional}",
            "detalle": "Sin historial de IV Rank todavía (necesita ~10 observaciones). "
                        "No se recomienda ratio spread sin ese dato -- default a spread 1x1.",
        }

    iv_cara = iv_rank_pct >= 60  # umbral provisorio, recalibrar con datos propios

    if senal_direccional == "alcista":
        if iv_cara:
            return {"recomendacion": "Bull call spread 1x1 (o ratio 1x2 si hay alta convicción de zona objetivo)",
                    "detalle": f"IV Rank={iv_rank_pct:.0f}% (cara) -- la prima que se cobra en la pata corta "
                                f"vale más, el spread sale más barato neto."}
        return {"recomendacion": "Bull call spread 1x1",
                "detalle": f"IV Rank={iv_rank_pct:.0f}% (normal/barata) -- sin ventaja extra por vender prima cara."}

    if senal_direccional == "bajista":
        if iv_cara:
            return {"recomendacion": "Bear put spread 1x1 (o ratio 1x2 si hay alta convicción de zona objetivo)",
                    "detalle": f"IV Rank={iv_rank_pct:.0f}% (cara)."}
        return {"recomendacion": "Bear put spread 1x1",
                "detalle": f"IV Rank={iv_rank_pct:.0f}% (normal/barata)."}

    # sin señal direccional clara
    if iv_cara:
        return {"recomendacion": "Vender prima sin direccional (strangle/iron condor en TM Inversiones)",
                "detalle": f"IV Rank={iv_rank_pct:.0f}% (cara) y sin señal de dirección -- "
                            f"candidato a cosechar VRP puro."}
    return {"recomendacion": "No operar",
            "detalle": f"IV Rank={iv_rank_pct:.0f}% (normal/barata) y sin señal de dirección -- sin ventaja de ningún lado."}


def actualizar_panel_decision(planilla, ticker: str, senal_direccional: str, bucket_iv: str):
    try:
        pestana_panel = planilla.worksheet(NOMBRE_PESTANA_PANEL)
    except gspread.exceptions.WorksheetNotFound:
        pestana_panel = planilla.add_worksheet(title=NOMBRE_PESTANA_PANEL, rows=1000, cols=len(ENCABEZADOS_PANEL))
        pestana_panel.append_row(ENCABEZADOS_PANEL)

    iv_rank = leer_ultimo_iv_rank(planilla, bucket_iv)
    resultado = recomendar_estructura(senal_direccional, iv_rank)

    fila = [
        str(datetime.datetime.now()),
        ticker,
        senal_direccional,
        bucket_iv,
        round(iv_rank, 1) if iv_rank is not None else "sin datos",
        resultado["recomendacion"],
        resultado["detalle"],
    ]
    pestana_panel.append_row(fila)
    print(f"Panel de Decisión actualizado: {fila}")
