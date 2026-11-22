"""Informes: embudo por periodo y métricas diarias.

Cada evento se asigna a su propia fecha de Bogotá: envíos por fecha de envío,
respuestas por fecha de recepción, visitas por fecha de GA4, formularios por fecha
de recepción y cierres por fecha de cierre. Así una conversión de un día sin envíos
no desaparece del informe.
"""

import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import HTTPException

from app.api.lecturas import ids_segmento
from app.config import config
from app.db import Conn, Row, obtener, todos
from app.dominio.reglas import BOGOTA, ETAPAS

FECHA = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _fecha(valor: str | None, campo: str) -> date | None:
    if not valor:
        return None
    if not FECHA.match(valor):
        raise HTTPException(400, f"«{campo}» debe tener formato AAAA-MM-DD")
    try:
        return date.fromisoformat(valor)
    except ValueError as exc:
        raise HTTPException(400, f"«{campo}» no es una fecha válida") from exc


def rango(filtros: dict[str, str]) -> tuple[date, date]:
    hoy = datetime.now(BOGOTA).date()
    desde = _fecha(filtros.get("desde"), "desde") or hoy - timedelta(days=30)
    hasta = _fecha(filtros.get("hasta"), "hasta") or hoy
    if desde > hasta:
        raise HTTPException(400, "La fecha «desde» no puede ser posterior a «hasta»")
    return desde, hasta


def embudo(c: Conn, filtros: dict[str, str]) -> list[Row]:
    """Etapa actual de los contactos; «desde/hasta» filtran por fecha de creación (Bogotá)."""
    condiciones = ["anonimizado_en IS NULL"]
    valores: list[Any] = []
    if filtros.get("segmento"):
        seg = obtener(c, "segmentos", filtros["segmento"])
        condiciones.append("id=ANY(%s::uuid[])")
        valores.append(ids_segmento(c, seg["reglas"]))
    if filtros.get("campana"):
        condiciones.append("id IN (SELECT contacto_id FROM inscripciones WHERE campana_id=%s)")
        valores.append(filtros["campana"])
    if filtros.get("fuente"):
        condiciones.append("origen=%s")
        valores.append(filtros["fuente"])
    desde = _fecha(filtros.get("desde"), "desde")
    hasta = _fecha(filtros.get("hasta"), "hasta")
    if desde and hasta and desde > hasta:
        raise HTTPException(400, "La fecha «desde» no puede ser posterior a «hasta»")
    if desde:
        condiciones.append("creado_en >= (%s::date)::timestamp AT TIME ZONE 'America/Bogota'")
        valores.append(desde)
    if hasta:
        condiciones.append("creado_en < (%s::date + 1)::timestamp AT TIME ZONE 'America/Bogota'")
        valores.append(hasta)
    conteos = {
        r["etapa"]: r["n"]
        for r in todos(
            c,
            "SELECT etapa,count(*) AS n FROM contactos WHERE "
            + " AND ".join(condiciones)
            + " GROUP BY etapa",
            tuple(valores),
        )
    }
    return [{"etapa": e, "conteo": conteos.get(e, 0)} for e in ETAPAS]


CORREO_POR_CAMPANA = """
WITH eventos AS (
  SELECT (m.enviado_en AT TIME ZONE 'America/Bogota')::date AS fecha, i.campana_id,
         count(*) AS enviados, 0 AS respuestas, 0 AS visitas, 0 AS leads, 0 AS clientes
    FROM mensajes m JOIN inscripciones i ON i.id = m.inscripcion_id
   WHERE m.tipo = 'secuencia' AND m.estado = 'enviado'
   GROUP BY 1, 2
  UNION ALL
  SELECT (r.recibida_en AT TIME ZONE 'America/Bogota')::date, r.campana_id, 0, count(*), 0, 0, 0
    FROM respuestas r
   WHERE r.campana_id IS NOT NULL AND coalesce(r.clasificacion, '') <> 'fuera_oficina'
   GROUP BY 1, 2
  UNION ALL
  SELECT w.fecha, ca.id, 0, 0, sum(w.sesiones), 0, 0
    FROM metricas_web_diarias w
    JOIN campanas ca ON w.campana_utm = 'cam-' || left(ca.id::text, 8)
   GROUP BY 1, 2
  UNION ALL
  SELECT (f.recibido_en AT TIME ZONE 'America/Bogota')::date, ii.campana_id, 0, 0, 0, count(DISTINCT f.id), 0
    FROM envios_formulario f
    JOIN enlaces_seguimiento l ON l.referencia = f.referencia_opaca
    JOIN mensajes mm ON mm.id = l.mensaje_id
    JOIN inscripciones ii ON ii.id = mm.inscripcion_id
   GROUP BY 1, 2
  UNION ALL
  SELECT (o.cerrada_en AT TIME ZONE 'America/Bogota')::date, o.campana_origen_id, 0, 0, 0, 0, count(*)
    FROM oportunidades o
   WHERE o.etapa = 'cliente' AND o.campana_origen_id IS NOT NULL
   GROUP BY 1, 2
)
SELECT e.fecha, e.campana_id, ca.segmento_id, 'correo' AS fuente,
       sum(e.enviados)::int AS enviados, sum(e.respuestas)::int AS respuestas,
       sum(e.visitas)::int AS visitas, sum(e.leads)::int AS leads,
       sum(e.clientes)::int AS clientes
  FROM eventos e JOIN campanas ca ON ca.id = e.campana_id
 WHERE e.fecha BETWEEN %s AND %s
 GROUP BY e.fecha, e.campana_id, ca.segmento_id
"""

WEB_POR_CANAL = """
SELECT w.fecha, w.canal AS fuente, sum(w.sesiones)::int AS visitas, sum(w.leads)::int AS leads
  FROM metricas_web_diarias w
 WHERE w.fecha BETWEEN %s AND %s
   AND NOT EXISTS (SELECT 1 FROM campanas ca WHERE w.campana_utm = 'cam-' || left(ca.id::text, 8))
 GROUP BY w.fecha, w.canal
"""

CLIENTES_SIN_CAMPANA = """
SELECT (o.cerrada_en AT TIME ZONE 'America/Bogota')::date AS fecha,
       coalesce(ct.origen, 'directo_desconocido') AS fuente, count(*)::int AS clientes
  FROM oportunidades o LEFT JOIN contactos ct ON ct.id = o.contacto_id
 WHERE o.etapa = 'cliente' AND o.campana_origen_id IS NULL
   AND (o.cerrada_en AT TIME ZONE 'America/Bogota')::date BETWEEN %s AND %s
 GROUP BY 1, 2
"""


def metricas(c: Conn, filtros: dict[str, str]) -> list[Row]:
    desde, hasta = rango(filtros)
    filas: list[Row] = [dict(r) for r in todos(c, CORREO_POR_CAMPANA, (desde, hasta))]
    otros: dict[tuple[date, str], Row] = {}

    def otro(fecha: date, fuente: str) -> Row:
        clave = (fecha, fuente)
        if clave not in otros:
            otros[clave] = {
                "fecha": fecha,
                "fuente": fuente,
                "enviados": 0,
                "respuestas": 0,
                "visitas": 0,
                "leads": 0,
                "clientes": 0,
            }
        return otros[clave]

    for r in todos(c, WEB_POR_CANAL, (desde, hasta)):
        x = otro(r["fecha"], r["fuente"])
        x["visitas"] += r["visitas"]
        # Los leads de visitas de correo ya atribuidas a una campaña se cuentan por
        # formulario; aquí solo quedan canales sin campaña identificable.
        x["leads"] += r["leads"]
    for r in todos(c, CLIENTES_SIN_CAMPANA, (desde, hasta)):
        otro(r["fecha"], r["fuente"])["clientes"] += r["clientes"]
    filas.extend(otros.values())

    costos = {
        r["fecha"]: r["total"]
        for r in todos(
            c,
            "SELECT fecha,sum(CASE moneda WHEN 'COP' THEN monto/%s ELSE monto END) AS total FROM costos WHERE fecha BETWEEN %s AND %s GROUP BY fecha",
            (config().tasa_cop_por_usd, desde, hasta),
        )
    }
    por_dia: dict[date, list[Row]] = defaultdict(list)
    for r in filas:
        r["costoUSD"] = None
        por_dia[r["fecha"]].append(r)
    for fecha, total in costos.items():
        if not por_dia[fecha]:
            r = {
                "fecha": fecha,
                "fuente": "directo_desconocido",
                "enviados": 0,
                "respuestas": 0,
                "visitas": 0,
                "leads": 0,
                "clientes": 0,
                "costoUSD": None,
            }
            por_dia[fecha].append(r)
            filas.append(r)
    # El costo es el total del día y aparece una sola vez por fecha; el frontend
    # agrega por fecha antes de calcular costo por lead y costo de adquisición.
    for fecha, total in costos.items():
        primera = sorted(
            por_dia[fecha], key=lambda r: (str(r.get("campana_id") or "~"), r["fuente"])
        )[0]
        primera["costoUSD"] = total
    return sorted(
        filas,
        key=lambda r: (str(r["fecha"]), str(r.get("campana_id") or "~"), r["fuente"]),
    )
