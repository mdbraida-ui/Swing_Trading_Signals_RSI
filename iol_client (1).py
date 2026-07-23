# -*- coding: utf-8 -*-
"""
============================================================================
 IOLClient - Wrapper de la API de InvertirOnline (IOL)
============================================================================

Armado a partir de:
  - Documentación oficial de autenticación (api.invertironline.com/Help/
    Autenticacion): POST /token con username/password/grant_type=password
  - Swagger real de la API (confirmado por captura de pantalla): endpoints
    de MiCuenta, Operar, Titulos
  - Código de referencia funcionando (github.com/facundo-bogado/api_iol)
    para el formato de estadocuenta, portafolio y cotización

LO ÚNICO NO 100% CONFIRMADO: el JSON exacto que esperan /operar/Comprar y
/operar/Vender (la documentación interactiva de esos dos endpoints
requiere estar logueado en el navegador, no pude verla desde acá). Usé
los nombres de campo más estándar y documentados en la plataforma
("mercado", "simbolo", "cantidad", "precio", "plazo", "validez").

*** ANTES DE USAR CON PLATA REAL ***
IOL tiene un entorno de pruebas (sandbox) en api-sandbox.invertironline.com
que permite simular compras/ventas sin tocar la cuenta real. Recomiendo
fuerte probar `comprar_mercado()` y `vender_mercado()` ahí primero -- si
el JSON de body no es exacto, la API va a devolver un error 400 con el
detalle de qué campo está mal, y se ajusta en minutos. Para usar el
sandbox, cambiar BASE_URL de esta clase a "https://api-sandbox.invertironline.com".
============================================================================
"""

import time
import requests


class IOLClient:
    BASE_URL = "https://api.invertironline.com"
    TIMEOUT_SEGUNDOS = 15

    def __init__(self, usuario: str, password: str):
        self.usuario = usuario
        self.password = password
        self.access_token = None
        self.refresh_token = None
        self.token_expira_en = 0  # timestamp unix

        self._autenticar()

    # ------------------------------------------------------------------
    # AUTENTICACIÓN
    # ------------------------------------------------------------------
    def _autenticar(self):
        """POST /token con usuario/contraseña. El bearer token dura 15
        minutos; se guarda también el refresh_token para renovarlo sin
        volver a mandar la contraseña."""
        data = {
            "username": self.usuario,
            "password": self.password,
            "grant_type": "password",
        }
        r = requests.post(f"{self.BASE_URL}/token", data=data, timeout=self.TIMEOUT_SEGUNDOS)
        r.raise_for_status()
        respuesta = r.json()
        self.access_token = respuesta["access_token"]
        self.refresh_token = respuesta["refresh_token"]
        # ".expires_in" viene en segundos; se resta un margen de 60s para
        # renovar un poco antes de que venza de verdad.
        self.token_expira_en = time.time() + int(respuesta.get("expires_in", 900)) - 60

    def _refrescar_token(self):
        data = {"refresh_token": self.refresh_token, "grant_type": "refresh_token"}
        r = requests.post(f"{self.BASE_URL}/token", data=data, timeout=self.TIMEOUT_SEGUNDOS)
        if not r.ok:
            # El refresh token también puede vencer -- si falla, se
            # vuelve a autenticar con usuario/contraseña desde cero.
            self._autenticar()
            return
        respuesta = r.json()
        self.access_token = respuesta["access_token"]
        self.refresh_token = respuesta["refresh_token"]
        self.token_expira_en = time.time() + int(respuesta.get("expires_in", 900)) - 60

    def _headers(self) -> dict:
        if time.time() >= self.token_expira_en:
            self._refrescar_token()
        return {"Authorization": f"Bearer {self.access_token}"}

    def _get(self, path: str, params: dict = None) -> dict:
        r = requests.get(f"{self.BASE_URL}{path}", headers=self._headers(),
                          params=params, timeout=self.TIMEOUT_SEGUNDOS)
        r.raise_for_status()
        return r.json()

    def _post(self, path: str, body: dict) -> dict:
        headers = self._headers()
        headers["Content-Type"] = "application/json"
        r = requests.post(f"{self.BASE_URL}{path}", headers=headers, json=body,
                           timeout=self.TIMEOUT_SEGUNDOS)
        # No usamos raise_for_status() acá: si IOL rechaza la orden
        # (ej. fondos insuficientes, mercado cerrado), preferimos leer
        # el mensaje de error del body en vez de solo lanzar excepción.
        try:
            data = r.json()
        except Exception:
            data = {}
        if not r.ok:
            data.setdefault("error_http", r.status_code)
            data.setdefault("error_texto", r.text[:500])
        return data

    # ------------------------------------------------------------------
    # CONSULTAS
    # ------------------------------------------------------------------
    def consultar_saldo(self) -> float:
        """Efectivo disponible en pesos (cuenta Argentina, cuentas[0])."""
        estado = self._get("/api/v2/estadocuenta")
        cuenta_pesos = next(
            (c for c in estado["cuentas"] if c.get("tipo") == "inversion_Argentina_Pesos"),
            estado["cuentas"][0],  # fallback: primera cuenta si no matchea el tipo exacto
        )
        return float(cuenta_pesos["disponible"])

    def consultar_posiciones(self) -> dict:
        """{ "GGAL.BA": {"cantidad": 15, "precio_promedio": 4500.50,
        "ultimo_precio": 4610.75}, ... }
        NOTA: los símbolos que devuelve IOL vienen sin sufijo ".BA"
        (ej. "GGAL"), a diferencia de Yahoo Finance que sí lo usa. Se
        normaliza acá agregando ".BA" para que coincida con TICKERS_SWING.
        `ultimo_precio` viene en la misma respuesta del portafolio (no
        hace falta una llamada extra) -- útil para valuar la cartera a
        mercado en vez de a precio de costo."""
        portafolio = self._get("/api/v2/portafolio/argentina")
        posiciones = {}
        for activo in portafolio.get("activos", []):
            simbolo = activo["titulo"]["simbolo"]
            ticker = simbolo if simbolo.endswith(".BA") else f"{simbolo}.BA"
            posiciones[ticker] = {
                "cantidad": int(activo["cantidad"]),
                "precio_promedio": float(activo.get("precioCompra", 0)),
                "ultimo_precio": float(activo.get("ultimoPrecio", 0)),
            }
        return posiciones

    def obtener_precio(self, ticker: str, mercado: str = "bCBA") -> float:
        """Último precio operado. `ticker` puede venir con o sin ".BA"."""
        simbolo = ticker.replace(".BA", "")
        cotizacion = self._get(f"/api/v2/{mercado}/Titulos/{simbolo}/Cotizacion")
        return float(cotizacion["ultimoPrecio"])

    # ------------------------------------------------------------------
    # OPERAR (compra/venta a mercado)
    # ------------------------------------------------------------------
    def comprar_mercado(self, ticker: str, cantidad: int, mercado: str = "bCBA",
                         margen_pct: float = 0.01) -> dict:
        """
        Envía una orden de compra con un precio límite calculado sobre la
        cotización actual (+1% por defecto), para que se comporte como
        una orden "a mercado" -- se ejecuta casi seguro al instante sin
        dejar la orden pendiente en la punta compradora.

        Devuelve {"exito": bool, "precio_ejecutado": float, "numero_operacion": ...}

        *** AJUSTADO TRAS TEST REAL ***
        IOL rechazó "precio": 0 con el error "El cálculo de porcentajes
        mínimo y máximo no puede tener un valor nullable o menor a 0" --
        no existe la convención de "0 = mercado" en esta API. Hay que
        mandar un precio límite real.

        *** AJUSTADO TRAS TEST REAL #2 ***
        En instrumentos de precio alto (ej. TSLA.BA ~$40.000+), IOL
        rechaza con "Los decimales indicados no son compatibles con la
        alteración mínima permitida" -- BYMA exige que el precio caiga
        en un múltiplo específico según el rango de precio del
        instrumento (tick size), y ese múltiplo no es siempre $0.01. No
        tenemos la tabla exacta de tick size por rango, así que en vez
        de adivinarla, se reintenta con redondeos progresivamente más
        gruesos (2 decimales -> 1 -> 0 -> múltiplo de 5) hasta que IOL
        acepte o se agoten los intentos.
        """
        simbolo = ticker.replace(".BA", "")
        from datetime import date
        precio_actual = self.obtener_precio(ticker, mercado=mercado)
        precio_base = precio_actual * (1 + margen_pct)

        for redondeo in self._REDONDEOS_PRECIO:
            precio_limite = redondeo(precio_base)
            body = {
                "mercado": mercado,
                "simbolo": simbolo,
                "cantidad": cantidad,
                "precio": precio_limite,
                "plazo": "t0",
                "validez": date.today().isoformat(),
            }
            respuesta = self._post("/api/v2/operar/Comprar", body)
            if not self._es_error_alteracion_minima(respuesta):
                return self._interpretar_respuesta_orden(respuesta)
        return self._interpretar_respuesta_orden(respuesta)  # último intento, ya con el error real

    def vender_mercado(self, ticker: str, cantidad: int, mercado: str = "bCBA",
                        margen_pct: float = 0.01) -> dict:
        """Precio límite = cotización actual -1%, para que se ejecute
        casi seguro al instante contra la punta compradora. Mismo
        reintento con redondeos progresivos que comprar_mercado (ver su
        docstring, sección AJUSTADO TRAS TEST REAL #2)."""
        simbolo = ticker.replace(".BA", "")
        from datetime import date
        precio_actual = self.obtener_precio(ticker, mercado=mercado)
        precio_base = precio_actual * (1 - margen_pct)

        for redondeo in self._REDONDEOS_PRECIO:
            precio_limite = redondeo(precio_base)
            body = {
                "mercado": mercado,
                "simbolo": simbolo,
                "cantidad": cantidad,
                "precio": precio_limite,
                "plazo": "t0",
                "validez": date.today().isoformat(),
            }
            respuesta = self._post("/api/v2/operar/Vender", body)
            if not self._es_error_alteracion_minima(respuesta):
                return self._interpretar_respuesta_orden(respuesta)
        return self._interpretar_respuesta_orden(respuesta)

    # Redondeos a probar en orden, del más fino al más grueso -- el
    # primero que IOL acepte (no rechace por "alteración mínima") gana.
    _REDONDEOS_PRECIO = [
        lambda p: round(p, 2),
        lambda p: round(p, 1),
        lambda p: round(p, 0),
        lambda p: round(p / 5) * 5,
    ]

    @staticmethod
    def _es_error_alteracion_minima(respuesta: dict) -> bool:
        texto = str(respuesta)
        return "alteración mínima" in texto or "alteracion minima" in texto.lower()

    def _interpretar_respuesta_orden(self, respuesta: dict, intentos: int = 5,
                                      espera_segundos: float = 1.5) -> dict:
        """
        Normaliza la respuesta cruda de IOL. CONFIRMADO CON DATOS REALES:
        la respuesta inmediata de /operar/Comprar-Vender no trae el precio
        real ejecutado (el "precio" que devuelve ahí es el precio LÍMITE
        que mandamos, no el operado). Hay que consultar
        GET /api/v2/operaciones/{numero} aparte, y ahí el precio real
        ejecutado es `montoOperacion / cantidad` (funciona incluso con
        fills parciales, porque monto y cantidad son los totales).

        Se reintenta unas pocas veces por si la operación todavía no
        está "terminada" en el instante en que se consulta.
        """
        if "error_http" in respuesta:
            return {"exito": False, "error": respuesta.get("error_texto", respuesta)}
        numero_operacion = respuesta.get("numero") or respuesta.get("numeroOperacion")
        if numero_operacion is None:
            return {"exito": False, "error": f"Respuesta inesperada: {respuesta}"}

        precio_ejecutado = None
        for _ in range(intentos):
            try:
                detalle = self.consultar_detalle_operacion(numero_operacion)
            except Exception:
                detalle = {}
            cantidad = detalle.get("cantidad") or 0
            monto_operacion = detalle.get("montoOperacion")
            if detalle.get("estadoActual") == "terminada" and cantidad and monto_operacion:
                precio_ejecutado = monto_operacion / cantidad
                break
            time.sleep(espera_segundos)

        if precio_ejecutado is None:
            # No se pudo confirmar el precio real tras varios intentos --
            # se usa el precio límite enviado como aproximación, pero se
            # marca explícitamente para que quien reciba esto sepa que
            # es una estimación, no el precio real confirmado.
            precio_ejecutado = respuesta.get("precio", 0)
            return {
                "exito": True, "numero_operacion": numero_operacion,
                "precio_ejecutado": precio_ejecutado, "precio_confirmado": False,
            }

        return {
            "exito": True, "numero_operacion": numero_operacion,
            "precio_ejecutado": precio_ejecutado, "precio_confirmado": True,
        }

    def consultar_detalle_operacion(self, numero_operacion) -> dict:
        """GET /api/v2/operaciones/{numero} -- para confirmar precio real
        ejecutado de una orden ya enviada (útil si comprar_mercado /
        vender_mercado no traen el precio final en la respuesta inicial)."""
        return self._get(f"/api/v2/operaciones/{numero_operacion}")