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
        """{ "GGAL.BA": {"cantidad": 15, "precio_promedio": 4500.50}, ... }
        NOTA: los símbolos que devuelve IOL vienen sin sufijo ".BA"
        (ej. "GGAL"), a diferencia de Yahoo Finance que sí lo usa. Se
        normaliza acá agregando ".BA" para que coincida con TICKERS_SWING."""
        portafolio = self._get("/api/v2/portafolio/argentina")
        posiciones = {}
        for activo in portafolio.get("activos", []):
            simbolo = activo["titulo"]["simbolo"]
            ticker = simbolo if simbolo.endswith(".BA") else f"{simbolo}.BA"
            posiciones[ticker] = {
                "cantidad": int(activo["cantidad"]),
                "precio_promedio": float(activo.get("precioCompra", activo.get("ultimoPrecio", 0))),
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
        """
        simbolo = ticker.replace(".BA", "")
        from datetime import date
        precio_actual = self.obtener_precio(ticker, mercado=mercado)
        precio_limite = round(precio_actual * (1 + margen_pct), 2)
        body = {
            "mercado": mercado,
            "simbolo": simbolo,
            "cantidad": cantidad,
            "precio": precio_limite,
            "plazo": "t0",
            "validez": date.today().isoformat(),
        }
        respuesta = self._post("/api/v2/operar/Comprar", body)
        return self._interpretar_respuesta_orden(respuesta)

    def vender_mercado(self, ticker: str, cantidad: int, mercado: str = "bCBA",
                        margen_pct: float = 0.01) -> dict:
        """Precio límite = cotización actual -1%, para que se ejecute
        casi seguro al instante contra la punta compradora."""
        simbolo = ticker.replace(".BA", "")
        from datetime import date
        precio_actual = self.obtener_precio(ticker, mercado=mercado)
        precio_limite = round(precio_actual * (1 - margen_pct), 2)
        body = {
            "mercado": mercado,
            "simbolo": simbolo,
            "cantidad": cantidad,
            "precio": precio_limite,
            "plazo": "t0",
            "validez": date.today().isoformat(),
        }
        respuesta = self._post("/api/v2/operar/Vender", body)
        return self._interpretar_respuesta_orden(respuesta)

    @staticmethod
    def _interpretar_respuesta_orden(respuesta: dict) -> dict:
        """Normaliza la respuesta cruda de IOL al formato que espera
        bot_swing_diario.py: {"exito": bool, "precio_ejecutado": float,
        "numero_operacion": ...}. AJUSTAR según la estructura real que
        devuelva IOL una vez probado (esto es un supuesto razonable, no
        confirmado -- las respuestas de éxito suelen traer "numero" o
        "numeroOperacion" con el id de la orden creada)."""
        if "error_http" in respuesta:
            return {"exito": False, "error": respuesta.get("error_texto", respuesta)}
        numero_operacion = respuesta.get("numero") or respuesta.get("numeroOperacion")
        if numero_operacion is None:
            return {"exito": False, "error": f"Respuesta inesperada: {respuesta}"}
        return {
            "exito": True,
            "numero_operacion": numero_operacion,
            # El precio de ejecución real de una orden a mercado no
            # siempre viene en la respuesta inmediata de la orden -- IOL
            # puede requerir consultar GET /api/v2/operaciones/{numero}
            # después para saber el precio operado real. Placeholder:
            "precio_ejecutado": respuesta.get("precio", 0),
        }

    def consultar_detalle_operacion(self, numero_operacion) -> dict:
        """GET /api/v2/operaciones/{numero} -- para confirmar precio real
        ejecutado de una orden ya enviada (útil si comprar_mercado /
        vender_mercado no traen el precio final en la respuesta inicial)."""
        return self._get(f"/api/v2/operaciones/{numero_operacion}")
