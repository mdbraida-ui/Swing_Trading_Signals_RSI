# -*- coding: utf-8 -*-
"""
============================================================================
 NOTIFICACIONES TELEGRAM - Bot Swing RSI+Bollinger
============================================================================

Setup necesario (una sola vez):
  1. Hablar con @BotFather en Telegram -> /newbot -> te da un TOKEN
  2. Mandarle un mensaje cualquiera a tu bot nuevo (para que sepa a quién
     responder)
  3. Conseguir tu chat_id: abrir en el navegador
     https://api.telegram.org/bot<TU_TOKEN>/getUpdates
     y buscar el campo "chat":{"id": ...} en la respuesta JSON
  4. Guardar TOKEN y CHAT_ID como variables de entorno (nunca hardcodeados
     en el código, menos si el repo es público o compartido):
       TELEGRAM_BOT_TOKEN=xxxxx
       TELEGRAM_CHAT_ID=xxxxx

En GitHub Actions, estas van como "Secrets" del repositorio (Settings ->
Secrets and variables -> Actions) y se inyectan como env vars al workflow.
============================================================================
"""

import os
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

TIMEOUT_SEGUNDOS = 10
TZ_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")


def _ahora_argentina() -> str:
    """GitHub Actions corre en UTC -- sin esto, los mensajes muestran la
    hora del servidor (3hs adelantada respecto a Argentina) en vez de la
    hora real local. America/Argentina/Buenos_Aires es UTC-3 todo el año
    (sin horario de verano desde 2009), así que no hace falta lógica
    adicional para DST."""
    return datetime.now(TZ_ARGENTINA).strftime("%d/%m/%Y %H:%M")


def _enviar(mensaje: str) -> bool:
    """Envía un mensaje de texto (Markdown) al chat configurado.
    Devuelve True/False según si Telegram confirmó el envío -- nunca
    lanza excepción hacia afuera, porque un fallo de notificación no
    debería frenar al bot de operar."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[telegram] TOKEN/CHAT_ID no configurados, mensaje no enviado:\n{mensaje}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=payload, timeout=TIMEOUT_SEGUNDOS)
        r.raise_for_status()
        return True
    except Exception as e:
        print(f"[telegram] error al enviar mensaje: {e}")
        return False


def notificar_bot_conectado(rutina: str = ""):
    """rutina: 'apertura' (10:30) o 'cierre' (17:00), para dejar claro
    en qué corrida está el bot."""
    ahora = _ahora_argentina()
    etiqueta = f" ({rutina})" if rutina else ""
    _enviar(f"🟢 *Bot Swing conectado*{etiqueta}\n{ahora}")


def notificar_bot_desconectado(rutina: str = "", resumen: str = ""):
    ahora = _ahora_argentina()
    etiqueta = f" ({rutina})" if rutina else ""
    texto = f"🔴 *Bot Swing desconectado*{etiqueta}\n{ahora}"
    if resumen:
        texto += f"\n\n{resumen}"
    _enviar(texto)


def notificar_error(contexto: str, detalle: str):
    """Para fallos que el bot no puede resolver solo (ej. la API de IOL
    no responde, una orden fue rechazada) -- así te enterás sin tener
    que revisar logs."""
    ahora = _ahora_argentina()
    _enviar(f"⚠️ *Error en el bot* ({contexto})\n{ahora}\n\n{detalle}")


def notificar_apertura_posicion(ticker: str, fecha: str, precio_entrada: float,
                                 stop_loss: float, take_profit: float = None,
                                 acciones: int = None):
    partes = [
        "🟩 *Nueva posición abierta*",
        f"Activo: `{ticker}`",
        f"Fecha: {fecha}",
        f"Precio entrada: ${precio_entrada:,.2f}",
        f"Stop Loss inicial: ${stop_loss:,.2f}",
    ]
    if take_profit is not None:
        partes.append(f"Take Profit: ${take_profit:,.2f}")
    if acciones is not None:
        partes.append(f"Cantidad: {acciones}")
    _enviar("\n".join(partes))


def notificar_cierre_posicion(ticker: str, fecha: str, precio_salida: float,
                               motivo: str, pnl_pesos: float, pnl_pct: float):
    """motivo: 'take_profit', 'stop_loss', 'trailing_stop',
    'cierre_forzado_max_dias', etc."""
    emoji = "🟢" if pnl_pesos >= 0 else "🔴"
    motivo_legible = {
        "take_profit": "Take Profit",
        "stop_loss": "Stop Loss",
        "trailing_stop": "Trailing Stop",
        "cierre_forzado_max_dias": "Cierre forzado (máx. días)",
        "stop_loss_10pct": "Stop Loss 10% (Fase A)",
        "cierre_bajo_ema50": "Cierre bajo EMA50 (Fase B)",
    }.get(motivo, motivo)

    signo = "+" if pnl_pesos >= 0 else "-"
    monto_abs = abs(pnl_pesos)
    pct_abs = abs(pnl_pct)
    partes = [
        f"{emoji} *Posición cerrada*",
        f"Activo: `{ticker}`",
        f"Fecha: {fecha}",
        f"Precio salida: ${precio_salida:,.2f}",
        f"Motivo: {motivo_legible}",
        f"Resultado: {signo}${monto_abs:,.2f} ({signo}{pct_abs:.2f}%)",
    ]
    _enviar("\n".join(partes))


def notificar_trailing_actualizado(ticker: str, stop_anterior: float, stop_nuevo: float):
    """Opcional: aviso liviano cuando se sube el trailing stop (podés
    omitir esta llamada si preferís menos ruido de notificaciones)."""
    _enviar(
        f"📈 *Trailing actualizado* `{ticker}`\n"
        f"${stop_anterior:,.2f} -> ${stop_nuevo:,.2f}"
    )


def notificar_resumen_apertura(cantidad_tickers: int, senales_pendientes: int,
                                senales_confirmadas: int, en_cooldown: int,
                                posiciones_abiertas: int):
    """Segundo mensaje de la rutina de apertura -- confirma que el bot
    terminó de actualizar Indicadores y da un pantallazo del día, aunque
    esta estrategia no compre en apertura (las entradas se evalúan recién
    en la rutina de cierre, con el precio de esa ventana como proxy del
    cierre del día)."""
    partes = [
        "📊 *Resumen de apertura*",
        f"Tickers seguidos: {cantidad_tickers}",
        f"Señales pendientes (esperando BBW): {senales_pendientes}",
        f"Señales confirmadas hoy: {senales_confirmadas}",
        f"Tickers en cooldown: {en_cooldown}",
        f"Posiciones abiertas: {posiciones_abiertas}",
    ]
    if senales_confirmadas > 0:
        partes.append("\n_Las entradas se ejecutan en la ventana de cierre (~16:30), no ahora._")
    _enviar("\n".join(partes))