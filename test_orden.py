# -*- coding: utf-8 -*-
"""
============================================================================
 TEST DE ORDEN REAL - compra 1 acción y la vende enseguida
============================================================================
Objetivo: confirmar que comprar_mercado()/vender_mercado() en iol_client.py
mandan el JSON correcto a la API real de IOL (el único punto no verificado
al 100% contra la documentación). Compra la menor cantidad posible de un
papel, y la vende de inmediato para cerrar la prueba.

SEGURIDAD: no ejecuta nada salvo que la variable de entorno
CONFIRMAR_ORDEN_REAL sea exactamente "SI" -- para que no se dispare por
error desde un run automático del cron.

Esto USA PLATA REAL de tu cuenta (aunque sea 1 acción de un papel
barato). No es el sandbox -- ya confirmamos que ese no está disponible.
============================================================================
"""

import os
import sys
import time

from iol_client import IOLClient

TICKER_PRUEBA = os.environ.get("TICKER_PRUEBA", "COME.BA")
CANTIDAD_PRUEBA = int(os.environ.get("CANTIDAD_PRUEBA", "1"))
SEGUNDOS_ESPERA_ENTRE_ORDENES = 5


def main():
    confirmar = os.environ.get("CONFIRMAR_ORDEN_REAL", "").strip().upper()
    if confirmar != "SI":
        print(
            "Por seguridad, este script no ejecuta nada salvo que "
            "CONFIRMAR_ORDEN_REAL=SI esté seteado explícitamente. "
            f"Valor recibido: '{confirmar}'"
        )
        sys.exit(0)

    print(f"=== TEST DE ORDEN REAL: {CANTIDAD_PRUEBA} de {TICKER_PRUEBA} ===\n")

    iol = IOLClient(
        usuario=os.environ["IOL_USUARIO"],
        password=os.environ["IOL_PASSWORD"],
    )
    print("✅ Autenticación OK")

    saldo = iol.consultar_saldo()
    print(f"Saldo disponible: ${saldo:,.2f}")

    precio_actual = iol.obtener_precio(TICKER_PRUEBA)
    costo_aproximado = precio_actual * CANTIDAD_PRUEBA
    print(f"Precio actual de {TICKER_PRUEBA}: ${precio_actual:,.2f}")
    print(f"Costo aproximado de la compra: ${costo_aproximado:,.2f}")

    if costo_aproximado > saldo:
        print(f"\n❌ ABORTADO: el costo estimado (${costo_aproximado:,.2f}) "
              f"supera el saldo disponible (${saldo:,.2f}).")
        sys.exit(1)

    print("\n=== Enviando orden de COMPRA ===")
    resultado_compra = iol.comprar_mercado(TICKER_PRUEBA, CANTIDAD_PRUEBA)
    print("Respuesta cruda de IOL (compra):", resultado_compra)

    if not resultado_compra.get("exito"):
        print(
            "\n❌ La compra falló. Este es el resultado más útil de la "
            "prueba: revisar 'error' arriba para ver qué campo del JSON "
            "de comprar_mercado() en iol_client.py hay que ajustar."
        )
        sys.exit(1)

    print(f"\n✅ Compra ejecutada. Número de operación: "
          f"{resultado_compra.get('numero_operacion')}")
    print(f"Esperando {SEGUNDOS_ESPERA_ENTRE_ORDENES}s antes de vender...")
    time.sleep(SEGUNDOS_ESPERA_ENTRE_ORDENES)

    print("\n=== Enviando orden de VENTA (cerrar la prueba) ===")
    resultado_venta = iol.vender_mercado(TICKER_PRUEBA, CANTIDAD_PRUEBA)
    print("Respuesta cruda de IOL (venta):", resultado_venta)

    if resultado_venta.get("exito"):
        print(
            "\n✅ PRUEBA COMPLETA: compra y venta ejecutadas correctamente. "
            "comprar_mercado()/vender_mercado() funcionan contra la API real."
        )
    else:
        print(
            "\n⚠️ La compra se ejecutó pero LA VENTA FALLÓ. "
            "REVISAR MANUALMENTE tu cuenta de IOL -- puede haber quedado "
            f"la posición de {CANTIDAD_PRUEBA} {TICKER_PRUEBA} abierta, "
            "hay que venderla a mano desde la web/app."
        )
        sys.exit(1)


if __name__ == "__main__":
    main()
