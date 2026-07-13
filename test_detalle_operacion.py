# -*- coding: utf-8 -*-
"""
Diagnóstico puntual: consulta el detalle de una operación ya ejecutada
(no crea ninguna orden nueva, es de solo lectura) para ver la estructura
real del JSON que devuelve IOL y encontrar el campo correcto del precio
operado.
"""
import os
from iol_client import IOLClient

NUMEROS_OPERACION = [181231443, 181231470]  # compra y venta del test anterior

iol = IOLClient(
    usuario=os.environ["IOL_USUARIO"],
    password=os.environ["IOL_PASSWORD"],
)

for numero in NUMEROS_OPERACION:
    print(f"\n=== Operación {numero} ===")
    detalle = iol.consultar_detalle_operacion(numero)
    print(detalle)
