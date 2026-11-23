import math
import re
import unicodedata
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import holidays
from fastapi import HTTPException

BOGOTA = ZoneInfo("America/Bogota")
ETAPAS = [
    "identificado",
    "contactable",
    "contactado",
    "interesado",
    "lead",
    "calificado",
    "discovery",
    "propuesta",
    "cliente",
    "postventa",
]
CAMPOS = {"municipio", "sector", "tamano", "puntaje", "etapa", "origen", "baseLegal", "contactoIds"}


def normalizar_nombre(s: str) -> str:
    s = "".join(x for x in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(x))
    s = re.sub(r"\bs\.?a\.?s\.?\b|\bltda\.?\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def dominio(s: str) -> str:
    u = urlsplit(s if "://" in s else "https://" + s)
    return (u.hostname or "").lower().removeprefix("www.")


def telefono(s: str) -> str:
    return ("+" if s.startswith("+") else "+57") + re.sub(r"\D", "", s)


def reglas_validas(reglas: Any) -> list[dict[str, str]]:
    if not isinstance(reglas, list):
        raise HTTPException(400, "Las reglas deben ser una lista")
    for r in reglas:
        if (
            not isinstance(r, dict)
            or r.get("campo") not in CAMPOS
            or r.get("operador") not in ("es", "no_es", "contiene", "mayor_que", "menor_que")
            or not isinstance(r.get("valor"), str)
        ):
            raise HTTPException(400, "Regla de segmento no válida")
    return reglas


def numero(v: str) -> float:
    try:
        return float(v.strip() or "0")
    except ValueError:
        return math.nan


def coincide(
    c: dict[str, Any], empresas: list[dict[str, Any]], reglas: list[dict[str, str]]
) -> bool:
    return coincide_indexado(c, {str(e["id"]): e for e in empresas}, reglas)


def coincide_indexado(
    c: dict[str, Any], empresas: dict[str, dict[str, Any]], reglas: list[dict[str, str]]
) -> bool:
    """Misma lógica que coincide() de src/lib/reglas.ts, con empresas indexadas por id."""
    e = empresas.get(str(c["empresaId"]), {})
    campos = {
        **{k: e.get(k, "") for k in ("municipio", "sector", "tamano")},
        "puntaje": str(e.get("puntaje", {}).get("total", 0)),
        "etapa": c["etapa"],
        "origen": c["origen"],
        "baseLegal": "aprobada" if c["baseLegal"]["aprobadaPorAdmin"] else "pendiente",
    }
    for r in reglas:
        if r["campo"] == "contactoIds":
            if c["id"] not in r["valor"].split(","):
                return False
            continue
        a, b, op = str(campos.get(r["campo"], "")), r["valor"], r["operador"]
        if not {
            "es": a == b,
            "no_es": a != b,
            "contiene": b.lower() in a.lower(),
            "mayor_que": numero(a) > numero(b),
            "menor_que": numero(a) < numero(b),
        }[op]:
            return False
    return True


def validar_secuencia(s: dict[str, Any], prohibidas: list[str]) -> list[str]:
    errors = []
    v = s["ventanaEnvio"]
    if not v["dias"] or v["desde"] >= v["hasta"]:
        errors.append("Ventana de envío inválida")
    if not s["pasos"]:
        errors.append("Se requiere al menos un paso")
    for i, p in enumerate(s["pasos"]):
        if i == 0 and not p.get("asunto", "").strip():
            errors.append("Paso 1: falta asunto")
        for var in ("baja", "firma"):
            if "{{" + var + "}}" not in p["cuerpo"]:
                errors.append(f"Paso {i + 1}: falta {{{{{var}}}}}")
        if not 30 <= len(p["cuerpo"]) <= 3000:
            errors.append(f"Paso {i + 1}: longitud entre 30 y 3000 caracteres")
        if p["esperaDiasHabiles"] < 0:
            errors.append("La espera no puede ser negativa")
        errors.extend(
            "Palabra prohibida: " + w for w in prohibidas if w and w.lower() in p["cuerpo"].lower()
        )
        if p.get("formato") == "html":
            # `cuerpo` ya es la versión en texto simple (se valida arriba); aquí el HTML.
            html = p.get("cuerpoHtml") or ""
            if not html.strip():
                errors.append(f"Paso {i + 1}: falta el texto con formato")
            elif len(html) > 20000:
                errors.append(f"Paso {i + 1}: el texto con formato admite hasta 20.000 caracteres")
            else:
                for var in ("baja", "firma"):
                    if "{{" + var + "}}" not in html:
                        errors.append(f"Paso {i + 1}: falta {{{{{var}}}}} en el texto con formato")
    return errors


class VentanaIncompatible(HTTPException):
    """La ventana de una secuencia y el horario global no tienen días u horas en común."""

    def __init__(self, detalle: str = "Las ventanas de envío no se cruzan") -> None:
        super().__init__(400, detalle)


def ventanas_compatibles(a: dict[str, Any], b: dict[str, Any]) -> bool:
    dias = set(a["dias"]) & set(b["dias"])
    return bool(dias) and max(a["desde"], b["desde"]) < min(a["hasta"], b["hasta"])


def siguiente_ventana(
    ahora: datetime, a: dict[str, Any], b: dict[str, Any], espera: int = 0
) -> datetime:
    dias = set(a["dias"]) & set(b["dias"])
    desde, hasta = max(a["desde"], b["desde"]), min(a["hasta"], b["hasta"])
    if not ventanas_compatibles(a, b):
        raise VentanaIncompatible()
    dt = ahora.astimezone(BOGOTA)
    festivos = holidays.country_holidays("CO", years=range(dt.year, dt.year + 3))
    while espera:
        dt += timedelta(days=1)
        if dt.weekday() < 5 and dt.date() not in festivos:
            espera -= 1
    for _ in range(740):
        if (dt.weekday() + 1) % 7 in dias and dt.date() not in festivos:
            hora = dt.strftime("%H:%M")
            if hora < desde:
                h, m = map(int, desde.split(":"))
                return dt.replace(hour=h, minute=m, second=0, microsecond=0)
            if hora < hasta:
                return dt
        dt = (dt + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    raise VentanaIncompatible("No hay una ventana disponible")
