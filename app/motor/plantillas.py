import re
from html import escape
from typing import Any, Callable

# Variables que NEXUS sabe reemplazar. Un paso también puede declarar las suyas en
# «variables» (con campo fuente y respaldo).
VARIABLES_DATOS = ("nombre", "cargo", "empresa", "municipio", "senal")
VARIABLES_SISTEMA = ("firma", "baja", "enlace")
VARIABLES_ADMITIDAS = VARIABLES_DATOS + VARIABLES_SISTEMA
ETIQUETA_BAJA = "Rechazar / Darme de baja"


def variables_de(*textos: str | None) -> list[str]:
    vistas: list[str] = []
    for t in textos:
        for m in re.findall(r"\{\{\s*(\w+)\s*\}\}", t or ""):
            if m not in vistas:
                vistas.append(m)
    return vistas


def variables_no_admitidas(secuencia: dict[str, Any]) -> list[str]:
    """Errores «Correo N: variable {{x}} no admitida» (se envían en blanco si no)."""
    errores = []
    for i, p in enumerate(secuencia.get("pasos", []), 1):
        propias = {v.get("nombre") for v in p.get("variables", []) if isinstance(v, dict)}
        for var in variables_de(p.get("asunto"), p.get("cuerpo"), p.get("cuerpoHtml")):
            if var not in VARIABLES_ADMITIDAS and var not in propias:
                errores.append(
                    f"Correo {i}: la variable {{{{{var}}}}} no existe en NEXUS "
                    f"(admitidas: {', '.join('{{' + x + '}}' for x in VARIABLES_ADMITIDAS)})"
                )
    return errores


def con_pie_de_baja(cuerpo: str, html: bool = False) -> str:
    """Garantiza el enlace de baja aunque la plantilla haya omitido el pie."""
    if "{{baja}}" in cuerpo:
        return cuerpo
    return cuerpo + ("<p>{{baja}}</p>" if html else "\n\n{{baja}}")


def _enlace(url: str) -> str:
    """Enlace visible; en vistas previas y pruebas (sin URL real) solo el texto."""
    u = escape(url, quote=True)
    return f'<a href="{u}">{u}</a>' if url.startswith(("https://", "http://")) else u


def _html(
    cuerpo: str,
    reemplazar: Callable[[re.Match[str]], str],
    enlace: str,
    texto_baja: str,
    baja_url: str,
) -> str:
    """Mismas variables que el texto simple, pero seguras dentro del HTML.

    Los valores van escapados (un dato nunca aporta HTML). `href="{{enlace}}"` recibe la URL
    de seguimiento; `{{enlace}}` suelto se vuelve un enlace visible y `{{baja}}` muestra el
    texto de baja con su enlace, igual que en el texto simple.
    """
    cuerpo = cuerpo.replace('href="{{enlace}}"', 'href="' + escape(enlace, quote=True) + '"')

    def valor(m: re.Match[str]) -> str:
        if m[1] == "enlace":
            return _enlace(enlace)
        if m[1] == "baja":
            u = escape(baja_url, quote=True)
            enlace_baja = (
                f'<a href="{u}">{ETIQUETA_BAJA}</a>'
                if baja_url.startswith(("https://", "http://"))
                else f"{ETIQUETA_BAJA}: {u}"
            )
            return escape(texto_baja, quote=True).replace("\n", "<br>") + "<br>" + enlace_baja
        return escape(reemplazar(m), quote=True).replace("\n", "<br>")

    return re.sub(r"\{\{(\w+)\}\}", valor, cuerpo)


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
    sistema = {
        "firma": marca["firma"],
        "baja": marca["baja"] + "\n" + ETIQUETA_BAJA + ": " + baja_url,
        "enlace": enlace,
    }
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

        p = {
            **paso,
            "cuerpo": re.sub(r"\{\{(\w+)\}\}", reemplazar, con_pie_de_baja(paso["cuerpo"])),
        }
        if paso.get("asunto"):
            p["asunto"] = re.sub(r"\{\{(\w+)\}\}", reemplazar, paso["asunto"])
        if paso.get("formato") == "html" and paso.get("cuerpoHtml"):
            p["cuerpoHtml"] = _html(
                con_pie_de_baja(paso["cuerpoHtml"], html=True),
                reemplazar,
                enlace,
                marca["baja"],
                baja_url,
            )
        pasos.append(p)
    return {"pasos": pasos, "respaldo": respaldo}
