# -*- coding: utf-8 -*-
"""
============================================================================
 SWING TRADING - ESTRATEGIA "PULLBACK A EMA21"
 Complemento del bot intraday IOL (RSI + Bollinger, 15 min)
============================================================================

Diseñado para Google Colab. Cada bloque separado por "# %%" puede pegarse
en una celda distinta de un notebook.

Objetivo:
  - Operar con capital concentrado (1-2 posiciones) y horizonte de 3-10
    días, para que las comisiones de IOL no se coman el margen que sí
    afectan al intraday con capital chico ($100.000).

Contenido:
  1. Modelo de comisiones IOL (compra / venta, bonificación intradía)
  2. Descarga de datos diarios (Yahoo Finance, sufijo .BA)
  3. Indicadores: EMA21, EMA50, ATR14, RSI14 (vía librería `ta`)
  4. Detección de velas de reversión (martillo / envolvente alcista)
  5. Señal "Pullback a EMA21"
  6. Filtro opcional de tendencia del dólar CCL (proxy vía ratio de ADR)
  7. Motor de backtesting (position sizing fijo 2% y dinámico por ATR,
     trailing stop por EMA21, TP por resistencia o 2R, comisiones reales)
  8. Corrida sobre LEDE, CELU, IRSA, OEST, GBAN + comparación con/sin CCL
  9. Clase `SwingStrategy` para integrar al bot existente (conviviendo con
     el motor intraday, usando un pool de capital separado)
============================================================================
"""

# %% [1] IMPORTS Y CONFIGURACIÓN
# ---------------------------------------------------------------------------
import time
import numpy as np
import pandas as pd
import yfinance as yf
import ta
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import warnings
warnings.filterwarnings("ignore")

pd.set_option("display.float_format", lambda x: f"{x:,.2f}")

# --- Universo swing original (los 5 mejores del backtesting intraday) ---
TICKERS_SWING_TOP5 = ["LEDE.BA", "CELU.BA", "IRSA.BA", "OEST.BA", "GBAN.BA"]

# --- Universo ampliado: Panel Líder Merval (pedido por el usuario) ---
TICKERS_PANEL_LIDER = [
    "GGAL.BA", "YPFD.BA", "PAMP.BA", "BMA.BA", "LOMA.BA", "TRAN.BA",
    "TGSU2.BA", "BBAR.BA", "CEPU.BA", "VALO.BA", "SUPV.BA", "BYMA.BA",
    "TXAR.BA", "METR.BA", "EDN.BA", "COME.BA", "ALUA.BA", "CRES.BA",
    "IRSA.BA", "TGNO4.BA", "TECO2.BA", "ECOG.BA",
]

# Universo activo por defecto para correr el backtest (cambiar aquí para
# volver al top-5 original o combinar ambas listas)
TICKERS_SWING = TICKERS_PANEL_LIDER

# --- Parámetros generales ---
CAPITAL_INICIAL = 100_000.0        # capital total de la cuenta IOL
CAPITAL_SWING_PCT = 0.70           # % del capital que se reserva para swing
                                    # (el resto queda disponible para intraday)
RIESGO_POR_OPERACION = 0.02        # 2% del capital swing arriesgado por trade
MAX_DIAS_HOLDING = 10
MIN_DIAS_HOLDING = 3               # no se sale antes salvo por SL
RR_MINIMO = 2.0                    # TP = 2x riesgo si no hay resistencia clara

# --- Comisiones IOL (idénticas a las usadas en el bot intraday) ---
COMISION_COMPRA_PCT = 0.005        # 0.5%
IVA_PCT = 0.21                     # 21% sobre la comisión y los derechos
DERECHOS_MERCADO_PCT = 0.0005      # 0.05%, siempre, ambas puntas, sin bonificar


# %% [2] MODELO DE COMISIONES
# ---------------------------------------------------------------------------
def costo_compra(monto: float) -> float:
    """Costo total de la punta compradora: comisión + IVA + derechos + IVA."""
    comision = monto * COMISION_COMPRA_PCT
    comision_iva = comision * IVA_PCT
    derechos = monto * DERECHOS_MERCADO_PCT
    derechos_iva = derechos * IVA_PCT
    return comision + comision_iva + derechos + derechos_iva


def costo_venta(monto: float, intradia: bool) -> float:
    """
    Costo total de la punta vendedora.
    En swing (holding >= 1 día hábil) la venta NUNCA es intradía,
    por lo tanto la comisión NO se bonifica. Los derechos de mercado
    tampoco se bonifican nunca (ni en intraday ni en swing).
    """
    if intradia:
        comision = 0.0  # bonificada
        comision_iva = 0.0
    else:
        comision = monto * COMISION_COMPRA_PCT
        comision_iva = comision * IVA_PCT
    derechos = monto * DERECHOS_MERCADO_PCT
    derechos_iva = derechos * IVA_PCT
    return comision + comision_iva + derechos + derechos_iva


def costo_total_roundtrip(monto_entrada: float, monto_salida: float) -> float:
    """Costo total de comprar y vender una posición swing (nunca intradía)."""
    return costo_compra(monto_entrada) + costo_venta(monto_salida, intradia=False)


# %% [3] DESCARGA DE DATOS DIARIOS (YAHOO FINANCE)
# ---------------------------------------------------------------------------
def limpiar_datos_diarios(
    df: pd.DataFrame, ticker: str = "",
    max_variacion_diaria: float = 0.40,
) -> pd.DataFrame:
    """
    Detecta y corrige saltos de precio anómalos en el Close diario.

    Motivación: Yahoo Finance tiene errores de datos conocidos en
    tickers .BA de menor liquidez (un tick puntual con un precio
    absurdo, o un split/ajuste de capital mal aplicado en la serie
    ajustada). Sin este filtro, un solo día con un dato corrupto puede
    generar un PnL de cientos de miles de pesos en una sola operación
    y arruinar todas las métricas agregadas de ese ticker (esto es
    exactamente lo que pasó con YPFD.BA: un pnl_neto_total de ~9
    millones, imposible para 3-10 días de holding con el capital y
    sizing usados).

    Un salto de +40%/-40% en un solo día es fisicamente posible en
    Merval en escenarios extremos (ej. anuncios de deuda, PASO), así
    que este filtro es deliberadamente conservador: solo corrige, no
    descarta el ticker entero, y siempre imprime qué corrigió para que
    se pueda auditar a mano si fue un error real de mercado o un bug
    de datos.
    """
    df = df.copy()
    retorno_diario = df["Close"].pct_change()
    anomalos = retorno_diario.abs() > max_variacion_diaria

    if anomalos.any():
        for fecha in df.index[anomalos]:
            print(
                f"[ANOMALÍA {ticker}] {fecha.date()}: variación diaria de "
                f"{retorno_diario.loc[fecha]*100:.1f}% frente al día anterior. "
                f"Se reemplaza por el último valor válido (revisar a mano "
                f"si fue un split/evento real o un error de Yahoo Finance)."
            )
        for col in ["Open", "High", "Low", "Close"]:
            df.loc[anomalos, col] = np.nan
        df[["Open", "High", "Low", "Close"]] = (
            df[["Open", "High", "Low", "Close"]].ffill()
        )

    return df


def descargar_datos_diarios(ticker: str, periodo: str = "3y",
                             limpiar_anomalias: bool = True) -> pd.DataFrame:
    """
    Descarga velas diarias desde Yahoo Finance para un ticker .BA.
    Devuelve columnas: Open, High, Low, Close, Volume (índice = fecha).
    """
    df = yf.download(ticker, period=periodo, interval="1d", progress=False,
                      auto_adjust=True)
    if df.empty:
        raise ValueError(f"No se pudieron descargar datos para {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df.rename(columns=str.title)
    df = df.dropna()
    if limpiar_anomalias:
        df = limpiar_datos_diarios(df, ticker=ticker)
    return df


# %% [4] INDICADORES TÉCNICOS
# ---------------------------------------------------------------------------
def calcular_indicadores(df: pd.DataFrame) -> pd.DataFrame:
    """Agrega EMA10, EMA20, EMA21, EMA50, ATR14, RSI14 y Bandas de
    Bollinger (20,2) al dataframe de precios diarios."""
    df = df.copy()
    df["EMA10"] = ta.trend.EMAIndicator(df["Close"], window=10).ema_indicator()
    df["EMA20"] = ta.trend.EMAIndicator(df["Close"], window=20).ema_indicator()
    df["EMA21"] = ta.trend.EMAIndicator(df["Close"], window=21).ema_indicator()
    df["EMA50"] = ta.trend.EMAIndicator(df["Close"], window=50).ema_indicator()
    df["ATR14"] = ta.volatility.AverageTrueRange(
        df["High"], df["Low"], df["Close"], window=14
    ).average_true_range()
    df["RSI14"] = ta.momentum.RSIIndicator(df["Close"], window=14).rsi()
    bb = ta.volatility.BollingerBands(df["Close"], window=20, window_dev=2)
    df["BB_upper"] = bb.bollinger_hband()
    df["BB_mid"] = bb.bollinger_mavg()
    df["BB_lower"] = bb.bollinger_lband()
    return df


# %% [5] DETECCIÓN DE VELAS DE REVERSIÓN
# ---------------------------------------------------------------------------
def es_martillo(o, h, l, c, ratio_mecha=1.5) -> bool:
    """
    Martillo alcista: cuerpo pequeño en la parte superior del rango,
    mecha inferior larga (al menos `ratio_mecha` veces el cuerpo),
    mecha superior corta.

    NOTA: el umbral original (ratio_mecha=2.0) es el "libro de texto"
    pero en la práctica sobre datos diarios argentinos genera muy pocas
    activaciones. Se bajó el default a 1.5, que sigue siendo un martillo
    reconocible pero no exige una mecha excepcionalmente larga.
    """
    cuerpo = abs(c - o)
    rango = h - l
    if rango <= 0:
        return False
    mecha_inferior = min(o, c) - l
    mecha_superior = h - max(o, c)
    if cuerpo == 0:
        cuerpo = rango * 0.01  # evita división por cero en doji perfecto
    return (mecha_inferior >= ratio_mecha * cuerpo) and (mecha_superior <= cuerpo * 0.8)


def es_envolvente_alcista(o_prev, c_prev, o, c, envolvente_parcial_pct=0.0) -> bool:
    """
    Envolvente alcista: vela previa bajista, vela actual alcista cuyo
    cuerpo envuelve el cuerpo de la vela previa.

    `envolvente_parcial_pct` permite relajar la exigencia de envolver
    el 100% del cuerpo previo (ej. 0.15 acepta que el cuerpo actual
    cubra al menos 85% del rango de la vela previa). En 0.0 exige
    envolvente estricta (comportamiento original).
    """
    prev_bajista = c_prev < o_prev
    actual_alcista = c > o
    margen = abs(o_prev - c_prev) * envolvente_parcial_pct
    envuelve = (o <= c_prev + margen) and (c >= o_prev - margen)
    return prev_bajista and actual_alcista and envuelve


def es_vela_alcista_fuerte(o, h, l, c, pct_cierre_superior=0.65) -> bool:
    """
    Patrón adicional, más laxo que martillo/envolvente: una vela
    simplemente alcista y decidida, que cierra en el tramo superior de
    su propio rango (ej. arriba del 65% del rango), sin mecha superior
    dominante. Sirve para no perder rebotes válidos que no forman un
    martillo ni una envolvente "de libro" pero igual muestran presión
    compradora clara tras el pullback.
    """
    rango = h - l
    if rango <= 0:
        return False
    if c <= o:
        return False
    posicion_cierre = (c - l) / rango
    return posicion_cierre >= pct_cierre_superior


def detectar_vela_reversion(df: pd.DataFrame, ratio_mecha: float = 1.5,
                             envolvente_parcial_pct: float = 0.15,
                             incluir_vela_fuerte: bool = True) -> pd.Series:
    """
    Devuelve una Serie booleana: True en los días donde se confirma
    martillo, envolvente alcista (estricta o parcial), o -si
    `incluir_vela_fuerte`- una vela alcista fuerte que cierra en el
    tramo superior de su rango.
    """
    resultado = pd.Series(False, index=df.index)
    o, h, l, c = df["Open"], df["High"], df["Low"], df["Close"]
    for i in range(1, len(df)):
        martillo = es_martillo(o.iloc[i], h.iloc[i], l.iloc[i], c.iloc[i],
                                ratio_mecha=ratio_mecha)
        envolvente = es_envolvente_alcista(
            o.iloc[i - 1], c.iloc[i - 1], o.iloc[i], c.iloc[i],
            envolvente_parcial_pct=envolvente_parcial_pct
        )
        fuerte = incluir_vela_fuerte and es_vela_alcista_fuerte(
            o.iloc[i], h.iloc[i], l.iloc[i], c.iloc[i]
        )
        resultado.iloc[i] = bool(martillo or envolvente or fuerte)
    return resultado


# %% [6] SEÑAL "PULLBACK A EMA21"
# ---------------------------------------------------------------------------
def generar_senales_pullback_ema21(
    df: pd.DataFrame,
    tolerancia_pct: float = 0.025,
    ventana_confirmacion: int = 3,
    ratio_mecha: float = 1.5,
    envolvente_parcial_pct: float = 0.15,
    incluir_vela_fuerte: bool = True,
) -> pd.DataFrame:
    """
    Marca los días de señal según las reglas:
      1. Tendencia alcista: Close > EMA50
      2. Pullback: el precio tocó la EMA21 en el día actual O en
         cualquiera de los `ventana_confirmacion` días previos
         (por defecto 3). Esto es la relajación clave: en la versión
         original se exigía que el toque y la vela de reversión
         ocurrieran el MISMO día, un evento muy poco frecuente. En la
         práctica el precio suele tocar la EMA21 un día y confirmar la
         reversión 1-3 ruedas después.
      3. Vela de reversión (martillo, envolvente o vela alcista fuerte)
         el día de la confirmación.
      4. La compra se ejecuta al día SIGUIENTE (apertura).

    Devuelve el df original + columnas:
      'tendencia_alcista', 'toca_ema21', 'toco_ema21_reciente',
      'vela_reversion', 'senal_confirmada', 'entrada_manana'
    """
    df = df.copy()
    df["tendencia_alcista"] = df["Close"] > df["EMA50"]

    # "toca" EMA21: el rango de la vela (low..high) cruza la EMA21,
    # o el cierre está muy cerca de ella (tolerancia)
    dist_pct = (df["Close"] - df["EMA21"]).abs() / df["EMA21"]
    toca_por_rango = (df["Low"] <= df["EMA21"]) & (df["High"] >= df["EMA21"])
    toca_por_cercania = dist_pct <= tolerancia_pct
    df["toca_ema21"] = toca_por_rango | toca_por_cercania

    # ventana de confirmación: ¿tocó la EMA21 en algún momento de los
    # últimos N días (incluyendo hoy)?
    df["toco_ema21_reciente"] = (
        df["toca_ema21"].rolling(ventana_confirmacion, min_periods=1)
        .max().astype(bool)
    )

    df["vela_reversion"] = detectar_vela_reversion(
        df, ratio_mecha=ratio_mecha,
        envolvente_parcial_pct=envolvente_parcial_pct,
        incluir_vela_fuerte=incluir_vela_fuerte,
    )

    df["senal_confirmada"] = (
        df["tendencia_alcista"] & df["toco_ema21_reciente"] & df["vela_reversion"]
    )

    # la entrada real se ejecuta al día hábil siguiente
    df["entrada_manana"] = df["senal_confirmada"].shift(1).fillna(False)

    return df


# %% [6d] SEÑAL DE MOMENTUM: "CRUCE EMA10/EMA20 + RSI > 70"
# ---------------------------------------------------------------------------
def generar_senales_momentum_ema_rsi(
    df: pd.DataFrame,
    rsi_umbral: float = 70.0,
    ventana_confirmacion: int = 3,
    usar_filtro_rsi: bool = True,
) -> pd.DataFrame:
    """
    Señal de MOMENTUM: EMA10 cruza por encima de la EMA20 (más rápida que
    el cruce EMA21/EMA50, para entrar más temprano en la tendencia),
    opcionalmente confirmada con RSI14 > rsi_umbral.

    `usar_filtro_rsi=False` da el cruce EMA10/EMA20 "puro", sin exigir
    momentum confirmado -- para aislar si el filtro de RSI ayudaba o
    solo restaba señales sin cambiar la calidad.

    Reutiliza el mismo motor de backtest (stop bajo el mínimo del día de
    señal, TP por resistencia/2R, trailing por EMA21) que las demás.
    """
    df = df.copy()
    ema10_sobre_ema20 = df["EMA10"] > df["EMA20"]
    cruce_hoy = ema10_sobre_ema20 & ~ema10_sobre_ema20.shift(1).fillna(False)
    df["cruce_reciente"] = cruce_hoy.rolling(ventana_confirmacion, min_periods=1).max().astype(bool)

    condicion = df["cruce_reciente"] & ema10_sobre_ema20
    if usar_filtro_rsi:
        condicion = condicion & (df["RSI14"] > rsi_umbral)

    df["senal_confirmada"] = condicion
    df["entrada_manana"] = df["senal_confirmada"].shift(1).fillna(False)
    return df


# %% [6c] SEÑAL DE TENDENCIA: "CRUCE ALCISTA EMA21/EMA50"
# ---------------------------------------------------------------------------
def generar_senales_cruce_alcista(
    df: pd.DataFrame,
    ventana_confirmacion: int = 3,
    exigir_volumen_creciente: bool = False,
) -> pd.DataFrame:
    """
    Señal de TENDENCIA (no de reversión): EMA21 cruza por encima de la
    EMA50 -- el clásico "golden cross" corto. A diferencia de RSI+BB,
    esta señal busca capturar el INICIO de una tendencia alcista sostenida
    (el tipo de movimiento que RSI+BB estructuralmente no puede agarrar,
    porque en una tendencia fuerte el RSI nunca perfora 30).

    IMPORTANTE: esta señal se agrega para probar, con el mismo rigor
    estadístico que RSI+BB (n grande, t-stat sobre todo el universo), si
    existe una ventaja real -- no porque "se vea bien" en un gráfico
    puntual. Un solo ejemplo visual nunca es evidencia suficiente.

    Reutiliza el mismo motor de backtest que las otras dos estrategias
    (stop bajo el mínimo del día de señal, TP por resistencia/2R,
    trailing por EMA21), así que la comparación es directa.
    """
    df = df.copy()
    ema21_sobre_ema50 = df["EMA21"] > df["EMA50"]
    cruce_hoy = ema21_sobre_ema50 & ~ema21_sobre_ema50.shift(1).fillna(False)

    # ventana de confirmación: el cruce pudo haber ocurrido cualquiera de
    # los últimos N días (no exige que sea el día exacto)
    df["cruce_reciente"] = cruce_hoy.rolling(ventana_confirmacion, min_periods=1).max().astype(bool)

    condicion = df["cruce_reciente"] & ema21_sobre_ema50  # el cruce ya ocurrió y sigue vigente
    if exigir_volumen_creciente:
        condicion = condicion & (df["Volume"] > df["Volume"].rolling(20).mean())

    df["senal_confirmada"] = condicion
    df["entrada_manana"] = df["senal_confirmada"].shift(1).fillna(False)
    return df


# %% [6b] SEÑAL ALTERNATIVA: "RSI SOBREVENTA + PRECIO BAJO BOLLINGER"
# ---------------------------------------------------------------------------
def generar_senales_rsi_bb(
    df: pd.DataFrame,
    rsi_umbral: float = 30.0,
    exigir_tendencia_alcista: bool = False,
) -> pd.DataFrame:
    """
    Señal alternativa a Pullback EMA21, basada en lo que el propio
    backtesting intraday del bot había identificado como consistentemente
    rentable: RSI en sobreventa (RSI14 < rsi_umbral) Y precio por debajo
    de la banda inferior de Bollinger, el mismo día.

    A diferencia de Pullback EMA21 (que exige una tendencia alcista de
    fondo), esta es una señal de reversión a la media pura: no asume
    que el activo esté en tendencia, solo que está estadísticamente
    "muy barato" respecto a su propia volatilidad reciente. Por eso
    `exigir_tendencia_alcista` es False por defecto -- se puede activar
    para comparar la versión "reversión dentro de tendencia" vs.
    "reversión pura", igual que se hizo en el backtesting intraday.

    Reutiliza el mismo motor de backtest que Pullback EMA21 (stop bajo
    el mínimo del día de señal, TP por resistencia/2R, trailing stop
    por EMA21, entrada al día siguiente): la única diferencia real
    entre estrategias queda aislada en esta función.

    Devuelve el df original + columnas 'senal_confirmada', 'entrada_manana'
    (mismo contrato que generar_senales_pullback_ema21, para que el motor
    de backtest sea intercambiable entre ambas señales).
    """
    df = df.copy()
    if "RSI14" not in df.columns or "BB_lower" not in df.columns:
        raise ValueError(
            "Faltan RSI14/BB_lower: correr calcular_indicadores(df) antes "
            "de generar_senales_rsi_bb."
        )

    df["rsi_sobreventa"] = df["RSI14"] < rsi_umbral
    df["precio_bajo_bb"] = df["Close"] < df["BB_lower"]

    condicion = df["rsi_sobreventa"] & df["precio_bajo_bb"]
    if exigir_tendencia_alcista:
        df["tendencia_alcista"] = df["Close"] > df["EMA50"]
        condicion = condicion & df["tendencia_alcista"]

    df["senal_confirmada"] = condicion
    df["entrada_manana"] = df["senal_confirmada"].shift(1).fillna(False)
    return df


# %% [7] FILTRO DE DÓLAR CCL (PROXY)
# ---------------------------------------------------------------------------
def descargar_ccl_proxy(periodo: str = "3y") -> pd.Series:
    """
    IOL/Yahoo no exponen el CCL directamente. Se aproxima con el ratio
    ADR/local de un activo muy líquido (GGAL): CCL_proxy = GGAL(NYSE) * 10 / GGAL.BA
    (10 = ratio de conversión ADR de GGAL). Sirve para detectar tendencia
    y volatilidad del dólar financiero, no para el valor exacto del CCL.

    NOTA: si se dispone de una fuente real de CCL (ej. API de un
    proveedor de datos financieros argentino), reemplazar esta función
    por la descarga directa de esa serie.
    """
    ggal_ba = yf.download("GGAL.BA", period=periodo, interval="1d",
                           progress=False, auto_adjust=True)
    ggal_us = yf.download("GGAL", period=periodo, interval="1d",
                           progress=False, auto_adjust=True)

    # yfinance devuelve columnas MultiIndex (Precio, Ticker) incluso para
    # un solo símbolo en algunas versiones -> aplanar antes de indexar,
    # igual que en descargar_datos_diarios, para evitar terminar con un
    # DataFrame de 1 columna en vez de una Serie.
    if isinstance(ggal_ba.columns, pd.MultiIndex):
        ggal_ba.columns = ggal_ba.columns.get_level_values(0)
    if isinstance(ggal_us.columns, pd.MultiIndex):
        ggal_us.columns = ggal_us.columns.get_level_values(0)

    serie_ba = ggal_ba["Close"].squeeze()
    serie_us = ggal_us["Close"].squeeze()

    df = pd.concat([serie_ba, serie_us], axis=1, keys=["local", "adr"]).dropna()
    ccl_proxy = (df["adr"] * 10) / df["local"]
    ccl_proxy = ccl_proxy.squeeze()

    if not isinstance(ccl_proxy, pd.Series):
        raise TypeError(
            "descargar_ccl_proxy no pudo construir una Serie 1D "
            f"(tipo obtenido: {type(ccl_proxy)}). Revisar formato de "
            "columnas devuelto por yfinance."
        )

    ccl_proxy.name = "CCL_proxy"
    return ccl_proxy


def filtro_ccl_favorable(ccl_proxy: pd.Series, ventana: int = 10,
                          vol_max_pct: float = 0.04) -> pd.Series:
    """
    Devuelve True en los días donde el CCL proxy está "tranquilo":
    variación porcentual sobre la ventana de N días por debajo de
    `vol_max_pct`. La lógica: evitar abrir posiciones swing (3-10 días)
    justo antes/durante saltos cambiarios fuertes, que distorsionan
    el análisis técnico en pesos.
    """
    ccl_proxy = ccl_proxy.squeeze()
    if not isinstance(ccl_proxy, pd.Series):
        raise TypeError(
            f"filtro_ccl_favorable espera una Serie 1D, recibió {type(ccl_proxy)}"
        )
    variacion = ccl_proxy.pct_change(ventana).abs()
    favorable = variacion <= vol_max_pct
    favorable = favorable.reindex(ccl_proxy.index).fillna(False)
    return favorable.astype(bool)


# %% [8] MOTOR DE BACKTESTING
# ---------------------------------------------------------------------------
@dataclass
class Trade:
    ticker: str
    fecha_entrada: pd.Timestamp
    precio_entrada: float
    stop_loss_inicial: float
    take_profit: float
    acciones: int
    fecha_salida: pd.Timestamp = None
    precio_salida: float = None
    motivo_salida: str = None
    pnl_bruto: float = None
    costos: float = None
    pnl_neto: float = None
    dias_holding: int = None
    stop_final: float = None  # último trailing stop aplicado
    sizing_limitado: bool = False  # True si el cap de capital redujo las acciones pedidas


def calcular_resistencia_previa(df: pd.DataFrame, idx: int, ventana: int = 30) -> float:
    """Máximo de los últimos `ventana` días previos a idx (resistencia)."""
    inicio = max(0, idx - ventana)
    return df["High"].iloc[inicio:idx].max()


def sizing_fijo(capital_swing: float, riesgo_pct: float,
                 entrada: float, stop: float) -> int:
    """acciones = (capital * riesgo%) / (entrada - stop)"""
    riesgo_monto = capital_swing * riesgo_pct
    riesgo_por_accion = entrada - stop
    if riesgo_por_accion <= 0:
        return 0
    return int(riesgo_monto // riesgo_por_accion)


def sizing_atr(capital_swing: float, riesgo_pct: float,
               entrada: float, atr: float, multiplicador_atr: float = 2.0) -> int:
    """
    Position sizing dinámico basado en ATR: el riesgo por acción se
    define como `multiplicador_atr * ATR14` en vez de depender del
    stop técnico exacto. Suele dar un tamaño más estable quitando
    ruido de velas de señal atípicas.
    """
    riesgo_monto = capital_swing * riesgo_pct
    riesgo_por_accion = multiplicador_atr * atr
    if riesgo_por_accion <= 0:
        return 0
    return int(riesgo_monto // riesgo_por_accion)


def backtest_pullback_ema21(
    df: pd.DataFrame,
    ticker: str,
    capital_swing: float = CAPITAL_INICIAL * CAPITAL_SWING_PCT,
    metodo_sizing: str = "fijo",       # "fijo" | "atr"
    usar_trailing_stop: bool = True,
    filtro_ccl: pd.Series = None,      # Serie booleana alineada por fecha, opcional
    min_dias_holding: int = 1,         # 1 = sin restricción (comportamiento actual);
                                        # >1 bloquea salidas por TP antes de ese día
                                        # (el SL SIEMPRE puede saltar, sin importar
                                        # min_dias_holding -- nunca se fuerza a
                                        # sostener una posición perdedora)
    usar_take_profit_fijo: bool = True,  # False = "dejar correr la ganancia":
                                          # ignora el TP fijo (resistencia/2R) y
                                          # solo sale por stop/trailing/máx días,
                                          # para medir si capturar más del rebote
                                          # más allá del objetivo inicial suma valor
    trailing_margen_pct: float = 0.0,    # colchón bajo la EMA21 para el trailing
                                          # (ej. 0.03 = trailing 3% por debajo de
                                          # la EMA21, en vez de pegado a ella)
    trailing_activar_desde_dia: int = 0, # días a esperar antes de que el
                                          # trailing empiece a regir (0 = desde
                                          # el primer día en ganancia, actual)
    cooldown_dias: int = 0,  # días de espera obligatoria tras un stop_loss
                              # (el fijo, no el trailing) antes de poder
                              # reentrar en este mismo ticker. 0 = sin
                              # cooldown (comportamiento anterior).
) -> pd.DataFrame:
    """
    Corre el backtest de la estrategia Pullback EMA21 sobre un dataframe
    diario ya indexado con indicadores y señales.

    Reglas de salida:
      - SL: por debajo del mínimo de la vela de señal (fijo, o reemplazado
        por trailing stop en EMA21 una vez que el trade está en ganancia)
      - TP: resistencia previa (máx 30 ruedas) o 2R si no hay resistencia
        clara por encima del precio de entrada
      - Cierre forzado a los MAX_DIAS_HOLDING días si no tocó SL ni TP
      - No hay pirámide: una sola posición abierta a la vez sobre este ticker
    """
    trades = []
    en_posicion = False
    trade_actual = None
    capital_disponible = capital_swing
    cooldown_hasta = None  # fecha hasta la cual no se puede reentrar tras un stop_loss

    for i in range(1, len(df) - 1):
        fecha = df.index[i]

        # --- gestión de posición abierta ---
        if en_posicion:
            row = df.iloc[i]
            dias_en_posicion = (fecha - trade_actual.fecha_entrada).days

            # trailing stop dinámico sobre EMA21 (solo sube, nunca baja).
            # `trailing_margen_pct` da colchón por debajo de la EMA21 (útil
            # en RSI+BB: al entrar desde sobreventa profunda, la EMA21
            # todavía está "arriba", y un trailing pegado a ella corta el
            # rebote casi enseguida). `trailing_activar_desde_dia` retrasa
            # cuándo empieza a regir el trailing, para no ajustarlo el
            # mismo día/al día siguiente de la entrada.
            if (usar_trailing_stop and row["Close"] > trade_actual.precio_entrada
                    and dias_en_posicion >= trailing_activar_desde_dia):
                nuevo_stop = row["EMA21"] * (1 - trailing_margen_pct)
                if trade_actual.stop_final is None or nuevo_stop > trade_actual.stop_final:
                    trade_actual.stop_final = nuevo_stop
            stop_vigente = trade_actual.stop_final if trade_actual.stop_final else trade_actual.stop_loss_inicial

            salida = None
            motivo = None
            if row["Low"] <= stop_vigente:
                # El stop loss (o trailing stop) SIEMPRE puede saltar,
                # sin importar min_dias_holding: nunca tiene sentido
                # forzar a sostener una posición que ya perforó el stop.
                salida = stop_vigente
                motivo = "trailing_stop" if trade_actual.stop_final else "stop_loss"
            elif (usar_take_profit_fijo and row["High"] >= trade_actual.take_profit
                  and dias_en_posicion >= min_dias_holding):
                salida = trade_actual.take_profit
                motivo = "take_profit"
            elif dias_en_posicion >= MAX_DIAS_HOLDING:
                salida = row["Close"]
                motivo = "cierre_forzado_max_dias"

            if salida is not None:
                monto_entrada = trade_actual.precio_entrada * trade_actual.acciones
                monto_salida = salida * trade_actual.acciones
                costos = costo_total_roundtrip(monto_entrada, monto_salida)
                pnl_bruto = monto_salida - monto_entrada
                pnl_neto = pnl_bruto - costos

                trade_actual.fecha_salida = fecha
                trade_actual.precio_salida = salida
                trade_actual.motivo_salida = motivo
                trade_actual.pnl_bruto = pnl_bruto
                trade_actual.costos = costos
                trade_actual.pnl_neto = pnl_neto
                trade_actual.dias_holding = dias_en_posicion

                capital_disponible += pnl_neto
                trades.append(trade_actual)
                if motivo == "stop_loss" and cooldown_dias > 0:
                    cooldown_hasta = fecha + pd.Timedelta(days=cooldown_dias)
                en_posicion = False
                trade_actual = None
            continue

        # --- evaluar entrada (solo si no hay posición abierta) ---
        if not df["entrada_manana"].iloc[i]:
            continue

        if cooldown_hasta is not None and fecha < cooldown_hasta:
            continue  # todavía en cooldown tras el último stop_loss

        if filtro_ccl is not None:
            if fecha not in filtro_ccl.index:
                continue
            valor_filtro = filtro_ccl.loc[fecha]
            # Defensivo: si por algún motivo el valor no es escalar
            # (índice duplicado, formato inesperado de yfinance), se
            # toma el primero en vez de romper todo el backtest.
            if isinstance(valor_filtro, pd.Series):
                valor_filtro = valor_filtro.iloc[0]
            if not bool(valor_filtro):
                continue  # CCL desfavorable, se salta la señal

        vela_senal_idx = i - 1
        vela_senal = df.iloc[vela_senal_idx]
        entrada = df["Open"].iloc[i]
        stop_inicial = vela_senal["Low"] * 0.995  # pequeño colchón

        riesgo_por_accion = entrada - stop_inicial
        if riesgo_por_accion <= 0:
            continue

        # Si el riesgo por acción es una fracción ínfima del precio
        # (stop prácticamente pegado a la entrada), no es una operación
        # realista: cualquier micro-ruido dispararía el stop, y forzar
        # el sizing sobre un riesgo tan chico es lo que generaba
        # posiciones absurdamente grandes (ver comentario más abajo,
        # caso YPFD.BA). Se descarta directamente en vez de intentar
        # sizearla.
        riesgo_minimo_pct = 0.005  # 0.5% del precio de entrada
        if riesgo_por_accion / entrada < riesgo_minimo_pct:
            continue

        resistencia = calcular_resistencia_previa(df, vela_senal_idx)
        tp_por_resistencia = resistencia if resistencia > entrada else None
        tp_por_rr = entrada + RR_MINIMO * riesgo_por_accion
        take_profit = max(tp_por_resistencia, tp_por_rr) if tp_por_resistencia else tp_por_rr

        if metodo_sizing == "atr":
            acciones = sizing_atr(capital_disponible, RIESGO_POR_OPERACION,
                                  entrada, vela_senal["ATR14"])
        else:
            acciones = sizing_fijo(capital_disponible, RIESGO_POR_OPERACION,
                                    entrada, stop_inicial)

        # --- TOPE DE SEGURIDAD: nunca invertir más que el capital ---
        # disponible. Sin este control, un stop casi pegado al precio de
        # entrada (riesgo_por_accion muy chico) puede hacer que
        # sizing_fijo/sizing_atr devuelvan una cantidad de acciones
        # absurda, generando una posición de millones de pesos con un
        # capital real de $70.000. Esto es lo que explicaba resultados
        # como el de YPFD.BA con pnl_neto_total de ~9 millones: no era
        # (solo) un dato corrupto de Yahoo, era este control faltante.
        # Con capitales chicos y riesgo del 2%, este tope se activa
        # seguido (es normal: significa "concentrar todo el capital
        # disponible en la posición"), así que no se imprime por cada
        # trade -- se cuenta y se reporta agregado en las métricas.
        sizing_fue_limitado = False
        if acciones > 0 and entrada > 0:
            acciones_maximas_por_capital = int(capital_disponible // entrada)
            if acciones > acciones_maximas_por_capital:
                sizing_fue_limitado = True
                acciones = acciones_maximas_por_capital

        if acciones <= 0:
            continue

        trade_actual = Trade(
            ticker=ticker,
            fecha_entrada=fecha,
            precio_entrada=entrada,
            stop_loss_inicial=stop_inicial,
            take_profit=take_profit,
            acciones=acciones,
            sizing_limitado=sizing_fue_limitado,
        )
        en_posicion = True

    resultado = pd.DataFrame([t.__dict__ for t in trades])
    return resultado


# %% [9] MÉTRICAS DE PERFORMANCE
# ---------------------------------------------------------------------------
def detectar_trades_sospechosos(trades: pd.DataFrame, ticker: str = "",
                                 retorno_maximo_pct: float = 150.0) -> pd.DataFrame:
    """
    Segunda capa de seguridad además de `limpiar_datos_diarios`: marca
    operaciones cuyo retorno porcentual sobre el monto invertido es
    fisicamente inverosímil para un holding de 3-10 días (ej. +150%),
    típicamente causado por un dato de precio corrupto que no llegó a
    filtrarse en la limpieza previa (un High/Low erróneo puntual en
    vez de un Close erróneo). No las elimina automáticamente: las
    imprime para que se audite a mano cuál fue la fecha/ticker exacto.
    """
    if trades.empty:
        return trades
    monto_invertido = trades["precio_entrada"] * trades["acciones"]
    retorno_pct = 100 * trades["pnl_neto"] / monto_invertido
    sospechosas = trades[retorno_pct.abs() > retorno_maximo_pct]
    if not sospechosas.empty:
        for _, t in sospechosas.iterrows():
            print(
                f"[SOSPECHOSO {ticker}] entrada {t['fecha_entrada'].date()} @ "
                f"{t['precio_entrada']:.2f} -> salida {t['fecha_salida'].date()} @ "
                f"{t['precio_salida']:.2f} ({t['motivo_salida']}): retorno "
                f"{100*t['pnl_neto']/(t['precio_entrada']*t['acciones']):.0f}% "
                f"en {t['dias_holding']} días. Revisar si el precio de salida "
                f"es un dato real o un error de Yahoo Finance."
            )
    return sospechosas


def calcular_metricas(trades: pd.DataFrame, capital_inicial: float,
                       ticker: str = "") -> dict:
    if trades.empty:
        return {"operaciones": 0}

    detectar_trades_sospechosos(trades, ticker=ticker)

    ganadoras = trades[trades["pnl_neto"] > 0]
    perdedoras = trades[trades["pnl_neto"] <= 0]

    equity = capital_inicial + trades["pnl_neto"].cumsum()
    max_dd = ((equity.cummax() - equity) / equity.cummax()).max()

    return {
        "operaciones": len(trades),
        "win_rate_%": round(100 * len(ganadoras) / len(trades), 1),
        "pnl_neto_total": round(trades["pnl_neto"].sum(), 2),
        "pnl_promedio": round(trades["pnl_neto"].mean(), 2),
        "ganancia_prom": round(ganadoras["pnl_neto"].mean(), 2) if len(ganadoras) else 0,
        "perdida_prom": round(perdedoras["pnl_neto"].mean(), 2) if len(perdedoras) else 0,
        "dias_holding_prom": round(trades["dias_holding"].mean(), 1),
        "max_drawdown_%": round(100 * max_dd, 1),
        "retorno_%_sobre_capital": round(100 * trades["pnl_neto"].sum() / capital_inicial, 1),
        "ops_sizing_limitado": int(trades["sizing_limitado"].sum()) if "sizing_limitado" in trades.columns else 0,
    }


# %% [9b] EVALUACIÓN AGREGADA DE PORTAFOLIO (muestra combinada)
# ---------------------------------------------------------------------------
def combinar_trades_universo(resultados: dict, excluir_tickers: list = None) -> pd.DataFrame:
    """
    Concatena las operaciones de todos los tickers de una corrida de
    `correr_backtest_universo` en una sola tabla, ordenada por fecha de
    salida. Sirve para evaluar la señal con una muestra combinada en vez
    de 22 muestras individuales -muchas de solo 1-3 operaciones- que no
    alcanzan para sacar ninguna conclusión estadística por separado.

    `excluir_tickers`: lista de tickers a dejar afuera del pool (ej.
    para una prueba de robustez sacando un ticker con datos sospechosos
    de Yahoo Finance, y ver cuánto cambia el resultado agregado sin él).
    """
    excluir_tickers = set(excluir_tickers or [])
    todas = [r["trades"] for ticker, r in resultados.items()
             if ticker not in excluir_tickers
             and r.get("trades") is not None and not r["trades"].empty]
    if not todas:
        return pd.DataFrame()
    combinado = pd.concat(todas, ignore_index=True)
    combinado = combinado.sort_values("fecha_salida").reset_index(drop=True)
    return combinado


def evaluar_portafolio(resultados: dict, nombre_estrategia: str = "",
                        excluir_tickers: list = None) -> dict:
    """
    Evalúa la señal con la muestra combinada de todo el universo, usando
    RETORNO PORCENTUAL POR OPERACIÓN (no PnL en pesos) como base de
    comparación. Esto es deliberado: cada backtest individual por ticker
    corrió con el capital swing completo e independiente (ej. $70.000
    por ticker), así que sumar el PnL en pesos de las 22 corridas
    asumiría, incorrectamente, tener $70.000 disponibles para cada
    ticker AL MISMO TIEMPO (irreal con una cuenta real de $100.000). El
    retorno % por operación, en cambio, es independiente del capital y
    sí es válido para evaluar si la señal tiene una ventaja genuina.

    Devuelve un diccionario con:
      - n, win_rate, retorno_prom_%, retorno_prom_ganadoras_%,
        retorno_prom_perdedoras_%, expectativa_%  (retorno esperado por
        operación: win_rate*ganancia_prom + (1-win_rate)*perdida_prom)
      - t_stat: estadístico t de una muestra para H0: retorno medio = 0
        (una heurística rápida de significancia; con n chico no hay que
        sobre-interpretarlo, pero con n>=30 empieza a ser informativo)
      - pnl_neto_total_no_realista: la suma en pesos de las 22 corridas,
        etiquetada explícitamente como no representativa de una cuenta
        real (ver nota arriba), solo a título de referencia.
    """
    trades = combinar_trades_universo(resultados, excluir_tickers=excluir_tickers)
    if trades.empty:
        return {"n": 0}

    monto_invertido = trades["precio_entrada"] * trades["acciones"]
    retorno_pct = 100 * trades["pnl_neto"] / monto_invertido

    ganadoras = retorno_pct[retorno_pct > 0]
    perdedoras = retorno_pct[retorno_pct <= 0]
    n = len(retorno_pct)
    win_rate = len(ganadoras) / n

    media = retorno_pct.mean()
    std = retorno_pct.std(ddof=1) if n > 1 else 0.0
    t_stat = (media / (std / np.sqrt(n))) if std > 0 else float("nan")

    expectativa = (
        win_rate * (ganadoras.mean() if len(ganadoras) else 0)
        + (1 - win_rate) * (perdedoras.mean() if len(perdedoras) else 0)
    )

    resultado = {
        "estrategia": nombre_estrategia,
        "n": n,
        "win_rate_%": round(100 * win_rate, 1),
        "retorno_prom_%": round(media, 2),
        "retorno_prom_ganadoras_%": round(ganadoras.mean(), 2) if len(ganadoras) else 0,
        "retorno_prom_perdedoras_%": round(perdedoras.mean(), 2) if len(perdedoras) else 0,
        "expectativa_%": round(expectativa, 2),
        "t_stat": round(t_stat, 2) if not np.isnan(t_stat) else None,
        "dias_holding_prom": round(trades["dias_holding"].mean(), 1),
        "pnl_neto_total_no_realista": round(trades["pnl_neto"].sum(), 2),
    }

    print(f"\n=== Evaluación de portafolio ({nombre_estrategia or 'estrategia'}) ===")
    if excluir_tickers:
        print(f"(excluyendo: {', '.join(excluir_tickers)})")
    print(f"Operaciones combinadas (todo el universo): {n}")
    print(f"Win rate combinado: {resultado['win_rate_%']}%")
    print(f"Retorno promedio por operación: {resultado['retorno_prom_%']}%")
    print(f"  - de las ganadoras: +{resultado['retorno_prom_ganadoras_%']}%")
    print(f"  - de las perdedoras: {resultado['retorno_prom_perdedoras_%']}%")
    print(f"Expectativa matemática por operación: {resultado['expectativa_%']}%")
    if resultado["t_stat"] is not None:
        significativo = "sí" if abs(resultado["t_stat"]) > 2 else "no"
        print(f"t-stat (H0: retorno medio = 0): {resultado['t_stat']} "
              f"(|t|>2 sugiere -no prueba- diferencia de cero: {significativo})")
    if n < 30:
        print(f"[AVISO] n={n} es chico para conclusiones estadísticas robustas; "
              f"tomar el t-stat como orientativo, no como prueba.")
    print(f"(Referencia, no realista: suma de PnL en pesos de las 22 corridas "
          f"independientes: {resultado['pnl_neto_total_no_realista']:,.2f})")

    return resultado


# %% [9c] BACKTEST DE PORTAFOLIO CON CAPITAL COMPARTIDO (real, no 22 corridas independientes)
# ---------------------------------------------------------------------------
def backtest_portafolio_compartido(
    tickers: list = TICKERS_SWING,
    capital_swing: float = CAPITAL_INICIAL * CAPITAL_SWING_PCT,
    fecha_inicio: str = "2025-01-01",
    periodo_descarga: str = "3y",   # historia extra antes de fecha_inicio, para
                                     # que EMA50/BB(20)/RSI(14) ya estén "calientes"
    estrategia: str = "rsi_bb",
    rsi_umbral: float = 30.0,
    exigir_tendencia_alcista_rsi_bb: bool = False,
    tolerancia_pct: float = 0.025,
    ventana_confirmacion: int = 3,
    metodo_sizing: str = "fijo",
    usar_trailing_stop: bool = True,
    min_dias_holding: int = 1,
    usar_take_profit_fijo: bool = True,
    trailing_margen_pct: float = 0.0,
    trailing_activar_desde_dia: int = 0,
    tope_maximo_posicion: float = None,  # ej. 100_000: nunca invertir más
                                          # que esto en UNA posición, sin
                                          # importar cuánto haya crecido el
                                          # efectivo disponible. Al tener
                                          # este tope, cuando el efectivo
                                          # supera el tope pueden quedar
                                          # varias posiciones simultáneas
                                          # abiertas (el sobrante de
                                          # efectivo queda libre para
                                          # otra señal el mismo día).
                                          # None = sin tope (comportamiento
                                          # anterior: usa todo lo disponible).
    cooldown_dias: int = 0,  # días de espera obligatoria en un ticker
                              # después de que ese mismo ticker saque por
                              # stop_loss (el stop fijo inicial, no el
                              # trailing -- el trailing casi siempre sale
                              # en ganancia o breakeven). Evita el patrón
                              # de "whipsaw": re-entrar de lleno en el
                              # mismo papel al día siguiente de que te
                              # saque por pérdida, mientras sigue en
                              # sobreventa varios días seguidos.
                              # 0 = sin cooldown (comportamiento anterior).
    tasa_efectivo_anual: float = 0.0,  # tasa nominal anual (ej. 0.35 = 35%)
                                        # que devenga el efectivo NO invertido
                                        # cada día hábil (simula dejarlo en un
                                        # Money Market / FCI / caución en vez
                                        # de pesos parados sin rendir). Se
                                        # capitaliza diario: tasa_diaria =
                                        # (1+tasa_efectivo_anual)**(1/252)-1.
                                        # 0.0 = sin rendimiento (comportamiento
                                        # anterior, efectivo 100% parado).
                                        # ES UN SUPUESTO del usuario -- el
                                        # motor no descarga tasas reales
                                        # históricas de ningún lado.
) -> dict:
    """
    A diferencia de `correr_backtest_universo` (que corre cada ticker con
    el capital swing COMPLETO e independiente, como si tuvieras $70.000
    para cada uno de los 22 al mismo tiempo), esta función simula UNA
    sola cuenta con efectivo compartido entre todos los tickers, día por
    día, en orden cronológico real. Si dos señales compiten por el mismo
    efectivo, se resuelven en el orden en que aparecen los tickers en
    `tickers` (no hay priorización por fuerza de señal, es simple orden
    de lista) -- es la respuesta realista a "cuánto hubiera ganado una
    cuenta de $X operando esto en la práctica".

    Devuelve:
      - "trades": DataFrame con todas las operaciones ejecutadas
      - "equity_curve": Serie de efectivo + valor de posiciones abiertas
        a cada fecha (aproximado: no marca a mercado intra-operación,
        solo actualiza al cerrar cada trade -- subestima el drawdown
        real dentro de una posición abierta, pero es correcto para el
        resultado final de la cuenta)
      - "capital_inicial", "capital_final", "retorno_%"
    """
    # --- 1. Descargar y preparar cada ticker (con historia extra para
    #        que los indicadores ya estén calculados antes de fecha_inicio) ---
    datos_por_ticker = {}
    for ticker in tickers:
        try:
            df = descargar_datos_diarios(ticker, periodo=periodo_descarga)
            df = calcular_indicadores(df)
            if estrategia == "rsi_bb":
                df = generar_senales_rsi_bb(
                    df, rsi_umbral=rsi_umbral,
                    exigir_tendencia_alcista=exigir_tendencia_alcista_rsi_bb,
                )
            elif estrategia == "cruce_alcista":
                df = generar_senales_cruce_alcista(
                    df, ventana_confirmacion=ventana_confirmacion,
                )
            elif estrategia == "momentum_ema_rsi":
                df = generar_senales_momentum_ema_rsi(
                    df, rsi_umbral=rsi_umbral, ventana_confirmacion=ventana_confirmacion,
                )
            else:
                df = generar_senales_pullback_ema21(
                    df, tolerancia_pct=tolerancia_pct,
                    ventana_confirmacion=ventana_confirmacion,
                )
            datos_por_ticker[ticker] = df
        except Exception as e:
            print(f"[WARN] {ticker}: no se pudo preparar ({e})")
        time.sleep(0.3)

    # --- 2. Calendario común: todas las fechas >= fecha_inicio presentes
    #        en cualquiera de los tickers ---
    fecha_inicio_ts = pd.Timestamp(fecha_inicio)
    todas_las_fechas = sorted(set().union(*[
        set(df.index[df.index >= fecha_inicio_ts]) for df in datos_por_ticker.values()
    ]))

    # --- 3. Simulación día por día con efectivo compartido ---
    efectivo = capital_swing
    posiciones_abiertas = {}   # ticker -> dict con datos de la posición
    trades = []
    equity_curve = {}
    cooldown_hasta = {}   # ticker -> fecha hasta la cual no se puede reentrar
    tasa_diaria = (1 + tasa_efectivo_anual) ** (1 / 252) - 1 if tasa_efectivo_anual else 0.0

    for fecha in todas_las_fechas:
        # -- devengamiento diario del efectivo NO invertido (Money Market/
        #    caución). Se aplica sobre el efectivo libre de este momento del
        #    día, antes de evaluar cierres/entradas -- es una aproximación
        #    (en la realidad el rendimiento se calcularía sobre el saldo
        #    exacto minuto a minuto), suficiente para dimensionar el efecto.
        if tasa_diaria:
            efectivo *= (1 + tasa_diaria)

        # -- a) evaluar cierres de posiciones abiertas --
        for ticker in list(posiciones_abiertas.keys()):
            df = datos_por_ticker[ticker]
            if fecha not in df.index:
                continue
            row = df.loc[fecha]
            pos = posiciones_abiertas[ticker]
            dias_en_posicion = (fecha - pos["fecha_entrada"]).days

            if (usar_trailing_stop and row["Close"] > pos["precio_entrada"]
                    and dias_en_posicion >= trailing_activar_desde_dia):
                nuevo_stop = row["EMA21"] * (1 - trailing_margen_pct)
                if pos["stop_final"] is None or nuevo_stop > pos["stop_final"]:
                    pos["stop_final"] = nuevo_stop
            stop_vigente = pos["stop_final"] if pos["stop_final"] else pos["stop_loss_inicial"]

            salida, motivo = None, None
            if row["Low"] <= stop_vigente:
                salida = stop_vigente
                motivo = "trailing_stop" if pos["stop_final"] else "stop_loss"
            elif (usar_take_profit_fijo and row["High"] >= pos["take_profit"]
                  and dias_en_posicion >= min_dias_holding):
                salida = pos["take_profit"]
                motivo = "take_profit"
            elif dias_en_posicion >= MAX_DIAS_HOLDING:
                salida = row["Close"]
                motivo = "cierre_forzado_max_dias"

            if salida is not None:
                monto_entrada = pos["precio_entrada"] * pos["acciones"]
                monto_salida = salida * pos["acciones"]
                costos = costo_total_roundtrip(monto_entrada, monto_salida)
                pnl_neto = (monto_salida - monto_entrada) - costos
                efectivo += monto_salida - costo_venta(monto_salida, intradia=False)

                trades.append({
                    "ticker": ticker, "fecha_entrada": pos["fecha_entrada"],
                    "precio_entrada": pos["precio_entrada"], "acciones": pos["acciones"],
                    "fecha_salida": fecha, "precio_salida": salida,
                    "motivo_salida": motivo, "pnl_neto": pnl_neto,
                    "dias_holding": dias_en_posicion,
                })
                if motivo == "stop_loss" and cooldown_dias > 0:
                    cooldown_hasta[ticker] = fecha + pd.Timedelta(days=cooldown_dias)
                del posiciones_abiertas[ticker]

        # -- b) evaluar nuevas entradas (en orden de la lista de tickers) --
        for ticker in tickers:
            if ticker in posiciones_abiertas or efectivo <= 0:
                continue
            if ticker in cooldown_hasta and fecha < cooldown_hasta[ticker]:
                continue  # todavía en cooldown tras el último stop_loss
            df = datos_por_ticker.get(ticker)
            if df is None or fecha not in df.index:
                continue
            row = df.loc[fecha]
            if not row.get("entrada_manana", False):
                continue

            idx = df.index.get_loc(fecha)
            if idx == 0:
                continue
            vela_senal = df.iloc[idx - 1]
            entrada = row["Open"]
            stop_inicial = vela_senal["Low"] * 0.995
            riesgo_por_accion = entrada - stop_inicial
            if riesgo_por_accion <= 0 or riesgo_por_accion / entrada < 0.005:
                continue

            resistencia = calcular_resistencia_previa(df, idx - 1)
            tp_resistencia = resistencia if resistencia > entrada else None
            tp_rr = entrada + RR_MINIMO * riesgo_por_accion
            take_profit = max(tp_resistencia, tp_rr) if tp_resistencia else tp_rr

            if metodo_sizing == "atr":
                acciones = sizing_atr(efectivo, RIESGO_POR_OPERACION, entrada, vela_senal["ATR14"])
            else:
                acciones = sizing_fijo(efectivo, RIESGO_POR_OPERACION, entrada, stop_inicial)

            # Factor de comisión de compra (0.5%+IVA comisión + 0.05%+IVA
            # derechos), para que el tope de acciones deje margen y no
            # rechace la orden por unos pesos de comisión de más -- antes
            # el cap usaba efectivo/entrada "a secas", sin dejar lugar
            # para el costo de la operación, y con topes que calzan justo
            # (ej. tope_maximo_posicion divisible por el precio) la orden
            # se descartaba en silencio.
            factor_comision_compra = (
                COMISION_COMPRA_PCT * (1 + IVA_PCT) + DERECHOS_MERCADO_PCT * (1 + IVA_PCT)
            )
            if entrada > 0:
                max_acciones_efectivo = int(efectivo / (entrada * (1 + factor_comision_compra)))
                acciones = min(acciones, max_acciones_efectivo)
                if tope_maximo_posicion is not None:
                    max_acciones_tope = int(tope_maximo_posicion / (entrada * (1 + factor_comision_compra)))
                    acciones = min(acciones, max_acciones_tope)
            else:
                acciones = 0
            if acciones <= 0:
                continue

            monto_entrada = entrada * acciones
            costos_entrada = costo_compra(monto_entrada)
            # red de seguridad final: si por redondeo igual quedó ajustado,
            # se reduce de a 1 acción en vez de descartar todo el trade
            while acciones > 0 and (monto_entrada + costos_entrada) > efectivo:
                acciones -= 1
                monto_entrada = entrada * acciones
                costos_entrada = costo_compra(monto_entrada) if acciones > 0 else 0
            if acciones <= 0:
                continue

            efectivo -= (monto_entrada + costos_entrada)
            posiciones_abiertas[ticker] = {
                "precio_entrada": entrada, "acciones": acciones,
                "stop_loss_inicial": stop_inicial, "take_profit": take_profit,
                "stop_final": None, "fecha_entrada": fecha,
            }

        # -- c) registrar equity del día (efectivo + costo base de lo abierto) --
        valor_posiciones = sum(p["precio_entrada"] * p["acciones"] for p in posiciones_abiertas.values())
        equity_curve[fecha] = efectivo + valor_posiciones

    # --- 4. Cerrar a mercado cualquier posición que siga abierta al final ---
    for ticker, pos in posiciones_abiertas.items():
        df = datos_por_ticker[ticker]
        ultimo_precio = df["Close"].iloc[-1]
        monto_entrada = pos["precio_entrada"] * pos["acciones"]
        monto_salida = ultimo_precio * pos["acciones"]
        costos = costo_total_roundtrip(monto_entrada, monto_salida)
        pnl_neto = (monto_salida - monto_entrada) - costos
        efectivo += monto_salida - costo_venta(monto_salida, intradia=False)
        trades.append({
            "ticker": ticker, "fecha_entrada": pos["fecha_entrada"],
            "precio_entrada": pos["precio_entrada"], "acciones": pos["acciones"],
            "fecha_salida": df.index[-1], "precio_salida": ultimo_precio,
            "motivo_salida": "abierta_al_cierre_del_backtest", "pnl_neto": pnl_neto,
            "dias_holding": (df.index[-1] - pos["fecha_entrada"]).days,
        })

    trades_df = pd.DataFrame(trades).sort_values("fecha_salida").reset_index(drop=True) if trades else pd.DataFrame()
    equity_serie = pd.Series(equity_curve).sort_index()
    capital_final = efectivo

    resultado = {
        "trades": trades_df,
        "equity_curve": equity_serie,
        "capital_inicial": capital_swing,
        "capital_final": round(capital_final, 2),
        "retorno_%": round(100 * (capital_final - capital_swing) / capital_swing, 1),
        "operaciones": len(trades_df),
        "win_rate_%": round(100 * (trades_df["pnl_neto"] > 0).mean(), 1) if not trades_df.empty else 0,
    }

    print(f"\n=== Backtest de portafolio compartido ({estrategia}, desde {fecha_inicio}) ===")
    if tasa_efectivo_anual:
        print(f"(efectivo ocioso devengando {tasa_efectivo_anual*100:.1f}% TNA asumida -- "
              f"supuesto del usuario, no es una tasa histórica real descargada)")
    print(f"Capital inicial: ${capital_swing:,.0f}")
    print(f"Capital final: ${capital_final:,.2f}")
    print(f"Retorno: {resultado['retorno_%']}%")
    print(f"Operaciones: {resultado['operaciones']} | Win rate: {resultado['win_rate_%']}%")
    if not equity_serie.empty:
        dd = ((equity_serie.cummax() - equity_serie) / equity_serie.cummax()).max()
        print(f"Max drawdown (aprox., solo marca al cerrar trades): {100*dd:.1f}%")

    return resultado


# %% [10] CORRIDA COMPLETA SOBRE EL UNIVERSO SWING
# ---------------------------------------------------------------------------
def correr_backtest_universo(
    tickers: list = TICKERS_SWING,
    capital_swing: float = CAPITAL_INICIAL * CAPITAL_SWING_PCT,
    usar_ccl: bool = False,
    metodo_sizing: str = "fijo",
    usar_trailing_stop: bool = True,
    periodo: str = "5y",
    tolerancia_pct: float = 0.025,
    ventana_confirmacion: int = 3,
    estrategia: str = "pullback_ema21",   # "pullback_ema21" | "rsi_bb"
    rsi_umbral: float = 30.0,
    exigir_tendencia_alcista_rsi_bb: bool = False,
    min_dias_holding: int = 1,
    usar_take_profit_fijo: bool = True,
    trailing_margen_pct: float = 0.0,
    trailing_activar_desde_dia: int = 0,
    cooldown_dias: int = 0,
    usar_filtro_rsi_momentum: bool = True,  # False = cruce EMA10/20 "puro",
                                             # sin exigir RSI>rsi_umbral
) -> dict:
    """
    Corre el backtest para cada ticker del universo y devuelve:
      { ticker: {"trades": df_trades, "metricas": dict} }

    `estrategia` elige qué señal de entrada usar, manteniendo el mismo
    motor de backtest (stop, TP, trailing, costos, sizing) para poder
    comparar ambas en igualdad de condiciones:
      - "pullback_ema21": tendencia alcista + toque de EMA21 + vela de
        reversión (parámetros `tolerancia_pct`, `ventana_confirmacion`)
      - "rsi_bb": RSI14 en sobreventa + precio bajo banda inferior de
        Bollinger (parámetros `rsi_umbral`, `exigir_tendencia_alcista_rsi_bb`)
    """
    ccl_proxy = None
    filtro = None
    if usar_ccl:
        ccl_proxy = descargar_ccl_proxy(periodo=periodo)
        filtro = filtro_ccl_favorable(ccl_proxy)

    resultados = {}
    for ticker in tickers:
        try:
            df = descargar_datos_diarios(ticker, periodo=periodo)
            df = calcular_indicadores(df)

            if estrategia == "rsi_bb":
                df = generar_senales_rsi_bb(
                    df, rsi_umbral=rsi_umbral,
                    exigir_tendencia_alcista=exigir_tendencia_alcista_rsi_bb,
                )
            elif estrategia == "cruce_alcista":
                df = generar_senales_cruce_alcista(
                    df, ventana_confirmacion=ventana_confirmacion,
                )
            elif estrategia == "momentum_ema_rsi":
                df = generar_senales_momentum_ema_rsi(
                    df, rsi_umbral=rsi_umbral, ventana_confirmacion=ventana_confirmacion,
                    usar_filtro_rsi=usar_filtro_rsi_momentum,
                )
            else:
                df = generar_senales_pullback_ema21(
                    df, tolerancia_pct=tolerancia_pct,
                    ventana_confirmacion=ventana_confirmacion,
                )

            trades = backtest_pullback_ema21(
                df, ticker, capital_swing=capital_swing,
                metodo_sizing=metodo_sizing,
                usar_trailing_stop=usar_trailing_stop,
                filtro_ccl=filtro,
                min_dias_holding=min_dias_holding,
                usar_take_profit_fijo=usar_take_profit_fijo,
                trailing_margen_pct=trailing_margen_pct,
                trailing_activar_desde_dia=trailing_activar_desde_dia,
                cooldown_dias=cooldown_dias,
            )
            metricas = calcular_metricas(trades, capital_swing, ticker=ticker)
            metricas["dias_de_historia"] = len(df)
            resultados[ticker] = {"trades": trades, "metricas": metricas}
        except Exception as e:
            print(f"[WARN] {ticker}: no se pudo backtestear ({e})")
        time.sleep(0.5)  # evita rate-limiting de Yahoo Finance con 22 tickers
    return resultados


def imprimir_comparacion(resultados_a: dict, resultados_b: dict,
                          tickers: list = None,
                          etiqueta_a: str = "sin_ccl", etiqueta_b: str = "con_ccl",
                          ordenar_por: str = None):
    """
    Tabla comparativa entre dos corridas de resultados (dos escenarios
    de CCL, o dos estrategias distintas -ej. Pullback EMA21 vs RSI+BB-),
    ordenada por rentabilidad. `etiqueta_a`/`etiqueta_b` nombran las
    columnas para que la tabla sea legible sin importar qué se está
    comparando.
    """
    tickers = tickers if tickers is not None else TICKERS_SWING
    col_ops_a, col_win_a, col_pnl_a = f"ops_{etiqueta_a}", f"winrate_{etiqueta_a}", f"pnl_{etiqueta_a}"
    col_ops_b, col_win_b, col_pnl_b = f"ops_{etiqueta_b}", f"winrate_{etiqueta_b}", f"pnl_{etiqueta_b}"

    filas = []
    for ticker in tickers:
        m_a = resultados_a.get(ticker, {}).get("metricas", {})
        m_b = resultados_b.get(ticker, {}).get("metricas", {})
        filas.append({
            "ticker": ticker,
            col_ops_a: m_a.get("operaciones", 0),
            col_win_a: m_a.get("win_rate_%", 0),
            col_pnl_a: m_a.get("pnl_neto_total", 0),
            col_ops_b: m_b.get("operaciones", 0),
            col_win_b: m_b.get("win_rate_%", 0),
            col_pnl_b: m_b.get("pnl_neto_total", 0),
        })
    tabla = pd.DataFrame(filas)
    ordenar_por = ordenar_por or col_pnl_a
    if ordenar_por in tabla.columns:
        tabla = tabla.sort_values(ordenar_por, ascending=False)
    print(tabla.to_string(index=False))
    return tabla


# %% [11] MAIN DE BACKTESTING (correr en Colab)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=== [Pullback EMA21] Backtest SIN filtro CCL ===")
    resultados_pullback = correr_backtest_universo(usar_ccl=False, estrategia="pullback_ema21")
    for ticker, r in resultados_pullback.items():
        print(ticker, r["metricas"])

    print("\n=== [RSI sobreventa + Bollinger] Backtest SIN filtro CCL ===")
    resultados_rsi_bb = correr_backtest_universo(usar_ccl=False, estrategia="rsi_bb")
    for ticker, r in resultados_rsi_bb.items():
        print(ticker, r["metricas"])

    print("\n=== Comparación Pullback EMA21 vs RSI+Bollinger (sin CCL) ===")
    imprimir_comparacion(resultados_pullback, resultados_rsi_bb,
                         etiqueta_a="pullback_ema21", etiqueta_b="rsi_bb")

    # Evaluación de portafolio: combina las operaciones de los 22 tickers
    # en una sola muestra, usando retorno % por operación (no PnL en
    # pesos) para que sea comparable pese a que cada corrida individual
    # usó capital independiente.
    evaluar_portafolio(resultados_pullback, nombre_estrategia="Pullback EMA21")
    evaluar_portafolio(resultados_rsi_bb, nombre_estrategia="RSI + Bollinger")

    # Prueba de robustez: ¿cuánto cambia RSI+BB sin ECOG.BA? (ticker con
    # historia corta y un trade marcado como [SOSPECHOSO] por posible
    # dato corrupto de Yahoo Finance)
    evaluar_portafolio(resultados_rsi_bb, nombre_estrategia="RSI + Bollinger (sin ECOG)",
                       excluir_tickers=["ECOG.BA"])

    # El experimento anterior (sacar el TP fijo) dio resultados IDÉNTICOS
    # al original -> confirma que el trailing stop sobre EMA21, no el TP,
    # es la restricción real que corta las operaciones a 1-2 días. Para
    # medir si "dejarlo correr más" ayuda, hay que aflojar el TRAILING,
    # no el TP: se le da un colchón de 3% bajo la EMA21 y se retrasa su
    # activación a partir del día 2 (en vez de desde el primer cierre en
    # ganancia).
    print("\n=== [RSI+BB] Trailing más laxo (colchón 3% bajo EMA21, activa desde día 2) ===")
    resultados_rsi_bb_trailing_laxo = correr_backtest_universo(
        usar_ccl=False, estrategia="rsi_bb",
        trailing_margen_pct=0.03, trailing_activar_desde_dia=2,
    )
    for ticker, r in resultados_rsi_bb_trailing_laxo.items():
        print(ticker, r["metricas"])
    evaluar_portafolio(resultados_rsi_bb_trailing_laxo, nombre_estrategia="RSI+BB trailing laxo")
    evaluar_portafolio(resultados_rsi_bb_trailing_laxo, nombre_estrategia="RSI+BB trailing laxo (sin ECOG)",
                       excluir_tickers=["ECOG.BA"])
