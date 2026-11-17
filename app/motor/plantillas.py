import re
from typing import Any


def render(
    secuencia: dict[str, Any],
    contacto: dict[str, Any],
    empresa: dict[str, Any],
    marca: dict[str, Any],
    baja_url: str,
    enlace: str,
) -> dict[str, Any]:
    respaldo: list[str] = []
    ev = contacto.get("evidencias", []) + empresa.get("evidencias", [])
    alias = {"empresa": "razonSocial", "senal": "senal"}
    values = {
        "nombre": contacto.get("nombre"),
        "cargo": contacto.get("cargo"),
        "empresa": empresa.get("razonSocial"),
        "municipio": empresa.get("municipio"),
        "senal": next((s["descripcion"] for s in empresa.get("senales", [])), None),
    }
    sistema = {"firma": marca["firma"], "baja": marca["baja"] + "\n" + baja_url, "enlace": enlace}
    defaults = {
        "nombre": "equipo",
        "empresa": "su empresa",
        "senal": "trabaja con empresas grandes de la región",
        "cargo": "su equipo",
        "municipio": "su región",
    }
    pasos = []
    for paso in secuencia["pasos"]:
        variables = {v["nombre"]: v for v in paso["variables"]}

        def reemplazar(m: re.Match[str]) -> str:
            key = m[1]
            if key in sistema:
                return str(sistema[key])
            campo = variables.get(key, {}).get("campoFuente", alias.get(key, key)).split(".")[-1]
            value = values.get(key)
            verified = any(
                e["campo"] == campo and e["estado"] == "verificado" and e.get("valor") == value
                for e in ev
            )
            if value and verified:
                return str(value)
            if key not in respaldo:
                respaldo.append(key)
            return str(variables.get(key, {}).get("respaldo", defaults.get(key, "")))

        p = {**paso, "cuerpo": re.sub(r"\{\{(\w+)\}\}", reemplazar, paso["cuerpo"])}
        if paso.get("asunto"):
            p["asunto"] = re.sub(r"\{\{(\w+)\}\}", reemplazar, paso["asunto"])
        pasos.append(p)
    return {"pasos": pasos, "respaldo": respaldo}
