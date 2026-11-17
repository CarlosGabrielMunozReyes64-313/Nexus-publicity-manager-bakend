from collections import defaultdict
from datetime import datetime, timedelta

from app.api.lecturas import datos_segmento
from app.config import config
from app.db import Conn, Row, obtener, todos
from app.dominio.reglas import BOGOTA, ETAPAS


def embudo(c: Conn, filtros: dict[str, str]) -> list[Row]:
    rows = todos(c, "SELECT * FROM contactos WHERE anonimizado_en IS NULL")
    ids = None
    if filtros.get("segmento"):
        seg = obtener(c, "segmentos", filtros["segmento"])
        ids = {r["id"] for r in datos_segmento(c, seg["reglas"])}
    if filtros.get("campana"):
        inscritos = {
            str(r["contacto_id"])
            for r in todos(
                c,
                "SELECT contacto_id FROM inscripciones WHERE campana_id=%s",
                (filtros["campana"],),
            )
        }
        ids = inscritos if ids is None else ids & inscritos
    rows = [
        r
        for r in rows
        if (ids is None or str(r["id"]) in ids)
        and (not filtros.get("fuente") or r["origen"] == filtros["fuente"])
        and (
            not filtros.get("desde")
            or r["creado_en"].astimezone(BOGOTA).date().isoformat() >= filtros["desde"]
        )
        and (
            not filtros.get("hasta")
            or r["creado_en"].astimezone(BOGOTA).date().isoformat() <= filtros["hasta"]
        )
    ]
    return [{"etapa": e, "conteo": sum(r["etapa"] == e for r in rows)} for e in ETAPAS]


def metricas(c: Conn, filtros: dict[str, str]) -> list[Row]:
    desde = filtros.get("desde") or (datetime.now(BOGOTA).date() - timedelta(days=30)).isoformat()
    hasta = filtros.get("hasta") or datetime.now(BOGOTA).date().isoformat()
    rows = todos(
        c,
        """SELECT m.fecha AS fecha,
       i.campana_id,ca.segmento_id,count(DISTINCT m.id) AS enviados,
       count(DISTINCT r.id) AS respuestas,0 AS visitas,'correo' AS fuente,
       (SELECT count(*) FROM envios_formulario f JOIN enlaces_seguimiento l ON l.referencia=f.referencia_opaca
        JOIN mensajes mm ON mm.id=l.mensaje_id JOIN inscripciones ii ON ii.id=mm.inscripcion_id
        WHERE ii.campana_id=i.campana_id AND (f.recibido_en AT TIME ZONE 'America/Bogota')::date=m.fecha) AS leads,
       (SELECT count(*) FROM oportunidades o WHERE o.campana_origen_id=i.campana_id AND o.etapa='cliente'
         AND (o.cerrada_en AT TIME ZONE 'America/Bogota')::date=m.fecha) AS clientes
       FROM (SELECT *, (enviado_en AT TIME ZONE 'America/Bogota')::date AS fecha FROM mensajes) m JOIN inscripciones i ON i.id=m.inscripcion_id JOIN campanas ca ON ca.id=i.campana_id
       LEFT JOIN respuestas r ON r.mensaje_origen_id=m.id AND coalesce(r.clasificacion,'')<>'fuera_oficina'
       WHERE m.estado='enviado' AND m.fecha BETWEEN %s AND %s
       GROUP BY 1,2,3""",
        (desde, hasta),
    )
    for r in rows:
        campaign = "cam-" + str(r["campana_id"])[:8]
        web = todos(
            c,
            "SELECT sesiones FROM metricas_web_diarias WHERE fecha=%s AND campana_utm=%s",
            (r["fecha"], campaign),
        )
        r["visitas"] = sum(w["sesiones"] for w in web)
    rows += todos(
        c,
        "SELECT fecha,canal AS fuente,sum(sesiones) AS visitas,sum(leads) AS leads,0 AS enviados,0 AS respuestas,0 AS clientes FROM metricas_web_diarias WHERE canal<>'correo' AND fecha BETWEEN %s AND %s GROUP BY fecha,canal",
        (desde, hasta),
    )
    costs = todos(
        c,
        "SELECT fecha,sum(CASE moneda WHEN 'COP' THEN monto/%s ELSE monto END) AS total FROM costos WHERE fecha BETWEEN %s AND %s GROUP BY fecha",
        (config().tasa_cop_por_usd, desde, hasta),
    )
    byday = defaultdict(list)
    for r in rows:
        r["costoUSD"] = None
        byday[r["fecha"]].append(r)
    for cost in costs:
        bucket = byday[cost["fecha"]]
        if not bucket:
            r = {
                "fecha": cost["fecha"],
                "fuente": "directo_desconocido",
                "enviados": 0,
                "respuestas": 0,
                "visitas": 0,
                "leads": 0,
                "clientes": 0,
            }
            bucket.append(r)
            rows.append(r)
        bucket[0]["costoUSD"] = cost["total"]
    return sorted(rows, key=lambda r: str(r["fecha"]))
