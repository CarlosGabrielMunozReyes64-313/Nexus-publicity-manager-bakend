"""Imágenes para correos con diseño, guardadas en la base y publicadas en /img/<uuid>.

Solo PNG, JPEG o GIF (los formatos que muestran todos los programas de correo), hasta
1,5 MB. El tipo se comprueba por los bytes, no por el nombre. No se borran: un correo ya
enviado puede seguir mostrándolas.
"""

import base64
import binascii
from typing import Any

from fastapi import HTTPException

from app.config import config
from app.db import Conn, Row, auditar, insertar, todos

MAX_BYTES = 1_500_000
FIRMAS = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def _armar(r: Row) -> Row:
    ruta = f"/img/{r['id']}"
    return {
        "id": r["id"],
        "nombre": r["nombre"],
        "tipo": r["tipo"],
        "tamano": r["tamano"],
        "ejemplo": r["ejemplo"],
        "creadoEn": r["creado_en"],
        "ruta": ruta,
        "url": config().url_publica(ruta),
    }


def lista(c: Conn) -> list[Row]:
    return [
        _armar(r)
        for r in todos(
            c,
            "SELECT id,nombre,tipo,tamano,ejemplo,creado_en FROM imagenes "
            "ORDER BY ejemplo, creado_en DESC LIMIT 300",
        )
    ]


def subir(c: Conn, body: Any) -> Row:
    body = body if isinstance(body, dict) else {}
    nombre = " ".join(str(body.get("nombre") or "imagen").split())[:200] or "imagen"
    contenido = str(body.get("contenido") or "")
    if contenido.startswith("data:") and "," in contenido[:100]:
        contenido = contenido.split(",", 1)[1]
    try:
        datos = base64.b64decode(contenido, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(400, "La imagen no llegó completa; vuelva a elegirla") from exc
    if not datos:
        raise HTTPException(400, "Elija una imagen")
    if len(datos) > MAX_BYTES:
        raise HTTPException(413, "La imagen supera 1,5 MB: redúzcala (ideal: 1200 px de ancho)")
    tipo = next((t for firma, t in FIRMAS if datos.startswith(firma)), None)
    if not tipo:
        raise HTTPException(
            400,
            "Formato no admitido: use PNG, JPG o GIF (WebP y SVG no se ven en todos los correos)",
        )
    r = insertar(
        c, "imagenes", {"nombre": nombre, "tipo": tipo, "datos": datos, "tamano": len(datos)}
    )
    auditar(c, "subir_imagen", "imagenes", str(r["id"]))
    return {"ok": True, **_armar(r)}
