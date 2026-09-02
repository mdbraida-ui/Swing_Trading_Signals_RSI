# -*- coding: utf-8 -*-
"""
============================================================================
 BOT DE OPCIONES -- ENTRADA EMA18 / SALIDA PATRÓN DE VELA
 Producción real -- CTA REAL DE IOL, sin sandbox (decisión del usuario)
============================================================================

Reusa:
  - `iol_client.py` (IOLClient) TAL CUAL, sin modificar -- ya es
    genérico (comprar_mercado/vender_mercado funcionan con cualquier
    símbolo, no asumen que sea una acción). Confirmado: `.replace(".BA","")`
    es un no-op para símbolos de opciones (no tienen ese sufijo).
  - `cadena_opciones_scraper.py` para encontrar el strike ATM real con
    liquidez -- no se adivina el strike, se pide la cadena completa y
    se elige el más cercano al spot ENTRE LOS QUE TIENEN OPERATORIA.
  - Misma lógica de señal que `cruce_ema4_18_multiticker.py` (mecánica
    B) -- la que mejor rindió de las 4 probadas sobre COME: $1.095.813
    simulado en opciones, contra $362.574 de la mecánica de patrón de
    vela usada en la primera versión de este bot.

--------------------------------------------------------------------------
HORARIO DE MERCADO (Argentina): 10:30 a 17:00 -- ver el workflow de
GitHub Actions para el detalle de la ventana de corridas.

--------------------------------------------------------------------------
REGLA DE UNA SOLA POSICIÓN A LA VEZ (confirmado con el usuario)
--------------------------------------------------------------------------
Si hay una posición abierta (en cualquier ticker activo), NO se evalúan
entradas nuevas -- solo se chequea la condición de salida de la
posición ya abierta. El estado de la posición vive en la pestaña
"Posicion Activa Opciones" de Sheets (una sola fila, o vacía si no hay
posición) -- necesario porque GitHub Actions no mantiene memoria entre
corridas.

--------------------------------------------------------------------------
VENCIMIENTO -- MANUAL (decisión del usuario)
--------------------------------------------------------------------------
Se lee de `config_bot_opciones.csv`, columna `vencimiento` -- HAY QUE
ACTUALIZARLA A MANO cuando la serie vigente esté por vencer. El bot NO
detecta ni rolea vencimientos solo.

--------------------------------------------------------------------------
*** SIN CONFIRMAR AL 100% -- LEER ANTES DE ACTIVAR ***
--------------------------------------------------------------------------
Nunca se probó `/api/v2/operar/Comprar` ni `/Vender` con un símbolo de
OPCIÓN real (solo se probó con acciones, según el propio iol_client.py).
El propio archivo recomienda probar en sandbox antes de ir a cuenta
real -- el usuario decidió saltear ese paso. Primera corrida real:
prestar mucha atención a los mensajes de Telegram y al log de GitHub
Actions, y estar disponible para intervenir manualmente si algo no
sale como se espera.

Estrategia SIN stop-loss (así fue definida) -- una posición mal cerrada
por un error de símbolo/vencimiento puede quedar abierta más tiempo del
previsto sin ningún freno automático de pérdida.
============================================================================
"""

import os
import csv
import json
import sys
import datetime
import pandas as pd
import yfinance as yf
import ta
import gspread
from google.oauth2.service_account import Credentials

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from iol_client import IOLClient
import telegram_notifier as tg

OPCIONES_POR_CONTRATO_DEFAULT = 100  # fallback si el config no trae la columna
    # 1 contrato = N opciones -- CONFIRMADO que varía por ticker: YPFD
    # es 1000 (ajustado por el split 10:1 del 04/08/2026), el resto
    # (GGAL, PAMP, COME) sigue en 100. Se lee de la columna
    # `opciones_por_contrato` de config_bot_opciones.csv, por ticker --
    # esta constante es solo el valor de respaldo si esa columna
    # faltara para algún ticker.


def obtener_multiplicador_contrato(config_ticker: dict) -> int:
    try:
        return int(config_ticker.get("opciones_por_contrato", OPCIONES_POR_CONTRATO_DEFAULT))
    except (ValueError, TypeError):
        return OPCIONES_POR_CONTRATO_DEFAULT
GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEETS_ID_OPCIONES", "")
NOMBRE_PESTANA_POSICION = "Posicion Activa Opciones"
NOMBRE_PESTANA_HISTORICO = "Historico Ordenes Opciones"
NOMBRE_PESTANA_PENDIENTES = "Ordenes Pendientes Opciones"
NOMBRE_PESTANA_MANUALES = "Ordenes Manuales Opciones"

ENCABEZADOS_POSICION = ["ticker", "tipo", "simbolo_opcion", "strike", "fecha_entrada",
                         "precio_entrada", "cantidad_contratos", "numero_operacion_compra"]
ENCABEZADOS_HISTORICO = ["fecha_entrada", "fecha_salida", "ticker", "tipo", "simbolo_opcion",
                          "strike", "precio_entrada", "precio_salida", "cantidad_contratos",
                          "pnl_pesos", "pnl_pct", "numero_operacion_compra", "numero_operacion_venta"]
ENCABEZADOS_PENDIENTES = ["fecha", "ticker", "tipo_orden", "simbolo_opcion", "numero_operacion",
                           "cantidad_contratos", "detalle"]
ENCABEZADOS_MANUALES = ["accion", "ticker", "tipo", "simbolo_opcion", "cantidad_contratos",
                         "estado", "fecha_procesada", "detalle"]
# accion: "comprar" o "vender". estado: dejar VACÍO para que el bot la
# procese en la próxima corrida -- el bot la marca como "procesada" o
# "error" después de intentarla, y NUNCA vuelve a tocar una fila que ya
# tenga estado (para no reenviar la misma orden dos veces).


# ============================================================================
# CONFIG
# ============================================================================

def leer_config(ruta: str = RUTA_CONFIG) -> list:
    filas = []
    with open(ruta, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        for fila in lector:
            if fila.get("activo", "").strip().upper() == "SI":
                filas.append(fila)
    return filas


# ============================================================================
# SEÑAL (idéntica a entrada_ema18_salida_patron_vela_multiticker.py)
# ============================================================================

def descargar_datos_1h(ticker: str, periodo: str = "60d") -> pd.DataFrame:
    """60 días alcanza de sobra para el EMA18 (warm-up) -- no hace
    falta bajar 730 días para una corrida en vivo."""
    df = yf.download(ticker, period=periodo, interval="1h", progress=False, auto_adjust=True)
    if df.empty:
        raise ValueError(f"Sin datos 1h para {ticker}")
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df["EMA4"] = ta.trend.EMAIndicator(df["Close"], window=4).ema_indicator()
    df["EMA18"] = ta.trend.EMAIndicator(df["Close"], window=EMA_REFERENCIA).ema_indicator()
    return df


def evaluar_cruce(df: pd.DataFrame) -> str:
    """
    MECÁNICA B -- EMA4/EMA18 puro (la que mejor rindió de las 4
    probadas: $1.095.813 simulado en opciones sobre COME, contra
    $362.574 de la mecánica de patrón de vela usada antes).

    El mismo cruce abre un lado y cierra el otro -- evaluado en la
    última vela cerrada. Devuelve 'alcista', 'bajista' o None.
    """
    if len(df) < 2:
        return None
    actual, previa = df.iloc[-1], df.iloc[-2]
    if pd.isna(actual["EMA4"]) or pd.isna(actual["EMA18"]) or pd.isna(previa["EMA4"]) or pd.isna(previa["EMA18"]):
        return None
    if previa["EMA4"] <= previa["EMA18"] and actual["EMA4"] > actual["EMA18"]:
        return "alcista"
    if previa["EMA4"] >= previa["EMA18"] and actual["EMA4"] < actual["EMA18"]:
        return "bajista"
    return None


# ============================================================================
# SELECCIÓN DE STRIKE ATM (con datos reales de la cadena, no adivinado)
# ============================================================================

RUTA_CADENA_VIGENTE = "cadena_vigente.csv"


def leer_cadena_vigente(ticker_yahoo: str, tipo: str, ruta: str = RUTA_CADENA_VIGENTE) -> list:
    """Lee la cadena real (mantenida a mano por el usuario, no generada
    ni adivinada) y devuelve la lista de {"strike":..., "simbolo":...}
    para ese ticker+tipo, ordenada por strike ascendente."""
    filas = []
    with open(ruta, newline="", encoding="utf-8") as f:
        lector = csv.DictReader(f)
        for fila in lector:
            if fila["ticker_yahoo"] == ticker_yahoo and fila["tipo"] == tipo:
                filas.append({"strike": float(fila["strike"]), "simbolo": fila["simbolo"]})
    filas.sort(key=lambda f: f["strike"])
    return filas


def elegir_strike_inicial(cliente_iol: IOLClient, config_ticker: dict, tipo: str) -> dict:
    """
    ENTRADA INICIAL -- distinta de la regla de roleo. Elige la base
    INMEDIATA superior al spot (CALL) o inmediata inferior (PUT) --
    no "la más cercana en cualquier dirección". Se asume que, al estar
    pegada al spot (ATM), va a tener operatoria real -- si por algún
    motivo no la tuviera, cae a buscar un escalón más lejos como red
    de seguridad.
    """
    ticker_yahoo = config_ticker["ticker_yahoo"]
    cadena = leer_cadena_vigente(ticker_yahoo, tipo)
    if not cadena:
        print(f"{ticker_yahoo} ({tipo}): sin strikes cargados en {RUTA_CADENA_VIGENTE}.")
        return None

    spot = cliente_iol.obtener_precio(ticker_yahoo, mercado="bCBA")

    if tipo == "call":
        candidatos = [f for f in cadena if f["strike"] > spot]  # inmediata superior primero
        candidatos.sort(key=lambda f: f["strike"])
    else:
        candidatos = [f for f in cadena if f["strike"] < spot]  # inmediata inferior primero
        candidatos.sort(key=lambda f: -f["strike"])

    for candidato in candidatos:
        try:
            datos = cliente_iol._get(f"/api/v2/bCBA/Titulos/{candidato['simbolo']}/Cotizacion")
        except Exception:
            continue
        if datos.get("cantidadOperaciones", 0) > 0:
            return {"simbolo": candidato["simbolo"], "strike": candidato["strike"],
                    "precio_opcion": datos.get("ultimoPrecio", 0), "spot": spot}

    print(f"{ticker_yahoo} ({tipo}): ninguna base cercana al spot tiene operatoria hoy.")
    return None


def determinar_strike_roleo(cadena: list, strike_actual: float, spot: float, tipo: str) -> dict:
    """
    REGLA DE ROLEO (confirmada con 2 ejemplos concretos del usuario):
      CALL: el nuevo strike es el MAYOR strike de la cadena que sea a
      la vez > strike_actual (avanza, no retrocede) Y <= spot (nunca
      más alto que el precio actual). Puede saltar varios escalones si
      el precio se movió rápido.
      PUT (espejo): el MENOR strike que sea < strike_actual Y >= spot.

    Devuelve el candidato elegido o None si todavía no corresponde
    rolear (el precio no alcanzó el próximo escalón).
    """
    if tipo == "call":
        superiores = [f for f in cadena if f["strike"] > strike_actual]
        if not superiores:
            return None
        siguiente_escalon = min(f["strike"] for f in superiores)
        if spot < siguiente_escalon:
            return None  # todavía no llegó al próximo escalón, no rolea

        candidatos_validos = [f for f in cadena if strike_actual < f["strike"] <= spot]
        if not candidatos_validos:
            return None
        return max(candidatos_validos, key=lambda f: f["strike"])
    else:
        inferiores = [f for f in cadena if f["strike"] < strike_actual]
        if not inferiores:
            return None
        siguiente_escalon = max(f["strike"] for f in inferiores)
        if spot > siguiente_escalon:
            return None

        candidatos_validos = [f for f in cadena if spot <= f["strike"] < strike_actual]
        if not candidatos_validos:
            return None
        return min(candidatos_validos, key=lambda f: f["strike"])  # ningún candidato cercano tiene operatoria hoy


# ============================================================================
# GOOGLE SHEETS -- ESTADO
# ============================================================================

def conectar_sheets():
    credenciales_json = json.loads(os.environ["GOOGLE_SHEETS_CREDENTIALS_JSON"])
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credenciales = Credentials.from_service_account_info(credenciales_json, scopes=scopes)
    cliente = gspread.authorize(credenciales)
    return cliente.open_by_key(GOOGLE_SHEET_ID)


def obtener_o_crear_pestana(planilla, nombre, encabezados):
    try:
        return planilla.worksheet(nombre)
    except gspread.exceptions.WorksheetNotFound:
        pestana = planilla.add_worksheet(title=nombre, rows=1000, cols=len(encabezados))
        pestana.append_row(encabezados)
        return pestana


def leer_posiciones_activas(pestana) -> dict:
    """Devuelve {ticker: {...datos...}} -- una entrada por ticker con
    posición abierta. Como cada ticker solo puede tener CALL o PUT
    (nunca los dos a la vez), esta estructura por ticker ya impone esa
    regla de forma natural."""
    valores = pestana.get_all_values()
    posiciones = {}
    for fila in valores[1:]:
        if len(fila) >= len(ENCABEZADOS_POSICION) and fila[0]:
            datos = dict(zip(ENCABEZADOS_POSICION, fila))
            posiciones[datos["ticker"]] = datos
    return posiciones


def agregar_posicion(pestana, posicion: dict):
    pestana.append_row([posicion.get(c, "") for c in ENCABEZADOS_POSICION])


def quitar_posicion(pestana, ticker: str):
    """Reescribe la pestaña completa SIN la fila de ese ticker,
    preservando las posiciones abiertas de los demás tickers."""
    posiciones = leer_posiciones_activas(pestana)
    posiciones.pop(ticker, None)
    pestana.clear()
    pestana.append_row(ENCABEZADOS_POSICION)
    for datos in posiciones.values():
        pestana.append_row([datos.get(c, "") for c in ENCABEZADOS_POSICION])


def registrar_historico(pestana, fila_dict: dict):
    pestana.append_row([fila_dict.get(c, "") for c in ENCABEZADOS_HISTORICO])


def registrar_pendiente(pestana, fila_dict: dict):
    pestana.append_row([fila_dict.get(c, "") for c in ENCABEZADOS_PENDIENTES])


def leer_ordenes_manuales_pendientes(pestana) -> list:
    """Devuelve las filas de la pestaña de órdenes manuales que TODAVÍA
    no tienen estado (columna 'estado' vacía) -- son las que hay que
    procesar en esta corrida. Cada elemento incluye su número de fila
    real en la pestaña (base 1, contando el encabezado), para poder
    marcarla después sin tocar las demás."""
    valores = pestana.get_all_values()
    pendientes = []
    for i, fila in enumerate(valores[1:], start=2):  # fila 1 = encabezado
        if len(fila) < len(ENCABEZADOS_MANUALES):
            fila = fila + [""] * (len(ENCABEZADOS_MANUALES) - len(fila))
        datos = dict(zip(ENCABEZADOS_MANUALES, fila))
        if datos["accion"].strip() and not datos["estado"].strip():
            datos["_fila_numero"] = i
            pendientes.append(datos)
    return pendientes


def marcar_orden_manual(pestana, fila_numero: int, estado: str, detalle: str = ""):
    """Escribe 'estado' y 'detalle' en la fila puntual (columnas F y H
    de ENCABEZADOS_MANUALES) -- así la fila queda procesada y el bot
    nunca la vuelve a tocar en corridas futuras."""
    col_estado = ENCABEZADOS_MANUALES.index("estado") + 1  # gspread es base 1
    col_fecha = ENCABEZADOS_MANUALES.index("fecha_procesada") + 1
    col_detalle = ENCABEZADOS_MANUALES.index("detalle") + 1
    pestana.update_cell(fila_numero, col_estado, estado)
    pestana.update_cell(fila_numero, col_fecha, str(datetime.datetime.now()))
    pestana.update_cell(fila_numero, col_detalle, detalle)


NOMBRE_PESTANA_LOCK = "Lock Opciones"
TIMEOUT_LOCK_MINUTOS = 15  # mismo valor que el bot de acciones -- de sobra


def intentar_tomar_lock(planilla, nombre_rutina: str) -> bool:
    """
    LOCK CONTRA CORRIDAS SUPERPUESTAS -- calcado del mecanismo real de
    `sheets_dashboard.py` (bot de acciones), que existe por un bug real
    en producción el 19/08/2026: dos corridas casi simultáneas (cron
    interno de GitHub + cron-job.org externo, dentro de la misma
    ventana) pisaron el estado escrito por la otra. Este bot de
    opciones NUNCA tuvo este mecanismo hasta ahora -- se agrega con
    pestaña propia ("Lock Opciones"), independiente del lock del bot de
    acciones, para no interferir entre los dos.

    Devuelve True si se tomó el lock (hay que liberarlo en un finally).
    Si no se pudo tomar (otra corrida en curso) devuelve False -- en
    ese caso el caller no debe hacer NADA más y debe salir enseguida.
    Si hay cualquier error accediendo a la pestaña, falla "abierto"
    (devuelve True) para no bloquear el bot entero por un problema
    puntual de esa pestaña.
    """
    try:
        ws = obtener_o_crear_pestana(planilla, NOMBRE_PESTANA_LOCK, ["Rutina", "Desde"])
    except Exception as e:
        print(f"[lock] no se pudo acceder a la pestaña de lock, se sigue SIN lock: {e}")
        return True

    try:
        valores = ws.get_all_values()
        if len(valores) >= 2 and len(valores[1]) >= 2 and valores[1][0]:
            rutina_actual, timestamp_str = valores[1][0], valores[1][1]
            try:
                timestamp = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                minutos_transcurridos = (datetime.datetime.now() - timestamp).total_seconds() / 60
            except ValueError:
                minutos_transcurridos = TIMEOUT_LOCK_MINUTOS + 1

            if minutos_transcurridos < TIMEOUT_LOCK_MINUTOS:
                print(f"[lock] ocupado por '{rutina_actual}' desde hace {minutos_transcurridos:.1f} min "
                      f"-- se salta esta corrida de '{nombre_rutina}'")
                return False
            print(f"[lock] lock de '{rutina_actual}' abandonado hace {minutos_transcurridos:.1f} min -- se toma igual")

        ws.update(range_name="A1:B2",
                  values=[["Rutina", "Desde"], [nombre_rutina, datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")]])
        return True
    except Exception as e:
        print(f"[lock] error al tomar el lock, se sigue SIN lock: {e}")
        return True


def liberar_lock(planilla):
    """SIEMPRE se llama en un finally -- se libera incluso si la
    corrida explota a mitad de camino."""
    try:
        ws = planilla.worksheet(NOMBRE_PESTANA_LOCK)
        ws.update(range_name="A1:B2", values=[["Rutina", "Desde"], ["", ""]])
    except Exception as e:
        print(f"[lock] error al liberar el lock: {e}")


def _verificar_multiplicador_sospechoso(ticker: str, precio_visto_en_vivo: float,
                                          precio_ejecutado: float, multiplicador_usado: int):
    """
    *** RED DE SEGURIDAD -- multiplicador de contrato nunca confirmado
    contra una orden real ***
    Si el supuesto de `opciones_por_contrato` está mal, `precio_ejecutado`
    (montoOperacion / cantidad_enviada, calculado por IOL) va a salir
    aproximadamente `multiplicador_usado` veces más grande o más chico
    que el precio que vimos en vivo segundos antes de mandar la orden --
    un movimiento de precio normal en ese lapso jamás explicaría una
    diferencia de ese orden de magnitud. Si se detecta, avisa FUERTE por
    Telegram -- la orden ya se mandó (no se puede deshacer desde acá),
    pero hay que revisarla a mano de inmediato.
    """
    if precio_visto_en_vivo <= 0 or precio_ejecutado <= 0:
        return
    ratio = precio_ejecutado / precio_visto_en_vivo
    # tolerancia generosa (0.5x a 2x) para movimiento de precio normal --
    # cualquier cosa fuera de ese rango es sospechosa de un problema de
    # multiplicador, no de movimiento de mercado
    if ratio < 0.5 or ratio > 2.0:
        tg.notificar_error(
            f"POSIBLE ERROR DE MULTIPLICADOR -- {ticker}",
            f"Precio visto en vivo antes de operar: ${precio_visto_en_vivo:,.2f}\n"
            f"Precio ejecutado según IOL: ${precio_ejecutado:,.2f}\n"
            f"Ratio: {ratio:.1f}x (multiplicador usado: {multiplicador_usado})\n\n"
            f"Esto puede significar que el supuesto de "
            f"'{multiplicador_usado} opciones por contrato' está MAL para "
            f"este ticker -- revisar la orden en IOL manualmente ANTES de "
            f"que el bot vuelva a operar {ticker}."
        )
        print(f"[ALERTA] Ratio sospechoso de multiplicador para {ticker}: {ratio:.1f}x")


# ============================================================================
# MAIN
# ============================================================================

def determinar_modo_rutina() -> str:
    """
    'apertura', 'control' o 'cierre'. Prioridad:
      1) Variable de entorno MODO_RUTINA (para forzar manual vía
         workflow_dispatch, igual que en bot_bb_touch_diario.py).
      2) Si no está seteada, se infiere por la hora actual en
         Argentina -- apertura=10:40, cierre=17:40, cualquier otra
         hora=control.
    """
    modo_forzado = os.environ.get("MODO_RUTINA", "").strip().lower()
    if modo_forzado in ("apertura", "control", "cierre"):
        return modo_forzado

    from zoneinfo import ZoneInfo
    ahora = datetime.datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    hora_min = ahora.hour * 60 + ahora.minute
    if abs(hora_min - (10 * 60 + 40)) <= 4:  # margen de 4 min por si el cron se atrasa
        return "apertura"
    if abs(hora_min - (17 * 60 + 40)) <= 4:
        return "cierre"
    return "control"


def _ejecutar_bot(planilla):
    modo = determinar_modo_rutina()
    print(f"=== Corrida en modo: {modo} ===")

    if modo == "apertura":
        tg.notificar_bot_conectado(rutina="opciones - apertura")

    usuario_iol = os.environ["IOL_USUARIO"]
    password_iol = os.environ["IOL_PASSWORD"]
    cliente_iol = IOLClient(usuario_iol, password_iol)

    pestana_posicion = obtener_o_crear_pestana(planilla, NOMBRE_PESTANA_POSICION, ENCABEZADOS_POSICION)
    pestana_historico = obtener_o_crear_pestana(planilla, NOMBRE_PESTANA_HISTORICO, ENCABEZADOS_HISTORICO)
    pestana_pendientes = obtener_o_crear_pestana(planilla, NOMBRE_PESTANA_PENDIENTES, ENCABEZADOS_PENDIENTES)
    pestana_manuales = obtener_o_crear_pestana(planilla, NOMBRE_PESTANA_MANUALES, ENCABEZADOS_MANUALES)

    posiciones_activas = leer_posiciones_activas(pestana_posicion)
    config = leer_config()
    config_por_ticker = {c["ticker_yahoo"]: c for c in config}
    hoy_str = str(datetime.datetime.now())

    # ------------------------------------------------------------------
    # PASO 0: procesar órdenes MANUALES pendientes -- se ejecutan ANTES
    # que cualquier señal automática, para priorizar tu intervención
    # (por ejemplo, completar a mano un roleo que quedó a medio camino).
    # ------------------------------------------------------------------
    for orden in leer_ordenes_manuales_pendientes(pestana_manuales):
        ticker = orden["ticker"]
        tipo = orden["tipo"]
        simbolo_opcion = orden["simbolo_opcion"]
        try:
            cantidad_contratos = int(orden["cantidad_contratos"])
        except ValueError:
            marcar_orden_manual(pestana_manuales, orden["_fila_numero"], "error",
                                 "cantidad_contratos inválida")
            continue
        multiplicador = obtener_multiplicador_contrato(config_por_ticker.get(ticker, {}))
        cantidad_api = cantidad_contratos * multiplicador
        accion = orden["accion"].strip().lower()

        print(f"[MANUAL] Procesando orden manual: {accion} {simbolo_opcion} x{cantidad_contratos}")

        if accion == "comprar":
            resultado = cliente_iol.comprar_mercado(simbolo_opcion, cantidad_api, mercado="bCBA")
        elif accion == "vender":
            resultado = cliente_iol.vender_mercado(simbolo_opcion, cantidad_api, mercado="bCBA")
        else:
            marcar_orden_manual(pestana_manuales, orden["_fila_numero"], "error",
                                 f"acción desconocida: '{accion}' (debe ser 'comprar' o 'vender')")
            continue

        if resultado.get("pendiente"):
            marcar_orden_manual(pestana_manuales, orden["_fila_numero"], "pendiente_confirmacion",
                                 resultado.get("error", ""))
            tg.notificar_orden_pendiente(simbolo_opcion, accion)
            continue

        if not resultado.get("exito"):
            marcar_orden_manual(pestana_manuales, orden["_fila_numero"], "error",
                                 str(resultado.get("error", resultado)))
            tg.notificar_error(f"orden manual ({accion}) {simbolo_opcion}", str(resultado.get("error", resultado)))
            continue

        precio_ejecutado = resultado["precio_ejecutado"]
        marcar_orden_manual(pestana_manuales, orden["_fila_numero"], "procesada",
                             f"ejecutada a ${precio_ejecutado:,.2f}")

        # Si fue COMPRA, actualiza/crea la posición activa de ese ticker
        # (pisa lo que hubiera -- asumimos que una compra manual
        # reemplaza el estado conocido, ya que el usuario sabe lo que
        # está haciendo al forzarla).
        if accion == "comprar":
            agregar_posicion(pestana_posicion, {
                "ticker": ticker, "tipo": tipo, "simbolo_opcion": simbolo_opcion,
                "strike": orden.get("strike", ""), "fecha_entrada": hoy_str,
                "precio_entrada": precio_ejecutado, "cantidad_contratos": cantidad_contratos,
                "numero_operacion_compra": resultado["numero_operacion"],
            })
            posiciones_activas[ticker] = {"tipo": tipo}
        else:  # vender manual -- se asume que cierra/reduce la posición conocida
            quitar_posicion(pestana_posicion, ticker)
            posiciones_activas.pop(ticker, None)

        tg.notificar_orden_manual_ejecutada(accion, simbolo_opcion, cantidad_contratos, precio_ejecutado)

    # ------------------------------------------------------------------
    # PASO 1: revisar SALIDA de cada posición abierta (una por ticker,
    # pueden ser varias a la vez -- GGAL CALL + COME PUT, por ejemplo)
    # ------------------------------------------------------------------
    for ticker, posicion in list(posiciones_activas.items()):
        tipo = posicion["tipo"]
        simbolo_opcion = posicion["simbolo_opcion"]
        cantidad_contratos = int(posicion["cantidad_contratos"])
        multiplicador = obtener_multiplicador_contrato(config_por_ticker.get(ticker, {}))
        cantidad_api = cantidad_contratos * multiplicador

        try:
            df = descargar_datos_1h(ticker)
        except ValueError as e:
            print(f"[ERROR] {ticker}: {e}")
            continue

        cruce = evaluar_cruce(df)
        dispara_salida = (tipo == "call" and cruce == "bajista") or (tipo == "put" and cruce == "alcista")

        if dispara_salida:
            print(f"{ticker}: señal de salida detectada para {simbolo_opcion} ({tipo})")
            resultado_venta = cliente_iol.vender_mercado(simbolo_opcion, cantidad_api, mercado="bCBA")

            if resultado_venta.get("pendiente"):
                registrar_pendiente(pestana_pendientes, {
                    "fecha": hoy_str, "ticker": ticker, "tipo_orden": "venta",
                    "simbolo_opcion": simbolo_opcion, "numero_operacion": resultado_venta["numero_operacion"],
                    "cantidad_contratos": cantidad_contratos, "detalle": resultado_venta.get("error", ""),
                })
                tg.notificar_orden_pendiente(simbolo_opcion, "venta")
                continue

            if not resultado_venta.get("exito"):
                tg.notificar_error(f"venta {simbolo_opcion}", str(resultado_venta.get("error", resultado_venta)))
                continue

            precio_salida = resultado_venta["precio_ejecutado"]
            precio_entrada = float(posicion["precio_entrada"])
            monto_entrada = precio_entrada * cantidad_api
            monto_salida = precio_salida * cantidad_api
            comision_pct = 0.005 * 1.21
            costos = monto_entrada * comision_pct + monto_salida * comision_pct
            pnl_pesos = (monto_salida - monto_entrada) - costos
            pnl_pct = 100 * pnl_pesos / monto_entrada if monto_entrada else 0

            registrar_historico(pestana_historico, {
                "fecha_entrada": posicion["fecha_entrada"], "fecha_salida": hoy_str,
                "ticker": ticker, "tipo": tipo, "simbolo_opcion": simbolo_opcion,
                "strike": posicion["strike"], "precio_entrada": precio_entrada,
                "precio_salida": precio_salida, "cantidad_contratos": cantidad_contratos,
                "pnl_pesos": pnl_pesos, "pnl_pct": pnl_pct,
                "numero_operacion_compra": posicion["numero_operacion_compra"],
                "numero_operacion_venta": resultado_venta["numero_operacion"],
            })
            quitar_posicion(pestana_posicion, ticker)
            tg.notificar_cierre_posicion(simbolo_opcion, hoy_str, precio_salida, "cruce_ema4_18",
                                          pnl_pesos, pnl_pct)
            continue  # posición cerrada del todo -- no evaluar roleo sobre algo que ya no existe

        # ------------------------------------------------------------------
        # NO hubo señal de salida por cruce -- chequear si corresponde ROLEO
        # (mantener la posición pero mover el strike siguiendo el precio)
        # ------------------------------------------------------------------
        cadena = leer_cadena_vigente(ticker, tipo)
        spot_actual = cliente_iol.obtener_precio(ticker, mercado="bCBA")
        strike_actual = float(posicion["strike"])
        candidato_roleo = determinar_strike_roleo(cadena, strike_actual, spot_actual, tipo)

        if candidato_roleo is None:
            print(f"{ticker}: posición {simbolo_opcion} sigue abierta, sin roleo ni salida todavía.")
            continue

        # Confirmar que el strike destino tiene operatoria real antes de rolear
        try:
            datos_destino = cliente_iol._get(f"/api/v2/bCBA/Titulos/{candidato_roleo['simbolo']}/Cotizacion")
        except Exception as e:
            print(f"{ticker}: error consultando el strike destino del roleo ({e}), se saltea esta corrida.")
            continue
        if datos_destino.get("cantidadOperaciones", 0) == 0:
            print(f"{ticker}: el strike destino del roleo ({candidato_roleo['simbolo']}) "
                  f"no tiene operatoria hoy, se pospone el roleo.")
            continue

        print(f"{ticker}: ROLEO -- de {simbolo_opcion} (strike {strike_actual}) "
              f"a {candidato_roleo['simbolo']} (strike {candidato_roleo['strike']})")

        # 1) Vender la opción actual
        resultado_venta = cliente_iol.vender_mercado(simbolo_opcion, cantidad_api, mercado="bCBA")
        if resultado_venta.get("pendiente"):
            registrar_pendiente(pestana_pendientes, {
                "fecha": hoy_str, "ticker": ticker, "tipo_orden": "venta (roleo)",
                "simbolo_opcion": simbolo_opcion, "numero_operacion": resultado_venta["numero_operacion"],
                "cantidad_contratos": cantidad_contratos, "detalle": resultado_venta.get("error", ""),
            })
            tg.notificar_orden_pendiente(simbolo_opcion, "venta (roleo)")
            continue
        if not resultado_venta.get("exito"):
            tg.notificar_error(f"venta (roleo) {simbolo_opcion}", str(resultado_venta.get("error", resultado_venta)))
            continue

        precio_venta_vieja = resultado_venta["precio_ejecutado"]
        precio_entrada_vieja = float(posicion["precio_entrada"])
        monto_entrada_vieja = precio_entrada_vieja * cantidad_api
        monto_salida_vieja = precio_venta_vieja * cantidad_api
        comision_pct = 0.005 * 1.21
        costos_tramo = monto_entrada_vieja * comision_pct + monto_salida_vieja * comision_pct
        pnl_pesos_tramo = (monto_salida_vieja - monto_entrada_vieja) - costos_tramo
        pnl_pct_tramo = 100 * pnl_pesos_tramo / monto_entrada_vieja if monto_entrada_vieja else 0

        # Se registra como trade CERRADO independiente en el histórico (decisión del usuario)
        registrar_historico(pestana_historico, {
            "fecha_entrada": posicion["fecha_entrada"], "fecha_salida": hoy_str,
            "ticker": ticker, "tipo": tipo, "simbolo_opcion": simbolo_opcion,
            "strike": strike_actual, "precio_entrada": precio_entrada_vieja,
            "precio_salida": precio_venta_vieja, "cantidad_contratos": cantidad_contratos,
            "pnl_pesos": pnl_pesos_tramo, "pnl_pct": pnl_pct_tramo,
            "numero_operacion_compra": posicion["numero_operacion_compra"],
            "numero_operacion_venta": resultado_venta["numero_operacion"],
        })

        # 2) Comprar la nueva opción del strike destino
        resultado_compra = cliente_iol.comprar_mercado(candidato_roleo["simbolo"], cantidad_api, mercado="bCBA")
        if resultado_compra.get("pendiente"):
            # OJO: quedamos SIN posición activa hasta que se confirme --
            # ya vendimos la vieja, la compra nueva está pendiente
            quitar_posicion(pestana_posicion, ticker)
            registrar_pendiente(pestana_pendientes, {
                "fecha": hoy_str, "ticker": ticker, "tipo_orden": "compra (roleo)",
                "simbolo_opcion": candidato_roleo["simbolo"], "numero_operacion": resultado_compra["numero_operacion"],
                "cantidad_contratos": cantidad_contratos, "detalle": resultado_compra.get("error", ""),
            })
            tg.notificar_orden_pendiente(candidato_roleo["simbolo"], "compra (roleo)")
            continue
        if not resultado_compra.get("exito"):
            quitar_posicion(pestana_posicion, ticker)  # vendimos la vieja y la compra nueva falló -- avisar fuerte
            tg.notificar_error(f"compra (roleo) {candidato_roleo['simbolo']}",
                                f"Se vendió {simbolo_opcion} pero la compra de reemplazo falló: "
                                f"{resultado_compra.get('error', resultado_compra)}")
            continue

        precio_compra_nueva = resultado_compra["precio_ejecutado"]
        _verificar_multiplicador_sospechoso(ticker, datos_destino.get("ultimoPrecio", 0),
                                             precio_compra_nueva, multiplicador)
        quitar_posicion(pestana_posicion, ticker)
        agregar_posicion(pestana_posicion, {
            "ticker": ticker, "tipo": tipo, "simbolo_opcion": candidato_roleo["simbolo"],
            "strike": candidato_roleo["strike"], "fecha_entrada": hoy_str,
            "precio_entrada": precio_compra_nueva, "cantidad_contratos": cantidad_contratos,
            "numero_operacion_compra": resultado_compra["numero_operacion"],
        })
        posiciones_activas[ticker] = {"tipo": tipo}  # actualiza el estado en memoria para el resto de esta corrida
        tg.notificar_roleo(ticker, simbolo_opcion, candidato_roleo["simbolo"],
                            precio_venta_vieja, precio_compra_nueva, pnl_pesos_tramo, pnl_pct_tramo)
        continue

    # ------------------------------------------------------------------
    # PASO 2: evaluar ENTRADA en los tickers activos que TODAVÍA NO
    # tienen posición abierta -- pueden abrirse varias en la misma
    # corrida, mientras haya capital disponible (se re-chequea el saldo
    # después de cada compra, porque se va consumiendo).
    # ------------------------------------------------------------------
    for fila_config in config:
        ticker = fila_config["ticker_yahoo"]

        if ticker in posiciones_activas:
            continue  # ya tiene una posición abierta (CALL o PUT) -- no se abre otra encima

        try:
            df = descargar_datos_1h(ticker)
        except ValueError as e:
            print(f"[ERROR] {ticker}: {e}")
            continue

        cruce = evaluar_cruce(df)
        if cruce is None:
            print(f"{ticker}: sin señal de entrada.")
            continue
        senal = "call" if cruce == "alcista" else "put"
        print(f"{ticker}: señal de entrada detectada -> {senal.upper()}")

        info_strike = elegir_strike_inicial(cliente_iol, fila_config, senal)
        if info_strike is None:
            print(f"{ticker}: ningún strike cercano al spot tiene operatoria hoy, se saltea.")
            continue

        cantidad_contratos = int(fila_config["cantidad_contratos"])
        multiplicador = obtener_multiplicador_contrato(fila_config)
        cantidad_api = cantidad_contratos * multiplicador
        costo_estimado = info_strike["precio_opcion"] * cantidad_api * 1.01  # margen simple para comisión+slippage

        saldo_disponible = cliente_iol.consultar_saldo()
        if saldo_disponible < costo_estimado:
            print(f"{ticker}: capital insuficiente (disponible=${saldo_disponible:,.0f}, "
                  f"necesario≈${costo_estimado:,.0f}) -- se saltea esta señal.")
            continue

        resultado_compra = cliente_iol.comprar_mercado(info_strike["simbolo"], cantidad_api, mercado="bCBA")

        if resultado_compra.get("pendiente"):
            registrar_pendiente(pestana_pendientes, {
                "fecha": hoy_str, "ticker": ticker, "tipo_orden": "compra",
                "simbolo_opcion": info_strike["simbolo"], "numero_operacion": resultado_compra["numero_operacion"],
                "cantidad_contratos": cantidad_contratos, "detalle": resultado_compra.get("error", ""),
            })
            tg.notificar_orden_pendiente(info_strike["simbolo"], "compra")
            continue

        if not resultado_compra.get("exito"):
            tg.notificar_error(f"compra {info_strike['simbolo']}", str(resultado_compra.get("error", resultado_compra)))
            continue

        precio_entrada = resultado_compra["precio_ejecutado"]
        _verificar_multiplicador_sospechoso(ticker, info_strike["precio_opcion"], precio_entrada, multiplicador)
        agregar_posicion(pestana_posicion, {
            "ticker": ticker, "tipo": senal, "simbolo_opcion": info_strike["simbolo"],
            "strike": info_strike["strike"], "fecha_entrada": hoy_str,
            "precio_entrada": precio_entrada, "cantidad_contratos": cantidad_contratos,
            "numero_operacion_compra": resultado_compra["numero_operacion"],
        })
        posiciones_activas[ticker] = {"tipo": senal}  # para no reabrir en el mismo loop
        tg.notificar_apertura_opcion(info_strike["simbolo"], senal, hoy_str, precio_entrada, cantidad_contratos)

    if modo == "cierre":
        resumen = f"Posiciones abiertas al cierre: {len(posiciones_activas)}"
        if posiciones_activas:
            detalle = ", ".join(f"{t} ({p['tipo'].upper()})" for t, p in posiciones_activas.items())
            resumen += f"\n{detalle}"
        tg.notificar_bot_desconectado(rutina="opciones - cierre", resumen=resumen)


def main():
    """Toma el lock ANTES de cualquier otra cosa -- si otra corrida
    está en curso, ni siquiera se llama a _ejecutar_bot(). El lock se
    libera SIEMPRE al salir (haya terminado bien o con una excepción),
    gracias al try/finally -- mismo patrón que con_lock_rutina() del
    bot de acciones."""
    planilla = conectar_sheets()
    if not intentar_tomar_lock(planilla, "bot_opciones"):
        return
    try:
        _ejecutar_bot(planilla)
    finally:
        liberar_lock(planilla)


if __name__ == "__main__":
    main()
