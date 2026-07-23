# -*- coding: utf-8 -*-
"""
============================================================================
 ESTRATEGIA BB-TOUCH + BBW + EMA50 -- Motor de señales y costos (uso en vivo)
============================================================================

Módulo AUTOCONTENIDO: no depende de swing_pullback_ema21.py (que queda
asociado al bot RSI/BB anterior, dado de baja). Duplica acá las piezas de
infraestructura que sí siguen siendo necesarias (descarga de datos,
indicadores, modelo de costos de IOL) para que el nuevo bot no dependa de
un archivo que se está retirando.

--------------------------------------------------------------------------
REGLAS DE LA ESTRATEGIA (idénticas a las validadas en backtest v6)
--------------------------------------------------------------------------
  1) Señal pendiente: el precio toca la banda inferior de Bollinger
     (Low <= BB_lower). Queda viva sin límite de tiempo hasta que el BBW
     = (BB_upper - BB_lower)/BB_mid supere `BBW_UMBRAL` (0.200).
  2) Entrada: el mismo día que se confirma la señal, aproximada en vivo
     con el precio intradía de la ventana de cierre (~16:30-16:50) --
     ver bot_bb_touch_diario.py, rutina_cierre.
  3) Sizing: capital objetivo directo, hasta `TOPE_MAXIMO_POSICION`
     (o el efectivo remanente si es menor), incluyendo comisión+IVA+
     derechos de mercado dentro del monto. Reparto en orden de la
     planilla si hay más de una señal el mismo día.
  4) SL inicial (Fase A): 10% bajo el precio de entrada, chequeado
     intradía contra el precio en vivo (rutina_monitoreo_fase_a, cada
     10 min).
  5) Fase B: una vez que el precio CIERRA por encima de la EMA50, el SL
     dejar de regir -- única salida es un cierre por debajo de la EMA50.
     Protección de día de transición: el día que recién cruza no se
     evalúa salida ese mismo día.
  6) Cooldown de 3 días tras un stop_loss genuino (Fase A) antes de
     poder reentrar en el mismo ticker.
============================================================================
"""

import csv
import numpy as np
import pandas as pd
import yfinance as yf
import ta

# --- Parámetros de la estrategia (producción) ---
BBW_UMBRAL = 0.200
SL_INICIAL_PCT = 0.10
COOLDOWN_DIAS = 3
TOPE_MAXIMO_POSICION = 100_000.0

# --- Modelo de costos IOL ---
COMISION_COMPRA_PCT = 0.005    # 0.5%
IVA_PCT = 0.21                 # 21% sobre comisión y derechos
DERECHOS_MERCADO_PCT = 0.0005  # 0.05%


def costo_compra(monto: float) -> float:
    return monto * COMISION_COMPRA_PCT * (1 + IVA_PCT) + monto * DERECHOS_MERCADO_PCT * (1 + IVA_PCT)


def costo_venta(monto: float, intradia: bool = False) -> float:
    """Las ventas intradía (compra y venta el mismo día) tienen la
    comisión de compra-venta bonificada en IOL -- solo se cobran
    derechos de mercado. Ventas de posiciones que no son intradía pagan
    el mismo esquema que la compra."""
    if intradia:
        return monto * DERECHOS_MERCADO_PCT * (1 + IVA_PCT)
    return monto * COMISION_COMPRA_PCT * (1 + IVA_PCT) + monto * DERECHOS_MERCADO_PCT * (1 + IVA_PCT)


def costo_total_roundtrip(monto_entrada: float, monto_salida: float) -> float:
    return costo_compra(monto_entrada) + costo_venta(monto_salida, intradia=False)


# --- Factor de comisión de compra, usado en el sizing por capital objetivo ---
FACTOR_COMISION_COMPRA = COMISION_COMPRA_PCT * (1 + IVA_PCT) + DERECHOS_MERCADO_PCT * (1 + IVA_PCT)


def leer_planilla_tickers(ruta_csv: str, solo_activos: bool = True) -> list:
    """
    Lee la planilla de universo de tickers desde el repo (columnas:
    ticker, tipo, activo, notas). El ORDEN de las filas define la
    prioridad de reparto de efectivo cuando hay señales simultáneas.

    `tipo` puede ser: merval_lider, merval_general, cedear -- informativo
    para el dashboard, el motor de capital compartido no distingue entre
    ellas (pool único, ver Punto 5 de la memoria del proyecto).

    Devuelve una lista de dicts, en el mismo orden del archivo.
    """
    filas = []
    with open(ruta_csv, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        for fila in lector:
            ticker = fila["ticker"].strip().upper()
            tipo = fila["tipo"].strip().lower()
            activo = fila["activo"].strip().upper()
            if solo_activos and activo != "SI":
                continue
            filas.append({"ticker": ticker, "tipo": tipo, "activo": activo,
                           "notas": fila.get("notas", "")})
    return filas


def descargar_datos_diarios(ticker: str, periodo: str = "6mo", limpiar_anomalias: bool = True) -> pd.DataFrame:
    """
    Descarga velas diarias de Yahoo Finance. Aplana columnas
    multi-índice (problema conocido de yfinance con un solo ticker) y,
    si `limpiar_anomalias`, reemplaza saltos de precio >30% en un día
    por el último valor válido (protege contra errores de datos tipo
    el de ECOG.BA/TECO2.BA ya documentados -- NO aplica a CEDEARs,
    donde saltos sincronizados son movimientos de tipo de cambio
    genuinos, no errores).
    """
    df = yf.download(ticker, period=periodo, progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"Sin datos para {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    if limpiar_anomalias:
        variacion = df["Close"].pct_change()
        anomalias = variacion.abs() > 0.30
        if anomalias.any():
            for fecha in df.index[anomalias]:
                print(f"[ANOMALÍA {ticker}] {fecha.date()}: variación diaria de "
                      f"{100*variacion.loc[fecha]:.1f}%. Se reemplaza por el último valor válido.")
            for col in ["Open", "High", "Low", "Close"]:
                df.loc[anomalias, col] = np.nan
            df[["Open", "High", "Low", "Close"]] = df[["Open", "High", "Low", "Close"]].ffill()
    return df


def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    """EMA21/50, Bandas de Bollinger (20,2) y RSI14. EMA21 se deja
    calculada aunque esta estrategia solo usa EMA50, por si en algún
    momento se quiere comparar contra el otro motor del proyecto."""
    df = df.copy()
    close = df["Close"]
    df["EMA21"] = ta.trend.EMAIndicator(close, window=21).ema_indicator()
    df["EMA50"] = ta.trend.EMAIndicator(close, window=50).ema_indicator()
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_lower"] = bb.bollinger_lband()
    df["BB_mid"] = bb.bollinger_mavg()
    df["BB_upper"] = bb.bollinger_hband()
    df["BBW"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
    df["RSI14"] = ta.momentum.RSIIndicator(close, window=14).rsi()
    return df


def generar_senales_bb_touch_bbw(df: pd.DataFrame, bbw_umbral: float = BBW_UMBRAL) -> pd.DataFrame:
    """
    Reconstruye el estado "pendiente" desde el arranque de la serie
    descargada hasta HOY -- en vivo no hay estado persistente entre
    corridas, así que cada vez que se llama esta función se recalcula
    todo desde cero con el historial completo (igual que el backtest).
    Sin límite de tiempo para la señal pendiente (ver "CAMBIOS EN v6"
    del backtest -- se probó un límite de 6 velas y se descartó).
    """
    df = df.copy()
    toca_banda_baja = df["Low"] <= df["BB_lower"]

    senal = pd.Series(False, index=df.index)
    pendiente = False
    for i in range(len(df)):
        if toca_banda_baja.iloc[i]:
            pendiente = True
        if pendiente and df["BBW"].iloc[i] > bbw_umbral:
            senal.iloc[i] = True
            pendiente = False

    df["senal_confirmada"] = senal
    return df


def forzar_cierre_de_hoy(df: pd.DataFrame, precio_en_vivo: float, fecha_hoy) -> pd.DataFrame:
    """
    Yahoo Finance consolida la vela diaria recién después del cierre real
    (típicamente varias horas después de las 17hs ART) -- confiar en
    `df["Close"].iloc[-1]` durante la rueda significa, la mayoría de las
    veces, seguir mirando el cierre de AYER sin darse cuenta.

    Esta función fuerza el precio de HOY con el dato en vivo de IOL
    (`precio_en_vivo`, vía obtener_precio()) antes de recalcular
    indicadores -- así "cierre_hoy" en rutina_cierre() es de verdad el
    precio de la ventana 16:30, sea que Yahoo ya haya publicado la vela
    de hoy (se sobreescribe) o no (se agrega una fila nueva).
    """
    df = df.copy()
    if len(df) and df.index[-1].date() == fecha_hoy:
        df.loc[df.index[-1], "Close"] = precio_en_vivo
        df.loc[df.index[-1], "High"] = max(df["High"].iloc[-1], precio_en_vivo)
        df.loc[df.index[-1], "Low"] = min(df["Low"].iloc[-1], precio_en_vivo)
    else:
        nueva_fila = pd.DataFrame(
            {"Open": [precio_en_vivo], "High": [precio_en_vivo],
             "Low": [precio_en_vivo], "Close": [precio_en_vivo],
             "Volume": [0]},
            index=[pd.Timestamp(fecha_hoy)],
        )
        df = pd.concat([df, nueva_fila])
    return df


def calcular_acciones_por_capital_objetivo(efectivo_disponible: float, precio: float,
                                            tope: float = TOPE_MAXIMO_POSICION) -> int:
    """
    Sizing v4/v6: apunta a `tope` (o el efectivo remanente si es menor),
    incluyendo comisión+IVA+derechos dentro del monto. Devuelve la
    cantidad de acciones (puede ser 0 si no alcanza ni para 1).
    """
    if precio <= 0 or efectivo_disponible <= 0:
        return 0
    monto_objetivo = min(efectivo_disponible, tope)
    acciones = int(monto_objetivo / (precio * (1 + FACTOR_COMISION_COMPRA)))
    # red de seguridad por redondeo, igual que en el backtest
    while acciones > 0:
        monto_entrada = precio * acciones
        costos = costo_compra(monto_entrada)
        if (monto_entrada + costos) <= efectivo_disponible:
            break
        acciones -= 1
    return max(acciones, 0)


def calcular_pnl(precio_entrada: float, precio_salida: float, cantidad: int) -> tuple:
    monto_entrada = precio_entrada * cantidad
    monto_salida = precio_salida * cantidad
    costos = costo_total_roundtrip(monto_entrada, monto_salida)
    pnl_pesos = (monto_salida - monto_entrada) - costos
    pnl_pct = 100 * pnl_pesos / monto_entrada if monto_entrada else 0.0
    return pnl_pesos, pnl_pct
