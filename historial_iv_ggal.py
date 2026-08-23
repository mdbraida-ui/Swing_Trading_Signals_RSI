# -*- coding: utf-8 -*-
"""
============================================================================
 HISTORIAL DE IV -- REGISTRO EN GOOGLE SHEETS + IV RANK
 Pensado para correr vía GitHub Actions + cron-job.org, mismo patrón
 que `chequeo_senales_opciones.py` y el bot diario de producción.
============================================================================

Cada vez que corre: para cada opción de la WATCHLIST, pide su prima en
vivo a la API de IOL, calcula la IV implícita (Black-Scholes para
CALLs, binomial americano para PUTs -- ver `iv_solver_ggal.py`, cuyas
funciones están duplicadas acá para que este script sea autocontenido),
la compara contra la volatilidad realizada de un período equivalente a
los días a vencimiento, y agrega una fila a la pestaña "Historial IV".

Con el correr de los días/semanas, esta pestaña se convierte en la
serie histórica de IV necesaria para calcular IV Rank / IV Percentile
(cuánto más alta o baja está la IV de HOY respecto a su propio
historial) -- el criterio que de verdad usan los traders de opciones
profesionales, más preciso que solo comparar contra la volatilidad
realizada.

--------------------------------------------------------------------------
WATCHLIST -- EDITAR A MANO CON LAS OPCIONES A SEGUIR
--------------------------------------------------------------------------
Todavía no tenemos extracción automática de strike/vencimiento desde el
símbolo de IOL (el campo `descripcionTitulo` vino vacío en la prueba
del chat) -- por eso se completa a mano acá. Cuando se confirme el
formato del símbolo, se puede automatizar.

--------------------------------------------------------------------------
VARIABLES DE ENTORNO NECESARIAS (GitHub Secrets)
--------------------------------------------------------------------------
  - IOL_USUARIO, IOL_CONTRASENA: credenciales de IOL (el token vive
    solo 15 minutos, así que el script se loguea de cero en cada corrida).
  - GOOGLE_SHEETS_CREDENTIALS_JSON, GOOGLE_SHEET_ID: mismo patrón que
    `chequeo_senales_opciones.py`.
============================================================================
"""

import os
import json
import math
import datetime
import requests
import pandas as pd
import yfinance as yf
import gspread
from google.oauth2.service_account import Credentials

# ============================================================================
# WATCHLIST -- EDITAR ACÁ
# ============================================================================
WATCHLIST_OPCIONES = [
    # "bucket": etiqueta ESTANDARIZADA que se mantiene fija mes a mes,
    # aunque el símbolo real que la representa cambie al rolear el
    # vencimiento -- el IV Rank se calcula sobre el bucket, no sobre el
    # símbolo literal (que muere junto con el contrato). Convención
    # sugerida: "{CALL|PUT}_{ATM|strike aproximado}_{tenor aproximado}D"
    # {"simbolo": "GFGC7000AG", "strike": 7000.0, "vencimiento": "2026-08-21",
    #  "tipo": "call", "bucket": "CALL_ATM_30D"},
    # {"simbolo": "GFGV7500AG", "strike": 7500.0, "vencimiento": "2026-08-21",
    #  "tipo": "put", "bucket": "PUT_ATM_30D"},
]

TICKER_SUBYACENTE = "GGAL.BA"
SIMBOLO_SUBYACENTE_IOL = "GGAL"
MERCADO_IOL = "bcba"
TASA_LIBRE_RIESGO = 0.40  # ajustar según caución/BADLAR vigente
NOMBRE_PESTANA = "Historial IV"
ENCABEZADOS = ["fecha_hora", "bucket", "simbolo_opcion", "tipo", "strike", "vencimiento",
               "dias_a_vencimiento", "precio_subyacente", "precio_opcion",
               "iv", "volatilidad_realizada", "vrp", "vrp_relativo_pct", "iv_rank_pct"]


# ============================================================================
# BLACK-SCHOLES / BINOMIAL (duplicado de iv_solver_ggal.py -- autocontención)
# ============================================================================

def _norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def precio_black_scholes(S, K, T, r, sigma, tipo="call"):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if tipo == "call" else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if tipo == "call":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def vega_black_scholes(S, K, T, r, sigma):
    if T <= 0 or sigma <= 0:
        return 0.0
    d1 = (math.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * math.sqrt(T))
    return S * _norm_pdf(d1) * math.sqrt(T)


def calcular_iv(precio_mercado, S, K, T, r, tipo="call", tolerancia=1e-4, max_iteraciones=100):
    intrinseco = max(S - K, 0.0) if tipo == "call" else max(K - S, 0.0)
    if precio_mercado < intrinseco - tolerancia:
        return None
    sigma = 0.30
    for _ in range(max_iteraciones):
        precio_calc = precio_black_scholes(S, K, T, r, sigma, tipo)
        vega = vega_black_scholes(S, K, T, r, sigma)
        diff = precio_calc - precio_mercado
        if abs(diff) < tolerancia:
            return sigma
        if vega < 1e-8:
            break
        sigma = max(sigma - diff / vega, 0.01)
    sigma_bajo, sigma_alto = 0.0001, 5.0
    p_bajo = precio_black_scholes(S, K, T, r, sigma_bajo, tipo)
    p_alto = precio_black_scholes(S, K, T, r, sigma_alto, tipo)
    if not (p_bajo <= precio_mercado <= p_alto):
        return None
    for _ in range(200):
        sigma_medio = (sigma_bajo + sigma_alto) / 2
        p_medio = precio_black_scholes(S, K, T, r, sigma_medio, tipo)
        if abs(p_medio - precio_mercado) < tolerancia:
            return sigma_medio
        if p_medio < precio_mercado:
            sigma_bajo = sigma_medio
        else:
            sigma_alto = sigma_medio
    return None


def precio_binomial_americana(S, K, T, r, sigma, tipo="call", n_pasos=150):
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if tipo == "call" else max(K - S, 0.0)
    dt = T / n_pasos
    u = math.exp(sigma * math.sqrt(dt))
    d = 1 / u
    p = (math.exp(r * dt) - d) / (u - d)
    descuento = math.exp(-r * dt)
    valores = []
    for j in range(n_pasos + 1):
        precio_nodo = S * (u ** j) * (d ** (n_pasos - j))
        valores.append(max(precio_nodo - K, 0.0) if tipo == "call" else max(K - precio_nodo, 0.0))
    for i in range(n_pasos - 1, -1, -1):
        nuevos = []
        for j in range(i + 1):
            valor_cont = descuento * (p * valores[j + 1] + (1 - p) * valores[j])
            precio_nodo = S * (u ** j) * (d ** (i - j))
            valor_ejerc = max(precio_nodo - K, 0.0) if tipo == "call" else max(K - precio_nodo, 0.0)
            nuevos.append(max(valor_cont, valor_ejerc))
        valores = nuevos
    return valores[0]


def calcular_iv_americana(precio_mercado, S, K, T, r, tipo="put", n_pasos=100,
                           tolerancia=1e-3, max_iteraciones=50):
    intrinseco = max(S - K, 0.0) if tipo == "call" else max(K - S, 0.0)
    if precio_mercado < intrinseco - tolerancia:
        return None
    sigma_bajo, sigma_alto = 0.0001, 5.0
    p_bajo = precio_binomial_americana(S, K, T, r, sigma_bajo, tipo, n_pasos)
    p_alto = precio_binomial_americana(S, K, T, r, sigma_alto, tipo, n_pasos)
    if not (p_bajo <= precio_mercado <= p_alto):
        return None
    sigma_medio = None
    for _ in range(max_iteraciones):
        sigma_medio = (sigma_bajo + sigma_alto) / 2
        p_medio = precio_binomial_americana(S, K, T, r, sigma_medio, tipo, n_pasos)
        if abs(p_medio - precio_mercado) < tolerancia:
            return sigma_medio
        if p_medio < precio_mercado:
            sigma_bajo = sigma_medio
        else:
            sigma_alto = sigma_medio
    return sigma_medio


def calcular_volatilidad_realizada(precios_cierre, periodos_por_año=252):
    if len(precios_cierre) < 2:
        return None
    retornos = [math.log(precios_cierre[i] / precios_cierre[i - 1])
                for i in range(1, len(precios_cierre)) if precios_cierre[i - 1] > 0]
    n = len(retornos)
    if n < 2:
        return None
    media = sum(retornos) / n
    varianza = sum((r - media) ** 2 for r in retornos) / (n - 1)
    return math.sqrt(varianza) * math.sqrt(periodos_por_año)


def comparar_vrp(iv: float, volatilidad_realizada: float) -> dict:
    """VRP = IV - volatilidad realizada. Umbrales de la señal
    PROVISORIOS -- recalibrar en cuanto haya ~20-30 observaciones
    reales acumuladas en 'Historial IV'."""
    if iv is None or volatilidad_realizada is None:
        return {"vrp": None, "señal": "datos insuficientes"}
    vrp = iv - volatilidad_realizada
    vrp_relativo = vrp / volatilidad_realizada if volatilidad_realizada > 0 else None

    if vrp_relativo is not None and vrp_relativo > 0.30:
        señal = "IV muy por encima de la realizada -- candidato fuerte a vender prima"
    elif vrp_relativo is not None and vrp_relativo > 0.10:
        señal = "IV moderadamente por encima -- candidato a vender prima"
    elif vrp_relativo is not None and vrp_relativo > -0.10:
        señal = "IV y volatilidad realizada similares -- sin ventaja clara de vender"
    else:
        señal = "IV por debajo de la realizada -- no vender prima acá, considerar comprar volatilidad"

    return {"iv": iv, "volatilidad_realizada": volatilidad_realizada,
            "vrp": vrp, "vrp_relativo_pct": 100 * vrp_relativo if vrp_relativo is not None else None,
            "señal": señal}


# ============================================================================
# API DE IOL
# ============================================================================

def autenticar_iol(usuario, contrasena):
    resp = requests.post("https://api.invertironline.com/token",
                          data={"username": usuario, "password": contrasena, "grant_type": "password"})
    resp.raise_for_status()
    return "Bearer " + resp.json()["access_token"]


def cotizacion_iol(token, simbolo, mercado=MERCADO_IOL):
    headers = {"Authorization": token}
    url = f"https://api.invertironline.com/api/{mercado}/Titulos/{simbolo}/cotizacion"
    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# GOOGLE SHEETS
# ============================================================================

def conectar_sheets():
    credenciales_json = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS_JSON"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credenciales = Credentials.from_service_account_info(credenciales_json, scopes=scopes)
    cliente = gspread.authorize(credenciales)
    return cliente.open_by_key(os.environ["GOOGLE_SHEETS_ID_OPCIONES"])


def obtener_o_crear_pestana(planilla):
    try:
        return planilla.worksheet(NOMBRE_PESTANA)
    except gspread.exceptions.WorksheetNotFound:
        pestana = planilla.add_worksheet(title=NOMBRE_PESTANA, rows=2000, cols=len(ENCABEZADOS))
        pestana.append_row(ENCABEZADOS)
        return pestana


def leer_historial_iv(pestana, bucket):
    """Trae todas las IV ya registradas para un BUCKET (no un símbolo
    literal -- ver nota en WATCHLIST_OPCIONES) -- así el historial
    persiste a través de los rolls de vencimiento en vez de cortarse
    cada vez que un contrato puntual vence. Usado para el cálculo de
    IV Rank."""
    valores = pestana.get_all_values()
    ivs = []
    for fila in valores[1:]:
        # columna 1 = bucket, columna 9 = iv (con la columna nueva insertada)
        if len(fila) >= 10 and fila[1] == bucket and fila[9]:
            try:
                ivs.append(float(fila[9]))
            except ValueError:
                continue
    return ivs


def calcular_iv_rank(historial_ivs: list, iv_actual: float) -> float:
    """
    IV Rank simple: percentil de `iv_actual` dentro de `historial_ivs`
    (0-100). Devuelve None si no hay suficiente historial (menos de 10
    observaciones -- con menos que eso, el percentil no es confiable).
    """
    if len(historial_ivs) < 10 or iv_actual is None:
        return None
    menores = sum(1 for iv in historial_ivs if iv <= iv_actual)
    return 100 * menores / len(historial_ivs)


# ============================================================================
# MAIN
# ============================================================================

def main():
    if not WATCHLIST_OPCIONES:
        print("WATCHLIST_OPCIONES está vacía -- agregá al menos una opción para seguir.")
        return

    token_iol = autenticar_iol(os.environ["IOL_USUARIO"], os.environ["IOL_PASSWORD"])
    cotizacion_subyacente = cotizacion_iol(token_iol, SIMBOLO_SUBYACENTE_IOL)
    S = cotizacion_subyacente["ultimoPrecio"]

    # Volatilidad realizada base (se recorta por opción según T más abajo)
    df_hist = yf.download(TICKER_SUBYACENTE, period="1y", interval="1d", progress=False, auto_adjust=True)
    if isinstance(df_hist.columns, pd.MultiIndex):
        df_hist.columns = df_hist.columns.get_level_values(0)
    precios_diarios = df_hist["Close"].tolist()

    planilla = conectar_sheets()
    pestana = obtener_o_crear_pestana(planilla)
    hoy = datetime.date.today()

    for opcion in WATCHLIST_OPCIONES:
        simbolo = opcion["simbolo"]
        bucket = opcion["bucket"]
        K = opcion["strike"]
        tipo = opcion["tipo"]
        fecha_venc = datetime.datetime.strptime(opcion["vencimiento"], "%Y-%m-%d").date()
        dias_a_venc = (fecha_venc - hoy).days
        if dias_a_venc <= 0:
            print(f"{simbolo}: ya venció, se saltea.")
            continue
        T = dias_a_venc / 365

        cot_opcion = cotizacion_iol(token_iol, simbolo)
        precio_opcion = cot_opcion["ultimoPrecio"]
        if not precio_opcion or precio_opcion <= 0:
            print(f"{simbolo}: sin precio válido, se saltea.")
            continue

        if tipo == "call":
            iv = calcular_iv(precio_opcion, S, K, T, TASA_LIBRE_RIESGO, tipo="call")
        else:
            iv = calcular_iv_americana(precio_opcion, S, K, T, TASA_LIBRE_RIESGO, tipo="put")

        # volatilidad realizada con una ventana equivalente a los días a vencimiento
        ventana_dias = min(dias_a_venc, len(precios_diarios) - 1)
        precios_ventana = precios_diarios[-ventana_dias:] if ventana_dias > 1 else precios_diarios
        vol_realizada = calcular_volatilidad_realizada(precios_ventana)

        vrp = (iv - vol_realizada) if (iv is not None and vol_realizada is not None) else None
        vrp_relativo = (100 * vrp / vol_realizada) if (vrp is not None and vol_realizada) else None

        historial_previo = leer_historial_iv(pestana, bucket)
        iv_rank = calcular_iv_rank(historial_previo, iv)

        fila = [
            str(datetime.datetime.now()),
            bucket,
            simbolo,
            tipo,
            K,
            opcion["vencimiento"],
            dias_a_venc,
            round(S, 2),
            round(precio_opcion, 2),
            round(iv, 4) if iv is not None else "",
            round(vol_realizada, 4) if vol_realizada is not None else "",
            round(vrp, 4) if vrp is not None else "",
            round(vrp_relativo, 1) if vrp_relativo is not None else "",
            round(iv_rank, 1) if iv_rank is not None else "sin historial suficiente",
        ]
        pestana.append_row(fila)
        print(f"Registrado [{bucket}]: {fila}")


if __name__ == "__main__":
    main()
