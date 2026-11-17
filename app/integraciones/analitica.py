from datetime import date, timedelta
from typing import Any, Protocol

from googleapiclient.discovery import build

from app.api.datos import ahora
from app.config import config
from app.db import Conn, Row
from app.dominio.reglas import BOGOTA
from app.integraciones.conexiones import credenciales_servicio


def canal(grupo: str, fuente: str) -> str:
    if fuente == "nexus-pm" or grupo == "Email":
        return "correo"
    return {
        "Organic Search": "organico_busqueda",
        "Organic Social": "organico_redes",
        "Paid Search": "pagado",
        "Paid Social": "pagado",
        "Display": "pagado",
        "Cross-network": "pagado",
        "Referral": "referencia",
    }.get(grupo, "directo_desconocido")


class Analitica(Protocol):
    def ga4(self, desde: date, hasta: date) -> list[Row]: ...
    def search_console(self, desde: date, hasta: date) -> list[Row]: ...


class AnaliticaFalsa:
    def __init__(self, web: list[Row], busqueda: list[Row]) -> None:
        self.web = web
        self.busqueda = busqueda

    def ga4(self, desde: date, hasta: date) -> list[Row]:
        return self.web

    def search_console(self, desde: date, hasta: date) -> list[Row]:
        return self.busqueda


class AnaliticaReal:
    def ga4(self, desde: date, hasta: date) -> list[Row]:
        service = build(
            "analyticsdata", "v1beta", credentials=credenciales_servicio(), cache_discovery=False
        )
        dimensions = [
            "date",
            "sessionDefaultChannelGroup",
            "sessionSource",
            "sessionMedium",
            "sessionCampaignName",
        ]
        base = {
            "dateRanges": [{"startDate": desde.isoformat(), "endDate": hasta.isoformat()}],
            "dimensions": [{"name": d} for d in dimensions],
        }
        result: dict[tuple[str, ...], Row] = {}

        def pages(body: dict[str, Any]) -> list[Row]:
            out: list[Row] = []
            offset = 0
            while True:
                r = (
                    service.properties()
                    .runReport(
                        property="properties/" + config().ga4_propiedad_id,
                        body={**base, **body, "offset": str(offset), "limit": "10000"},
                    )
                    .execute()
                )
                out.extend(r.get("rows", []))
                offset += len(r.get("rows", []))
                if offset >= r.get("rowCount", 0) or not r.get("rows"):
                    return out

        for row in pages({"metrics": [{"name": "sessions"}, {"name": "totalUsers"}]}):
            d = tuple(x["value"] for x in row["dimensionValues"])
            result[d] = {
                "fecha": date(int(d[0][:4]), int(d[0][4:6]), int(d[0][6:8])),
                "canal": canal(d[1], d[2]),
                "fuente": d[2],
                "medio": d[3],
                "campana_utm": d[4],
                "sesiones": int(row["metricValues"][0]["value"]),
                "usuarios": int(row["metricValues"][1]["value"]),
                "leads": 0,
                "clics_whatsapp": 0,
            }
        for evento, col in (("generate_lead", "leads"), ("whatsapp_click", "clics_whatsapp")):
            for row in pages(
                {
                    "metrics": [{"name": "eventCount"}],
                    "dimensionFilter": {
                        "filter": {
                            "fieldName": "eventName",
                            "stringFilter": {"matchType": "EXACT", "value": evento},
                        }
                    },
                }
            ):
                d = tuple(x["value"] for x in row["dimensionValues"])
                if d in result:
                    result[d][col] = int(row["metricValues"][0]["value"])
        # Different GA groups may map to one application channel; aggregate keys before upsert.
        agg: dict[tuple[Any, ...], Row] = {}
        for row in result.values():
            key = tuple(row[k] for k in ("fecha", "canal", "fuente", "medio", "campana_utm"))
            if key not in agg:
                agg[key] = row
            else:
                for col in ("sesiones", "usuarios", "leads", "clics_whatsapp"):
                    agg[key][col] += row[col]
        return list(agg.values())

    def search_console(self, desde: date, hasta: date) -> list[Row]:
        service = build(
            "searchconsole", "v1", credentials=credenciales_servicio(), cache_discovery=False
        )
        out: list[Row] = []
        offset = 0
        while True:
            result = (
                service.searchanalytics()
                .query(
                    siteUrl=config().search_console_sitio,
                    body={
                        "startDate": desde.isoformat(),
                        "endDate": hasta.isoformat(),
                        "dimensions": ["date", "query", "page"],
                        "rowLimit": 25000,
                        "startRow": offset,
                    },
                )
                .execute()
            )
            rows = result.get("rows", [])
            out.extend(
                {
                    "fecha": r["keys"][0],
                    "consulta": r["keys"][1],
                    "pagina": r["keys"][2],
                    "clics": int(r["clicks"]),
                    "impresiones": int(r["impressions"]),
                    "posicion": r["position"],
                }
                for r in rows
            )
            if len(rows) < 25000:
                return out
            offset += len(rows)


def sincronizar(c: Conn, tipo: str, proveedor: Analitica | None = None) -> None:
    today = ahora().astimezone(BOGOTA).date()
    api = proveedor or AnaliticaReal()
    if tipo == "sincronizar_ga4":
        desde = today - timedelta(days=4)
        rows = api.ga4(desde, today - timedelta(days=1))
        c.execute(
            "DELETE FROM metricas_web_diarias WHERE fecha BETWEEN %s AND %s",
            (desde, today - timedelta(days=1)),
        )
        for r in rows:
            c.execute(
                "INSERT INTO metricas_web_diarias(fecha,canal,fuente,medio,campana_utm,sesiones,usuarios,leads,clics_whatsapp) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(fecha,canal,fuente,medio,campana_utm) DO UPDATE SET sesiones=excluded.sesiones,usuarios=excluded.usuarios,leads=excluded.leads,clics_whatsapp=excluded.clics_whatsapp,importado_en=now()",
                tuple(
                    r[k]
                    for k in (
                        "fecha",
                        "canal",
                        "fuente",
                        "medio",
                        "campana_utm",
                        "sesiones",
                        "usuarios",
                        "leads",
                        "clics_whatsapp",
                    )
                ),
            )
        ident = "ga4"
    else:
        desde = today - timedelta(days=5)
        rows = api.search_console(desde, today - timedelta(days=1))
        c.execute(
            "DELETE FROM consultas_busqueda_diarias WHERE fecha BETWEEN %s AND %s",
            (desde, today - timedelta(days=1)),
        )
        for r in rows:
            c.execute(
                "INSERT INTO consultas_busqueda_diarias(fecha,consulta,pagina,clics,impresiones,posicion) VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(fecha,consulta,pagina) DO UPDATE SET clics=excluded.clics,impresiones=excluded.impresiones,posicion=excluded.posicion,importado_en=now()",
                tuple(
                    r[k]
                    for k in ("fecha", "consulta", "pagina", "clics", "impresiones", "posicion")
                ),
            )
        ident = "search_console"
    c.execute("UPDATE integraciones SET ultima_sincronizacion=now() WHERE id=%s", (ident,))
