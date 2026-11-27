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
CAMPOS = {
    # Atributos guardados en NEXUS.
    "municipio",
    "sector",
    "tamano",
    "puntaje",
    "etapa",
    "origen",
    "baseLegal",
    "contactoIds",
    "cargo",
    "estadoCorreo",
    "estadoCuenta",
    "dominioCorreo",
    "lista",
    # Comportamientos respaldados por eventos registrados (valor «si» o «no»).
    "hizoClic",
    "respondio",
    "recibioCorreo",
}
COMPORTAMIENTOS = {"hizoClic", "respondio", "recibioCorreo"}
OPERADORES = ("es", "no_es", "contiene", "mayor_que", "menor_que")
MAX_PROFUNDIDAD = 3
MAX_REGLAS = 60


def normalizar_nombre(s: str) -> str:
    s = "".join(x for x in unicodedata.normalize("NFD", s.lower()) if not unicodedata.combining(x))
    s = re.sub(r"\bs\.?a\.?s\.?\b|\bltda\.?\b", "", s)
    return re.sub(r"[^a-z0-9]", "", s)


def dominio(s: str) -> str:
    u = urlsplit(s if "://" in s else "https://" + s)
    return (u.hostname or "").lower().removeprefix("www.")


def telefono(s: str) -> str:
    return ("+" if s.startswith("+") else "+57") + re.sub(r"\D", "", s)


def _regla_valida(r: Any, profundidad: int, cuenta: list[int]) -> None:
    cuenta[0] += 1
    if cuenta[0] > MAX_REGLAS:
        raise HTTPException(400, f"Un segmento admite hasta {MAX_REGLAS} reglas")
    if isinstance(r, dict) and "grupo" in r:
        if (
            r.get("grupo") not in ("y", "o")
            or not isinstance(r.get("reglas"), list)
            or not r["reglas"]
            or profundidad >= MAX_PROFUNDIDAD
        ):
            raise HTTPException(400, "Grupo de reglas no válido")
        for x in r["reglas"]:
            _regla_valida(x, profundidad + 1, cuenta)
        return
    if (
        not isinstance(r, dict)
        or r.get("campo") not in CAMPOS
        or r.get("operador") not in OPERADORES
        or not isinstance(r.get("valor"), str)
    ):
        raise HTTPException(400, "Regla de segmento no válida")
    if r["campo"] in COMPORTAMIENTOS and (
        r["operador"] not in ("es", "no_es") or r["valor"] not in ("si", "no")
    ):
        raise HTTPException(400, "Regla de segmento no válida")


def reglas_validas(reglas: Any) -> list[dict[str, Any]]:
    """Lista de reglas (se combinan con Y) o grupos {"grupo": "y"|"o", "reglas": [...]}."""
    if not isinstance(reglas, list):
        raise HTTPException(400, "Las reglas deben ser una lista")
    cuenta = [0]
    for r in reglas:
        _regla_valida(r, 1, cuenta)
    return reglas


def reglas_planas(reglas: list[Any]) -> list[dict[str, Any]]:
    """Todas las reglas simples, sin grupos (para resúmenes)."""
    out: list[dict[str, Any]] = []
    for r in reglas:
        if isinstance(r, dict) and "grupo" in r:
            out.extend(reglas_planas(r["reglas"]))
        else:
            out.append(r)
    return out


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
    c: dict[str, Any], empresas: dict[str, dict[str, Any]], reglas: list[Any]
) -> bool:
    """Misma lógica que coincide() de src/lib/reglas.ts, con empresas indexadas por id.

    La lista de primer nivel se combina con Y; un grupo combina sus reglas con Y u O.
    """
    e = empresas.get(str(c["empresaId"]), {})
    correo = str(c.get("correo") or "")
    campos = {
        **{k: e.get(k, "") for k in ("municipio", "sector", "tamano")},
        "puntaje": str(e.get("puntaje", {}).get("total", 0)),
        "etapa": c["etapa"],
        "origen": c["origen"],
        "baseLegal": "aprobada" if c["baseLegal"]["aprobadaPorAdmin"] else "pendiente",
        "cargo": str(c.get("cargo") or ""),
        "estadoCorreo": str(c.get("estadoCorreo") or ""),
        "estadoCuenta": str(e.get("estadoCuenta") or ""),
        "dominioCorreo": correo.split("@")[1].lower() if "@" in correo else "",
        "hizoClic": "si" if c.get("hizoClic") else "no",
        "respondio": "si" if c.get("respondio") else "no",
        "recibioCorreo": "si" if c.get("recibioCorreo") else "no",
    }

    def una(r: dict[str, Any]) -> bool:
        if "grupo" in r:
            resultados = (una(x) for x in r["reglas"])
            return any(resultados) if r["grupo"] == "o" else all(resultados)
        if r["campo"] == "contactoIds":
            dentro = bool(c["id"] in r["valor"].split(","))
            return dentro if r["operador"] != "no_es" else not dentro
        if r["campo"] == "lista":
            dentro = bool(r["valor"] in c.get("listas", ()))
            return dentro if r["operador"] != "no_es" else not dentro
        a, b, op = str(campos.get(r["campo"], "")), str(r["valor"]), r["operador"]
        return {
            "es": a.lower() == b.lower() if r["campo"] == "dominioCorreo" else a == b,
            "no_es": a.lower() != b.lower() if r["campo"] == "dominioCorreo" else a != b,
            "contiene": b.lower() in a.lower(),
            "mayor_que": numero(a) > numero(b),
            "menor_que": numero(a) < numero(b),
        }[str(op)]

    return all(una(r) for r in reglas)


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
