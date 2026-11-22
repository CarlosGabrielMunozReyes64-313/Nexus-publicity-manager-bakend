"""Búsqueda, paginación y exportación del registro de auditoría en el servidor."""

import csv
import io
import json
import re
from collections.abc import Iterator, Mapping
from typing import Any

from fastapi import HTTPException

from app.api.lecturas import armar
from app.db import Conn, Row, todos, uno
from app.esquemas.serializar import serializar

TEXTO = {"actor": "actor", "accion": "accion", "entidad": "entidad"}
FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
COLUMNAS = (
    "id",
    "fecha",
    "actor",
    "accion",
    "entidad",
    "entidadId",
    "costoUSD",
    "antes",
    "despues",
)


def _like(valor: str) -> str:
    return "%" + valor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def filtros(params: Mapping[str, str]) -> tuple[str, list[Any]]:
    """Construye el WHERE. Las fechas son días de calendario de Bogotá."""
    condiciones: list[str] = []
    valores: list[Any] = []
    for clave, columna in TEXTO.items():
        valor = (params.get(clave) or "").strip()
        if valor:
            condiciones.append(f"{columna} ILIKE %s")
            valores.append(_like(valor))
    entidad_id = (params.get("entidadId") or "").strip()
    if entidad_id:
        condiciones.append("entidad_id=%s")
        valores.append(entidad_id)
    for clave, operador in (("desde", ">="), ("hasta", "<")):
        valor = (params.get(clave) or "").strip()
        if not valor:
            continue
        if not FECHA.match(valor):
            raise HTTPException(400, "Use fechas con formato AAAA-MM-DD")
        dias = 1 if clave == "hasta" else 0
        condiciones.append(
            f"fecha {operador} ((%s::date + {dias})::timestamp AT TIME ZONE 'America/Bogota')"
        )
        valores.append(valor)
    return (" WHERE " + " AND ".join(condiciones) if condiciones else ""), valores


def _entero(params: Mapping[str, str], clave: str, defecto: int, minimo: int, maximo: int) -> int:
    try:
        valor = int(params.get(clave) or defecto)
    except ValueError as exc:
        raise HTTPException(400, f"El parámetro {clave} debe ser un número") from exc
    return max(minimo, min(maximo, valor))


def lista(c: Conn, params: Mapping[str, str]) -> list[Row]:
    """Compatibilidad: sin filtros devuelve las 500 acciones más recientes."""
    where, valores = filtros(params)
    limite = " LIMIT 500" if not where else ""
    return [
        armar(c, "auditoria", r)
        for r in todos(
            c,
            "SELECT * FROM registro_auditoria" + where + " ORDER BY fecha DESC,id DESC" + limite,
            tuple(valores),
        )
    ]


def pagina(c: Conn, params: Mapping[str, str]) -> Row:
    where, valores = filtros(params)
    tamano = _entero(params, "tamano", 50, 1, 200)
    numero = _entero(params, "pagina", 1, 1, 1_000_000)
    total = uno(c, "SELECT count(*) AS n FROM registro_auditoria" + where, tuple(valores))["n"]
    filas = todos(
        c,
        "SELECT * FROM registro_auditoria"
        + where
        + " ORDER BY fecha DESC,id DESC LIMIT %s OFFSET %s",
        (*valores, tamano, (numero - 1) * tamano),
    )
    return {
        "items": [armar(c, "auditoria", r) for r in filas],
        "total": total,
        "pagina": numero,
        "tamano": tamano,
    }


def _celda(valor: Any) -> str:
    if valor is None:
        return ""
    texto = json.dumps(valor, ensure_ascii=False) if isinstance(valor, (dict, list)) else str(valor)
    # Evita que una hoja de cálculo interprete el contenido como fórmula.
    return "'" + texto if texto[:1] in ("=", "+", "-", "@") else texto


def exportar(c: Conn, params: Mapping[str, str]) -> Iterator[str]:
    """Todas las coincidencias, por lotes, sin el límite de 500 filas."""
    where, valores = filtros(params)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    yield "﻿"
    writer.writerow(COLUMNAS)
    ultimo: tuple[Any, Any] | None = None
    while True:
        condicion = where
        extra: list[Any] = []
        if ultimo:
            condicion = (where + " AND " if where else " WHERE ") + "(fecha,id) < (%s,%s)"
            extra = list(ultimo)
        filas = todos(
            c,
            "SELECT * FROM registro_auditoria"
            + condicion
            + " ORDER BY fecha DESC,id DESC LIMIT 1000",
            (*valores, *extra),
        )
        for fila in filas:
            dato = serializar(armar(c, "auditoria", fila))
            writer.writerow([_celda(dato.get(k)) for k in COLUMNAS])
        yield buffer.getvalue()
        buffer.seek(0)
        buffer.truncate(0)
        if len(filas) < 1000:
            return
        ultimo = (filas[-1]["fecha"], filas[-1]["id"])
