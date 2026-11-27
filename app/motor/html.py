"""Correos con «texto con formato» o «diseño»: limpieza del HTML y su texto simple.

El HTML lo escribe el administrador (editor TipTap o editor de diseño) y se limpia siempre
en el servidor con una lista blanca:
- Formato: p, br, strong, em, ul, ol, li y a (href solo https: o {{enlace}}).
- Diseño: además h1–h3, div, span, hr, table/tbody/tr/td e img. Atributos de maquetación
  (width, align, cellpadding…) y `style` con propiedades CSS en lista blanca (sin
  position, url(), expresiones ni nada ejecutable). `img src` solo https: o /img/<uuid>
  (imágenes subidas a NEXUS, que se vuelven absolutas al enviar). Sin píxel de apertura.
El texto simple se deriva solo del HTML limpio y se guarda en `pasos.cuerpo`, que conserva
todas sus reglas (longitud, {{firma}} y {{baja}}).
"""

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Any

import nh3

ETIQUETAS = {"p", "br", "strong", "em", "ul", "ol", "li", "a"}
ETIQUETAS_DISENO = ETIQUETAS | {
    "h1",
    "h2",
    "h3",
    "div",
    "span",
    "hr",
    "table",
    "tbody",
    "tr",
    "td",
    "img",
    "b",
    "i",
}
ESTILOS = {
    "background-color",
    "border",
    "border-top",
    "border-bottom",
    "border-left",
    "border-right",
    "border-radius",
    "color",
    "display",
    "font-family",
    "font-size",
    "font-style",
    "font-weight",
    "height",
    "letter-spacing",
    "line-height",
    "margin",
    "margin-top",
    "margin-bottom",
    "margin-left",
    "margin-right",
    "max-width",
    "padding",
    "padding-top",
    "padding-bottom",
    "padding-left",
    "padding-right",
    "text-align",
    "text-decoration",
    "vertical-align",
    "width",
}
_CON_ESTILO = {
    "p",
    "div",
    "span",
    "h1",
    "h2",
    "h3",
    "td",
    "table",
    "a",
    "img",
    "ul",
    "ol",
    "li",
    "hr",
}
ATRIBUTOS = {
    **{t: {"style"} for t in _CON_ESTILO},
    "a": {"href", "style"},
    "img": {"src", "alt", "width", "height", "style"},
    "table": {"role", "width", "cellpadding", "cellspacing", "border", "align", "style"},
    "td": {"width", "align", "valign", "style"},
}
IMAGEN_NEXUS = re.compile(r"^/img/[0-9a-f-]{36}$")
ENLACE = "{{enlace}}"
# Plantilla mínima de un correo personal: sin píxel de apertura.
PLANTILLA = (
    '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
    '<body style="margin:0;padding:16px;background:#ffffff;">'
    '<div style="max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:15px;'
    'line-height:1.55;color:#1a2e24;">{cuerpo}</div></body></html>'
)
# Un diseño trae su propio fondo y ancho (tablas de 600 px).
PLANTILLA_DISENO = (
    '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
    '<body style="margin:0;padding:0;">{cuerpo}</body></html>'
)


def _href(etiqueta: str, atributo: str, valor: str) -> str | None:
    valor = valor.strip()
    if etiqueta == "a" and atributo == "href":
        if valor == ENLACE or valor.lower().startswith("https://"):
            return valor
        return None
    if etiqueta == "img" and atributo == "src":
        if IMAGEN_NEXUS.fullmatch(valor) or valor.lower().startswith("https://"):
            return valor
        return None
    if atributo in ("width", "height", "cellpadding", "cellspacing", "border"):
        return valor if re.fullmatch(r"\d{1,4}%?", valor) else None
    if atributo == "style" and re.search(r"url\s*\(|expression|javascript:|@import", valor, re.I):
        return None
    return valor


def es_diseno(html: str | None) -> bool:
    """Un cuerpo con tablas o imágenes es un diseño (se edita con el editor de diseño)."""
    return bool(html and re.search(r"<(table|img)\b", html, re.I))


def limpiar(html: str) -> str:
    """HTML seguro con la lista blanca; enlaces solo https: o {{enlace}}."""
    diseno = es_diseno(html)
    limpio = nh3.clean(
        html or "",
        tags=ETIQUETAS_DISENO if diseno else ETIQUETAS,
        attributes=ATRIBUTOS if diseno else {"a": {"href"}},
        url_schemes={"https"},
        link_rel=None,
        strip_comments=True,
        attribute_filter=_href,
        filter_style_properties=ESTILOS if diseno else None,
    ).strip()
    # Una imagen sin dirección válida (http:, data:, javascript:…) se quita entera.
    return re.sub(r"<img\b(?![^>]*\ssrc=)[^>]*>", "", limpio)


def problemas_diseno(html: str | None) -> list[str]:
    """Errores que impiden aprobar un diseño (sin texto alternativo en una imagen)."""
    if not es_diseno(html):
        return []
    sin_alt = [
        m
        for m in re.findall(r"<img\b[^>]*>", html or "", re.I)
        if not re.search(r'\salt="[^"]+\S[^"]*"|\salt="\S+"', m)
    ]
    return ["cada imagen necesita un texto alternativo (alt)"] if sin_alt else []


class _Texto(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.partes: list[str] = []
        self.listas: list[list[int]] = []  # [contador] por lista (0 = viñetas)
        self.href: str | None = None
        self.texto_enlace: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "br":
            self.partes.append("\n")
        elif tag in ("p", "h1", "h2", "h3", "table", "hr"):
            self._parrafo()
        elif tag in ("div", "tr", "td"):
            self._linea()
        elif tag in ("ul", "ol"):
            self._parrafo()
            self.listas.append([1 if tag == "ol" else 0])
        elif tag == "li":
            self._linea()
            marca = "- "
            if self.listas and self.listas[-1][0]:
                marca = f"{self.listas[-1][0]}. "
                self.listas[-1][0] += 1
            self.partes.append(marca)
        elif tag == "a":
            self.href = dict(attrs).get("href")
            self.texto_enlace = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("p", "h1", "h2", "h3", "table"):
            self._parrafo()
        elif tag in ("div", "tr", "td"):
            self._linea()
        elif tag in ("ul", "ol"):
            if self.listas:
                self.listas.pop()
            self._parrafo()
        elif tag == "a":
            texto = "".join(self.texto_enlace).strip()
            if self.href and self.href != texto:
                self.partes.append(f" ({self.href})" if texto else self.href)
            self.href = None

    def handle_data(self, data: str) -> None:
        data = re.sub(r"\s+", " ", data)
        self.partes.append(data)
        if self.href is not None:
            self.texto_enlace.append(data)

    def _linea(self) -> None:
        if self.partes and not "".join(self.partes).endswith("\n"):
            self.partes.append("\n")

    def _parrafo(self) -> None:
        actual = "".join(self.partes)
        if actual and not actual.endswith("\n\n"):
            self.partes.append("\n" if actual.endswith("\n") else "\n\n")


def a_texto(html: str) -> str:
    """Versión en texto simple de un HTML ya limpio."""
    p = _Texto()
    p.feed(html or "")
    p.close()
    texto = "".join(p.partes)
    texto = "\n".join(linea.strip() for linea in texto.splitlines())
    return re.sub(r"\n{3,}", "\n\n", texto).strip()


def preparar_pasos(secuencia: dict[str, Any]) -> dict[str, Any]:
    """Normaliza el formato de cada paso: en «html» limpia el HTML y deriva el texto."""
    pasos = []
    for p in secuencia.get("pasos", []):
        p = dict(p)
        if p.get("formato") == "html":
            limpio = limpiar(p.get("cuerpoHtml") or "")
            p["cuerpoHtml"] = limpio
            p["cuerpo"] = a_texto(limpio)
        else:
            p["formato"] = "texto"
            p.pop("cuerpoHtml", None)
        pasos.append(p)
    return {**secuencia, "pasos": pasos}


def con_plantilla(cuerpo_html: str) -> str:
    return (PLANTILLA_DISENO if es_diseno(cuerpo_html) else PLANTILLA).replace(
        "{cuerpo}", cuerpo_html
    )


def escapar(valor: str) -> str:
    return html_lib.escape(valor, quote=True)
