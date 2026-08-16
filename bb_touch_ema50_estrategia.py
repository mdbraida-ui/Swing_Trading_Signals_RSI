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
REGLAS DE LA ESTRATEGIA (v2 -- filtro de movimiento máximo agregado)
--------------------------------------------------------------------------
  1) Señal pendiente: el precio toca la banda inferior de Bollinger
     (Low <= BB_lower). Queda viva sin límite de tiempo hasta que el BBW
     = (BB_upper - BB_lower)/BB_mid supere `BBW_UMBRAL` (0.200).
  1b) Filtro de movimiento máximo: al confirmar, si el precio ya subió
     más de `PCT_MOVIDO_MAXIMO` (10%) desde el toque de banda, la señal
     se INVALIDA en vez de confirmarse -- evita comprar "rebotes ya
     gastados" con poco recorrido hasta la EMA50. Validado en backtest
     sobre el universo real (102 tickers, 2023-2026): mejora win rate
     y baja max drawdown, a cambio de algo de retorno bruto.
  2) Entrada: el mismo día que se confirma la señal, aproximada en vivo
     con el precio intradía de la ventana de cierre (~16:30-16:50) --
     ver bot_bb_touch_diario.py, rutina_cierre.
  3) Sizing: capital objetivo directo, hasta `TOPE_MAXIMO_POSICION`
     (o el efectivo remanente si es menor), incluyendo comisión+IVA+
     derechos de mercado dentro del monto. Reparto en orden de la
     planilla si hay más de una señal el mismo día.
  4) SL inicial (Fase A): 15% bajo el precio de entrada (subido de 10%
     el 10/08/2026, ver SL_INICIAL_PCT más abajo), chequeado
     intradía contra el precio en vivo (rutina_monitoreo_fase_a, cada
     10 min).
  5) Fase B: una vez que el precio CIERRA por encima de la EMA50, el SL
     dejar de regir -- única salida es un cierre por debajo de la EMA50.
     Protección de día de transición: el día que recién cruza no se
     evalúa salida ese mismo día.
  6) Cooldown de 3 días tras un stop_loss genuino (Fase A) antes de
     poder reentrar en el mismo ticker.
  7) Cola de reintento de ejecución (hasta 3 días, invalidada si el
     precio cae más del SL desde el precio de confirmación) -- ver
     bot_bb_touch_diario.py, sección "cola de reintento".
============================================================================
"""

import csv
import os
import numpy as np
import pandas as pd
import yfinance as yf
import ta

# --- Parámetros de la estrategia (producción) ---
BBW_UMBRAL = 0.200
SL_INICIAL_PCT = 0.15  # subido de 0.10 a 0.15 el 10/08/2026 -- validado en
                        # backtest sobre 102 tickers: retorno 731.3%->771.7%,
                        # win rate 48.4%->55.0%, max drawdown 4.6%->4.4%,
                        # robusto ante concentración (sin el mejor trade,
                        # retiene 83.3% del resultado). Se probaron también
                        # SL 20% (rendimientos decrecientes) y SL 50%
                        # (básicamente sin protección real, stop casi nunca
                        # dispara -- artefacto de backtest, no una mejora
                        # genuina). Ver bitácora del chat del proyecto,
                        # sesión del 06-10/08/2026, para el detalle completo
                        # de la comparación entre SL 10%/15%/20%/50%.
PCT_MOVIDO_MAXIMO = 10.0  # señal se invalida si el precio ya subió más de esto desde el toque de banda
COOLDOWN_DIAS = 3
TOPE_MAXIMO_POSICION = 100_000.0
DIAS_MAXIMO_REINTENTO_EJECUCION = 3  # ver "cola de señales pendientes de ejecución" en bot_bb_touch_diario.py

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


def desglosar_costos_compra(monto: float) -> tuple:
    """Devuelve (comision_con_iva, derechos_mercado_con_iva) por separado
    -- costo_compra() ya suma ambos, esto es para mostrarlos abiertos
    en el histórico de operaciones (columnas nuevas del dashboard)."""
    comision = monto * COMISION_COMPRA_PCT * (1 + IVA_PCT)
    derechos_mercado = monto * DERECHOS_MERCADO_PCT * (1 + IVA_PCT)
    return comision, derechos_mercado


def desglosar_costos_venta(monto: float, intradia: bool = False) -> tuple:
    """Igual que desglosar_costos_compra, pero para la pata de venta --
    respeta la misma bonificación de comisión intradía que costo_venta()."""
    derechos_mercado = monto * DERECHOS_MERCADO_PCT * (1 + IVA_PCT)
    comision = 0.0 if intradia else monto * COMISION_COMPRA_PCT * (1 + IVA_PCT)
    return comision, derechos_mercado


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

    Robusto ante problemas de codificación: si el CSV se armó/editó en
    Excel, Word o pegando texto desde una web, es común que termine en
    latin-1/cp1252 en vez de UTF-8, o con caracteres "espacio no
    separable" (\\xa0) sueltos -- probamos UTF-8 primero y caemos a
    latin-1 si falla, y limpiamos \\xa0 de cada campo para que no
    rompa comparaciones tipo `activo == "SI"`.
    """
    contenido = None
    for codificacion in ("utf-8-sig", "latin-1"):
        try:
            with open(ruta_csv, newline="", encoding=codificacion) as f:
                contenido = f.read()
            break
        except UnicodeDecodeError:
            continue
    if contenido is None:
        raise ValueError(f"No se pudo leer {ruta_csv} ni como UTF-8 ni como latin-1")

    contenido = contenido.replace("\xa0", " ")  # espacio no separable -> espacio normal

    filas = []
    lector = csv.DictReader(contenido.splitlines())
    for fila in lector:
        columna_ticker = "ticker" if "ticker" in fila else "ticker_cedear"
        ticker = fila[columna_ticker].strip().upper()
        tipo = fila["tipo"].strip().lower()
        activo = fila["activo"].strip().upper()
        if solo_activos and activo != "SI":
            continue
        filas.append({"ticker": ticker, "tipo": tipo, "activo": activo,
                       "notas": (fila.get("notas") or "").strip()})
    return filas


CARPETA_HISTORICOS_MANUALES = "historicos_manuales"


def cargar_historico_manual(ticker: str, carpeta: str = CARPETA_HISTORICOS_MANUALES) -> pd.DataFrame | None:
    """
    Carga un CSV con historial cargado a mano (formato exportado desde
    la fuente de datos del usuario: columnas Date, Open, High, Low,
    Close, Volume; fecha tipo "2/01/2025 17:00:00" -- día/mes/año, con
    hora fija que se ignora). Devuelve None si el archivo no existe --
    esto es lo normal para la gran mayoría de los tickers, que no
    necesitan histórico manual y siguen 100% en Yahoo.

    Existe por un problema real detectado en julio 2026: Yahoo Finance
    "reseteó" el historial de varios CEDEARs (META.BA, VIST.BA, PBR.BA,
    AVGO.BA entre otros) -- el símbolo sigue existiendo y Yahoo volvió
    a acumular datos desde ese momento en adelante, pero perdió todo el
    historial previo (confirmado: `yf.download(..., period="5y")`
    devolvía 1 sola fila, con fecha de HOY). Sin al menos ~100-120 días
    de historia, EMA50 no converge a un valor confiable -- de ahí la
    necesidad de "rellenar" el tramo viejo con una fuente alternativa.
    """
    ruta = os.path.join(carpeta, f"{ticker}.csv")
    if not os.path.exists(ruta):
        return None
    try:
        # las exportaciones de Google Sheets suelen traer filas/columnas
        # vacías al principio (formato visual de la hoja) -- se busca
        # la fila real de encabezado en vez de asumir que es la primera.
        crudo = pd.read_csv(ruta, header=None, dtype=str, nrows=20)
        fila_header = None
        for i in range(len(crudo)):
            if "Date" in crudo.iloc[i].values:
                fila_header = i
                break
        if fila_header is None:
            print(f"[histórico manual {ticker}] no se encontró la fila de encabezado 'Date' -- se ignora el archivo")
            return None

        df = pd.read_csv(ruta, header=fila_header)
        df = df.dropna(axis=1, how="all")   # columnas completamente vacías (las ",," del margen)
        df = df.dropna(subset=["Date"])     # filas sin fecha (blancos residuales)

        df["Date"] = pd.to_datetime(df["Date"], dayfirst=True).dt.normalize()
        df = df.set_index("Date")
        columnas_necesarias = ["Open", "High", "Low", "Close", "Volume"]
        faltantes = [c for c in columnas_necesarias if c not in df.columns]
        if faltantes:
            print(f"[histórico manual {ticker}] faltan columnas {faltantes} -- se ignora el archivo")
            return None
        return df[columnas_necesarias].sort_index()
    except Exception as e:
        print(f"[histórico manual {ticker}] error al leer {ruta}: {e} -- se ignora el archivo")
        return None


def descargar_datos_diarios(ticker: str, periodo: str = "6mo", limpiar_anomalias: bool = True) -> pd.DataFrame:
    """
    Descarga velas diarias de Yahoo Finance. Aplana columnas
    multi-índice (problema conocido de yfinance con un solo ticker) y,
    si `limpiar_anomalias`, reemplaza saltos de precio >30% en un día
    por el último valor válido (protege contra errores de datos tipo
    el de ECOG.BA/TECO2.BA ya documentados -- NO aplica a CEDEARs,
    donde saltos sincronizados son movimientos de tipo de cambio
    genuinos, no errores).

    *** EMPALME CON HISTÓRICO MANUAL (ver cargar_historico_manual) ***
    Si existe `historicos_manuales/{ticker}.csv`, se usa para cubrir
    las fechas ANTERIORES a lo que Yahoo tenga disponible -- Yahoo
    sigue siendo la fuente de verdad para cualquier fecha que sí tenga
    (se prioriza sobre el archivo manual en caso de solaparse), así que
    a medida que Yahoo vaya acumulando más historial propio con el
    correr de los meses, el archivo manual se vuelve progresivamente
    menos necesario sin que haga falta borrarlo ni tocar nada.
    """
    df_yahoo = yf.download(ticker, period=periodo, progress=False, auto_adjust=True)
    if not df_yahoo.empty and isinstance(df_yahoo.columns, pd.MultiIndex):
        df_yahoo.columns = df_yahoo.columns.get_level_values(0)

    df_manual = cargar_historico_manual(ticker)

    if df_manual is not None:
        if not df_yahoo.empty:
            fecha_min_yahoo = df_yahoo.index.min()
            df_manual_recortado = df_manual[df_manual.index < fecha_min_yahoo]
            df = pd.concat([df_manual_recortado, df_yahoo[["Open", "High", "Low", "Close", "Volume"]]]).sort_index()
            print(f"[histórico manual {ticker}] {len(df_manual_recortado)} filas manuales "
                  f"(hasta {df_manual_recortado.index.max().date() if len(df_manual_recortado) else 'N/A'}) "
                  f"+ {len(df_yahoo)} filas de Yahoo (desde {fecha_min_yahoo.date()})")
        else:
            df = df_manual
            print(f"[histórico manual {ticker}] Yahoo sin datos -- usando solo histórico manual "
                  f"({len(df)} filas, hasta {df.index.max().date()})")
    else:
        if df_yahoo.empty:
            raise ValueError(f"Sin datos para {ticker}")
        df = df_yahoo

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
    df["EMA10"] = ta.trend.EMAIndicator(close, window=10).ema_indicator()  # modo EMA10 (10/08/2026)
    df["EMA21"] = ta.trend.EMAIndicator(close, window=21).ema_indicator()
    df["EMA50"] = ta.trend.EMAIndicator(close, window=50).ema_indicator()
    bb = ta.volatility.BollingerBands(close, window=20, window_dev=2)
    df["BB_lower"] = bb.bollinger_lband()
    df["BB_mid"] = bb.bollinger_mavg()
    df["BB_upper"] = bb.bollinger_hband()
    df["BBW"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]
    df["RSI14"] = ta.momentum.RSIIndicator(close, window=14).rsi()
    return df


def generar_senales_bb_touch_bbw(df: pd.DataFrame, bbw_umbral: float = BBW_UMBRAL,
                                  pct_movido_maximo: float = PCT_MOVIDO_MAXIMO) -> pd.DataFrame:
    """
    Reconstruye el estado "pendiente" desde el arranque de la serie
    descargada hasta HOY -- en vivo no hay estado persistente entre
    corridas, así que cada vez que se llama esta función se recalcula
    todo desde cero con el historial completo (igual que el backtest).
    Sin límite de tiempo para la señal pendiente (ver "CAMBIOS EN v6"
    del backtest -- se probó un límite de 6 velas y se descartó).

    *** FILTRO DE MOVIMIENTO MÁXIMO (llevado a producción tras validar
    en backtest sobre el universo real de 102 tickers) ***
    Cuando el BBW finalmente supera el umbral, si el precio YA subió
    más de `pct_movido_maximo` desde el toque de banda que originó la
    espera, la señal se INVALIDA -- no se confirma, y hay que esperar
    un toque nuevo. Motivo (confirmado con diagnóstico de datos reales
    del proyecto): las señales que confirman tarde, con el precio ya
    corrido, tienen mediana de -14.8% peor resultado que las que
    confirman rápido -- "rebote ya gastado", poco recorrido hasta la
    EMA50. El movimiento hacia ABAJO nunca invalida (seguir cerca de
    mínimos es un buen punto de entrada, no uno malo).

    *** ESPERA DE VELA VERDE (llevado a producción tras validar en
    backtest, 29/07/2026 -- ver bitácora del chat) ***
    Una vez que BBW confirma Y pasa el filtro de movimiento, la compra
    YA NO se ejecuta ese mismo día -- se espera al primer día (sin
    límite de tiempo, empezando por ese mismo día inclusive) en que el
    precio muestre una "vela verde": Close > Open Y Close > Close del
    día anterior. Motivo: evitar comprar en plena caída (caso real,
    28/07/2026, dos tickers confirmaron señal en un día con -7% de
    caída). Validado en backtest sobre 102 tickers: retorno 529%->612%,
    max drawdown 6.6%->6.2%, robusto ante concentración (sin el mejor
    trade, sigue reteniendo 79.6% del resultado), y mejora también en
    el período 2025-2026 (el más débil de la estrategia en general).

    Agrega `fecha_toque_vigente` / `precio_toque_vigente`: la fecha y
    precio del toque de banda MÁS RECIENTE que originó el tramo
    "pendiente" actual (se reinicia con cada toque nuevo) -- permite
    mostrar en el dashboard hace cuánto viene esperando una señal.

    Agrega `esperando_vela_verde` / `fecha_confirmacion_bbw_vigente`:
    True en los días en que BBW ya confirmó (pasó el filtro de
    movimiento) pero todavía no apareció una vela verde -- y la fecha
    en que confirmó BBW, para mostrar en el dashboard cuánto tiempo
    lleva esperando la vela verde.

    Agrega `senal_invalidada_por_movimiento`: True en el día puntual
    en que una señal pendiente se descartó por el filtro de movimiento
    (informativo / debug, no se usa para decidir nada más adelante).
    """
    df = df.copy()
    toca_banda_baja = df["Low"] <= df["BB_lower"]

    senal = pd.Series(False, index=df.index)
    fecha_toque_vigente = pd.Series(pd.NaT, index=df.index, dtype="object")
    precio_toque_vigente = pd.Series(np.nan, index=df.index)
    senal_invalidada_por_movimiento = pd.Series(False, index=df.index)
    esperando_vela_verde = pd.Series(False, index=df.index)
    fecha_confirmacion_bbw_vigente = pd.Series(pd.NaT, index=df.index, dtype="object")

    pendiente_bbw = False
    fecha_toque_actual = None
    precio_toque_actual = None
    esperando_verde = False
    fecha_confirmacion_bbw_actual = None

    for i in range(len(df)):
        if toca_banda_baja.iloc[i]:
            pendiente_bbw = True
            fecha_toque_actual = df.index[i]
            precio_toque_actual = df["Close"].iloc[i]
        if pendiente_bbw:
            fecha_toque_vigente.iloc[i] = fecha_toque_actual
            precio_toque_vigente.iloc[i] = precio_toque_actual

        if pendiente_bbw and df["BBW"].iloc[i] > bbw_umbral:
            precio_hoy = df["Close"].iloc[i]
            pct_movido = (
                100 * (precio_hoy - precio_toque_actual) / precio_toque_actual
                if precio_toque_actual else 0.0
            )
            if pct_movido <= pct_movido_maximo:
                if not esperando_verde:  # solo se fija la primera vez -- no se reafirma
                    fecha_confirmacion_bbw_actual = df.index[i]
                esperando_verde = True
            else:
                senal_invalidada_por_movimiento.iloc[i] = True
            pendiente_bbw = False

        if esperando_verde:
            esperando_vela_verde.iloc[i] = True
            fecha_confirmacion_bbw_vigente.iloc[i] = fecha_confirmacion_bbw_actual
            if i > 0:
                close_hoy = df["Close"].iloc[i]
                open_hoy = df["Open"].iloc[i]
                close_ayer = df["Close"].iloc[i - 1]
                if close_hoy > open_hoy and close_hoy > close_ayer:
                    senal.iloc[i] = True
                    esperando_verde = False

    df["senal_confirmada"] = senal
    df["fecha_toque_vigente"] = fecha_toque_vigente
    df["precio_toque_vigente"] = precio_toque_vigente
    df["senal_invalidada_por_movimiento"] = senal_invalidada_por_movimiento
    df["esperando_vela_verde"] = esperando_vela_verde
    df["fecha_confirmacion_bbw_vigente"] = fecha_confirmacion_bbw_vigente
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

    *** AJUSTADO TRAS DATO SUCIO REAL (AAP.BA, 27/07/2026) ***
    Antes esta función combinaba el Low/High de Yahoo con el precio en
    vivo vía min()/max() -- "extendía" el rango del día en vez de
    reemplazarlo. El problema: si Yahoo reportó un Low intradiario
    erróneo (un print fantasma, desalineo de bid/ask en un CEDEAR de
    poca liquidez), ese dato sucio NUNCA se corregía -- min(low_sucio,
    precio_en_vivo) sigue devolviendo el low_sucio si es menor. Pasó en
    la práctica: AAP.BA mostró un "toque de banda inferior" que nunca
    ocurrió, con un Low ~17% por debajo del precio real de la rueda.

    *** AJUSTADO OTRA VEZ -- BUG REAL CON VELA VERDE (CAR.BA, 04/08/2026) ***
    El fix anterior sobreescribía TAMBIÉN el Open con el precio en vivo
    -- eso hace que Open de hoy == Close de hoy SIEMPRE durante una
    corrida intradía, y la condición de vela verde (Close > Open) NUNCA
    puede confirmar en vivo, aunque el precio haya subido de verdad
    respecto a ayer. Ahora se preserva el Open real que Yahoo ya
    publicó para hoy (normalmente disponible temprano en la rueda,
    incluso cuando el resto del día todavía está "sucio" o
    incompleto) -- Close/High/Low se siguen REEMPLAZANDO por completo
    con el precio en vivo (no se combinan con min/max), para no
    reintroducir el bug de AAP.BA.
    """
    df = df.copy()
    if len(df) and df.index[-1].date() == fecha_hoy:
        # Open se preserva tal cual lo tiene Yahoo -- necesario para
        # poder evaluar Close>Open (vela verde) con el Open real del
        # día, no un valor artificial igual al Close.
        df.loc[df.index[-1], ["High", "Low", "Close"]] = precio_en_vivo
    else:
        # No hay fila de hoy todavía (Yahoo no publicó nada) -- no hay
        # forma de saber el Open real sin otra fuente de datos, se usa
        # el precio en vivo también para Open como aproximación (mismo
        # comportamiento que antes en este caso puntual: la vela verde
        # simplemente no puede confirmar hoy con Open==Close, sigue
        # esperando un día donde sí haya diferencia real).
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


def calcular_pnl_con_desglose(precio_entrada: float, precio_salida: float, cantidad: int) -> dict:
    """Igual que calcular_pnl(), pero además devuelve el desglose de
    comisión y derechos de mercado por separado (suma de la pata de
    compra + la pata de venta) -- para las columnas nuevas del
    histórico de operaciones en Sheets."""
    monto_entrada = precio_entrada * cantidad
    monto_salida = precio_salida * cantidad

    comision_compra, ddm_compra = desglosar_costos_compra(monto_entrada)
    comision_venta, ddm_venta = desglosar_costos_venta(monto_salida, intradia=False)

    comision_total = comision_compra + comision_venta
    ddm_total = ddm_compra + ddm_venta

    pnl_pesos = (monto_salida - monto_entrada) - (comision_total + ddm_total)
    pnl_pct = 100 * pnl_pesos / monto_entrada if monto_entrada else 0.0

    return {
        "pnl_pesos": pnl_pesos,
        "pnl_pct": pnl_pct,
        "comision_total": comision_total,
        "ddm_total": ddm_total,
    }
