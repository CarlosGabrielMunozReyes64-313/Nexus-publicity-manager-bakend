"""Plantillas con diseño e imágenes alojadas en NEXUS (migración 0010)."""

import base64
import struct
import zlib

from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.config import config
from app.motor.html import a_texto, es_diseno, limpiar, problemas_diseno


def png(ancho: int = 2, alto: int = 2) -> bytes:
    """PNG mínimo válido (sin dependencias)."""

    def trozo(tipo: bytes, datos: bytes) -> bytes:
        return (
            struct.pack(">I", len(datos))
            + tipo
            + datos
            + struct.pack(">I", zlib.crc32(tipo + datos) & 0xFFFFFFFF)
        )

    filas = b"".join(b"\x00" + b"\x0f\x4c\x3a" * ancho for _ in range(alto))
    return (
        b"\x89PNG\r\n\x1a\n"
        + trozo(b"IHDR", struct.pack(">IIBBBBB", ancho, alto, 8, 2, 0, 0, 0))
        + trozo(b"IDAT", zlib.compress(filas))
        + trozo(b"IEND", b"")
    )


def test_galeria_predeterminada_y_duplicar(auth):  # noqa: F811
    todas = auth.get("/api/plantillas").json()
    base = [p for p in todas if p["predeterminada"]]
    assert len(base) == 6
    assert sum(p["diseno"] for p in base) == 5
    assert todas[0]["predeterminada"]  # la galería va primero
    disenada = next(p for p in base if p["nombre"] == "Invitación al diagnóstico RSE")
    assert "/img/" in disenada["cuerpoHtml"] and "{{baja}}" in disenada["cuerpo"]
    # No se modifica: se duplica para adaptarla.
    r = auth.put(f"/api/plantillas/{disenada['id']}", json={**disenada, "nombre": "X"})
    assert r.status_code == 409
    assert auth.post(f"/api/plantillas/{disenada['id']}/archivar", json={}).status_code == 409
    copia = auth.post(f"/api/plantillas/{disenada['id']}/duplicar", json={}).json()["id"]
    mia = auth.get(f"/api/plantillas/{copia}").json()
    assert not mia["predeterminada"] and mia["diseno"]
    assert mia["nombre"].endswith("(mi versión)")
    r = auth.put(
        f"/api/plantillas/{copia}",
        json={
            "nombre": "Mi invitación",
            "asunto": mia["asunto"],
            "cuerpo": mia["cuerpo"],
            "formato": "html",
            "cuerpoHtml": mia["cuerpoHtml"].replace("15 minutos", "10 minutos"),
        },
    )
    assert r.status_code == 200, r.text
    # La vista previa usa direcciones absolutas y públicas para las imágenes.
    v = auth.post(f"/api/plantillas/{copia}/previsualizar", json={}).json()
    assert config().url_publica("/img/") in v["html"]
    assert "10 minutos" in v["texto"] and "Darme de baja" in v["texto"]


def test_subir_imagen_y_publicarla_sin_sesion(auth, client):  # noqa: F811
    r = auth.post(
        "/api/imagenes", json={"nombre": "logo.png", "contenido": base64.b64encode(png()).decode()}
    )
    assert r.status_code == 200, r.text
    img = r.json()
    assert img["ruta"] == f"/img/{img['id']}" and img["url"].endswith(img["ruta"])
    assert any(x["id"] == img["id"] for x in auth.get("/api/imagenes").json())
    publica = client.get(img["ruta"])
    assert publica.status_code == 200
    assert publica.headers["content-type"] == "image/png"
    assert publica.content == png()
    assert client.get("/img/no-es-un-uuid").status_code == 404
    svg = base64.b64encode(b"<svg onload='x'></svg>").decode()
    assert auth.post("/api/imagenes", json={"nombre": "x.svg", "contenido": svg}).status_code == 400
    grande = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 1_600_000).decode()
    assert (
        auth.post("/api/imagenes", json={"nombre": "g.png", "contenido": grande}).status_code == 413
    )


def test_limpieza_del_diseno():
    sucio = (
        '<table role="presentation" width="600" onclick="x()"><tr>'
        '<td style="background-color:#0f4c3a;position:fixed;padding:8px">'
        '<img src="/img/11111111-0000-4000-8000-000000000001" alt="Logo" width="140">'
        '<img src="http://inseguro.example/x.png" alt="x">'
        '<img src="data:image/png;base64,AAAA" alt="y">'
        '<div style="background-image:url(https://rastreo.example/p.gif)">pixel</div>'
        '<a href="{{enlace}}" style="color:#fff;text-decoration:none">Botón</a>'
        "<script>alert(1)</script></td></tr></table><p>{{firma}}</p><p>{{baja}}</p>"
    )
    limpio = limpiar(sucio)
    assert es_diseno(limpio)
    for prohibido in ("onclick", "position", "http://", "data:", "url(", "<script", "rastreo"):
        assert prohibido not in limpio
    assert limpio.count("<img") == 1 and 'alt="Logo"' in limpio
    assert 'style="background-color:#0f4c3a;padding:8px"' in limpio
    assert a_texto(limpio).endswith("Botón ({{enlace}})\n\n{{firma}}\n\n{{baja}}")
    assert problemas_diseno(
        '<table><tr><td><img src="/img/11111111-0000-4000-8000-000000000001" alt=""></td></tr></table>'
    )


def test_secuencia_con_diseno_advierte_y_exige_alt(auth):  # noqa: F811
    base = next(
        p
        for p in auth.get("/api/plantillas").json()
        if p["nombre"] == "Seguimiento con recordatorio"
    )
    lista = auth.post("/api/listas", json={"nombre": "Diseño de prueba"}).json()["id"]
    seq = auth.post(
        "/api/secuencias",
        json={
            "nombre": "Con diseño",
            "objetivo": "Probar",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    ).json()["id"]
    b = auth.get(f"/api/secuencias/{seq}/borrador").json()["contenido"]
    b["pasos"][0].update(formato="html", cuerpoHtml=base["cuerpoHtml"], asunto="Hola {{empresa}}")
    assert auth.put(f"/api/secuencias/{seq}/borrador", json={"secuencia": b}).status_code == 200
    rev = auth.get(f"/api/secuencias/{seq}/revision").json()
    assert any("Promociones" in a for a in rev["advertencias"])
    b["pasos"][0]["cuerpoHtml"] = base["cuerpoHtml"].replace('alt="NEXUS Innovación"', 'alt=""')
    auth.put(f"/api/secuencias/{seq}/borrador", json={"secuencia": b})
    rev = auth.get(f"/api/secuencias/{seq}/revision").json()
    assert any("texto alternativo" in e for e in rev["errores"]) and not rev["puedeAprobar"]
