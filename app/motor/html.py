"""Correos con «texto con formato»: limpieza del HTML y su versión en texto simple.

El HTML lo escribe el administrador (editor TipTap) y se limpia siempre en el servidor
con una lista blanca: p, br, strong, em, ul, ol, li y a (href solo https: o {{enlace}}).
El texto simple se deriva solo del HTML limpio y se guarda en `pasos.cuerpo`, que conserva
todas sus reglas (longitud, {{firma}} y {{baja}}).
"""

import html as html_lib
import re
from html.parser import HTMLParser
from typing import Any

import nh3

ETIQUETAS = {"p", "br", "strong", "em", "ul", "ol", "li", "a"}
ENLACE = "{{enlace}}"
# Plantilla mínima de un correo personal: sin imágenes ni píxel de apertura.
PLANTILLA = (
    '<!DOCTYPE html><html lang="es"><head><meta charset="utf-8">'
    '<meta name="viewport" content="width=device-width, initial-scale=1"></head>'
    '<body style="margin:0;padding:16px;background:#ffffff;">'
    '<div style="max-width:600px;font-family:Arial,Helvetica,sans-serif;font-size:15px;'
    'line-height:1.55;color:#1a2e24;">{cuerpo}</div></body></html>'
)


def _href(etiqueta: str, atributo: str, valor: str) -> str | None:
    if etiqueta == "a" and atributo == "href":
        valor = valor.strip()
        if valor == ENLACE or valor.lower().startswith("https://"):
            return valor
        return None
    return valor


def limpiar(html: str) -> str:
    """HTML seguro con la lista blanca; enlaces solo https: o {{enlace}}."""
    return nh3.clean(
        html or "",
        tags=ETIQUETAS,
        attributes={"a": {"href"}},
        url_schemes={"https"},
        link_rel=None,
        strip_comments=True,
        attribute_filter=_href,
    ).strip()


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
        elif tag == "p":
            self._parrafo()
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
        if tag == "p":
            self._parrafo()
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
    return PLANTILLA.format(cuerpo=cuerpo_html)


def escapar(valor: str) -> str:
    return html_lib.escape(valor, quote=True)
