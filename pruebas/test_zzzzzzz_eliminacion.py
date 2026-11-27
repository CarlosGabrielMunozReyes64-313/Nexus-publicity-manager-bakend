"""Eliminar con vista previa (migración 0011) y plantilla al crear una secuencia."""

import base64
from datetime import datetime
from uuid import uuid4

from psycopg.types.json import Jsonb
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401
from test_zzzzz_flujo_aceptacion import hacer_elegibles, importar
from test_zzzzzz_plantillas_diseno import png

from app.db import transaccion, uno
from app.integraciones.gmail import GmailFalso
from app.motor.envios import RelojFalso, programar
from app.seguridad.auth import cifrar


def previa(auth, recurso, ids):  # noqa: F811
    r = auth.post("/api/eliminacion/previa", json={"recurso": recurso, "ids": ids})
    assert r.status_code == 200, r.text
    return r.json()


def eliminar(auth, recurso, ids):  # noqa: F811
    return auth.post("/api/eliminacion", json={"recurso": recurso, "ids": ids})


def contacto(auth, sufijo, nombre="Elena"):  # noqa: F811
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": nombre, "correo": f"{nombre.lower()}@{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Empresa {nombre} {sufijo}", "municipio": "Cali"},
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_secuencia_desde_plantilla_y_eliminar_contacto(auth, monkeypatch):  # noqa: F811
    sufijo = "e" + uuid4().hex[:8]
    reloj = RelojFalso(datetime.fromisoformat("2026-12-01T08:30:00-05:00"))
    import app.api.datos as datos_mod

    monkeypatch.setattr(datos_mod, "ahora", lambda: reloj.ahora())
    with transaccion("admin") as c:
        c.execute("UPDATE mensajes SET estado='cancelado' WHERE estado='programado'")
        c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
        lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        lim.update(separacionSegundos=[1, 1])
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(lim),))
        c.execute(
            "UPDATE integraciones SET estado='conectada',credenciales_cifradas=%s WHERE id='gmail'",
            (cifrar("falso"),),
        )
    try:
        ct = contacto(auth, sufijo)
        hacer_elegibles([ct["id"]])
        lista = auth.post(
            "/api/listas", json={"nombre": f"L {sufijo}", "contactoIds": [ct["id"]]}
        ).json()["id"]
        base = next(
            p
            for p in auth.get("/api/plantillas").json()
            if p["nombre"].startswith("Primer contacto")
        )
        r = auth.post(
            "/api/secuencias",
            json={
                "nombre": f"Desde plantilla {sufijo}",
                "objetivo": "Probar",
                "llamadaAccion": "Ver",
                "listaId": lista,
                "plantillaId": base["id"],
            },
        )
        assert r.status_code == 200, r.text
        seq = r.json()["id"]
        paso = auth.get(f"/api/secuencias/{seq}/borrador").json()["contenido"]["pasos"][0]
        assert paso["asunto"] == base["asunto"] and "{{senal}}" in paso["cuerpo"]
        # Editable antes de aprobar, con vista previa.
        b = auth.get(f"/api/secuencias/{seq}/borrador").json()["contenido"]
        b["pasos"][0]["asunto"] = "Asunto editado para {{empresa}}"
        assert auth.put(f"/api/secuencias/{seq}/borrador", json={"secuencia": b}).status_code == 200
        assert auth.post(f"/api/secuencias/{seq}/aprobar", json={}).json()["programacion"][
            "activada"
        ]
        programar(reloj, GmailFalso())
        with transaccion() as c:
            m = uno(
                c,
                "SELECT * FROM mensajes WHERE contacto_id=%s AND estado='programado'",
                (ct["id"],),
            )
        assert m["asunto"].startswith("Asunto editado para ")

        # Quitar de la lista ≠ eliminar el contacto.
        p = previa(auth, "contactos", [ct["id"]])
        assert p["titulo"].startswith("Elena")
        assert p["secuenciasActivas"] and p["secuenciasActivas"][0]["secuenciaId"] == seq
        assert any("1 correo(s) programado" in e for e in p["efectos"])
        assert eliminar(auth, "contactos", [ct["id"]]).status_code == 200
        with transaccion() as c:
            assert (
                uno(c, "SELECT estado FROM mensajes WHERE id=%s", (m["id"],))["estado"]
                == "cancelado"
            )
            ins = uno(c, "SELECT * FROM inscripciones WHERE contacto_id=%s", (ct["id"],))
            assert ins["estado"] == "finalizada" and ins["motivo_salida"] == "manual"
            fila = uno(c, "SELECT * FROM contactos WHERE id=%s", (ct["id"],))
            assert fila["correo"] is None and fila["anonimizado_en"] is not None
            assert not uno(
                c, "SELECT activo FROM lista_miembros WHERE contacto_id=%s", (ct["id"],)
            )["activo"]
        assert ct["id"] not in {x["id"] for x in auth.get("/api/contactos").json()}
        assert previa(auth, "contactos", [ct["id"]])["bloqueo"] == "Ya estaba eliminado"
        # Sin baja previa, el correo puede volver a importarse.
        res = importar(
            auth,
            "otra.csv",
            f"nombre,correo,empresa\nElena,elena@{sufijo}.example,Empresa Elena {sufijo}\n".encode(),
            {"existentes": "omitir"},
        )
        assert res["creados"] == 1
    finally:
        with transaccion("admin") as c:
            c.execute("UPDATE mensajes SET estado='cancelado' WHERE estado='programado'")
            c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
            c.execute(
                "UPDATE integraciones SET estado='desconectada',credenciales_cifradas=NULL WHERE id='gmail'"
            )


def test_eliminar_contacto_dado_de_baja_conserva_la_baja(auth):  # noqa: F811
    sufijo = "b" + uuid4().hex[:8]
    ct = contacto(auth, sufijo, "Bruno")
    with transaccion("admin") as c:
        c.execute(
            "INSERT INTO supresiones(correo,motivo,origen) VALUES(%s,'Pidió la baja','enlace_baja')",
            (f"bruno@{sufijo}.example",),
        )
    p = previa(auth, "contactos", [ct["id"]])
    assert any("baja registrada" in e for e in p["efectos"])
    assert eliminar(auth, "contactos", [ct["id"]]).status_code == 200
    res = importar(
        auth,
        "b.csv",
        f"nombre,correo,empresa\nBruno,bruno@{sufijo}.example,Empresa Bruno {sufijo}\n".encode(),
        {"existentes": "omitir"},
    )
    assert res["creados"] == 0 and "baja" in res["previa"]["filas"][0]["motivo"]


def test_bloqueos_y_eliminaciones_por_recurso(auth):  # noqa: F811
    sufijo = "r" + uuid4().hex[:8]
    ct = contacto(auth, sufijo, "Raul")
    # Segmento usado por una secuencia: bloqueado; libre: se elimina.
    auth.post(
        "/api/segmentos",
        json={
            "nombre": f"Seg {sufijo}",
            "reglas": [{"campo": "municipio", "operador": "es", "valor": "Cali"}],
            "justificacion": "x",
        },
    )
    seg = next(s for s in auth.get("/api/segmentos").json() if s["nombre"] == f"Seg {sufijo}")
    assert previa(auth, "segmentos", [seg["id"]])["puedeEliminar"]
    lista = auth.post(
        "/api/listas", json={"nombre": f"Lista {sufijo}", "contactoIds": [ct["id"]]}
    ).json()["id"]
    seq = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Usa lista {sufijo}",
            "objetivo": "Probar",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    ).json()["id"]
    p = previa(auth, "listas", [lista])
    assert not p["puedeEliminar"] and f"Usa lista {sufijo}" in p["bloqueo"]
    assert eliminar(auth, "listas", [lista]).status_code == 409
    auth.put(f"/api/secuencias/{seq}/audiencia", json={"segmentoId": seg["id"]})
    assert previa(auth, "listas", [lista])["puedeEliminar"]
    assert not previa(auth, "segmentos", [seg["id"]])["puedeEliminar"]
    assert eliminar(auth, "listas", [lista]).status_code == 200
    assert lista not in {x["id"] for x in auth.get("/api/listas?archivadas=true").json()}
    assert auth.get(f"/api/contactos/{ct['id']}").status_code == 200  # el contacto sigue
    # Plantillas: la galería no; las propias sí.
    galeria = next(p for p in auth.get("/api/plantillas").json() if p["predeterminada"])
    assert eliminar(auth, "plantillas", [galeria["id"]]).status_code == 409
    propia = auth.post(
        "/api/plantillas", json={"nombre": f"P {sufijo}", "cuerpo": "Hola {{firma}} {{baja}}"}
    ).json()["id"]
    # Imagen en uso por una plantilla: bloqueada.
    img = auth.post(
        "/api/imagenes", json={"nombre": "a.png", "contenido": base64.b64encode(png(3, 3)).decode()}
    ).json()
    html = (
        '<table><tr><td><img src="' + img["ruta"] + '" alt="Foto"></td></tr></table>'
        "<p>{{firma}}</p><p>{{baja}}</p>"
    )
    auth.put(
        f"/api/plantillas/{propia}",
        json={"nombre": f"P {sufijo}", "cuerpo": "x", "formato": "html", "cuerpoHtml": html},
    )
    assert "en uso" in previa(auth, "imagenes", [img["id"]])["bloqueo"]
    assert eliminar(auth, "plantillas", [propia]).status_code == 200
    assert eliminar(auth, "imagenes", [img["id"]]).status_code == 200
    # Empresa con contactos: bloqueada; sin contactos: se elimina.
    assert not previa(auth, "empresas", [ct["empresaId"]])["puedeEliminar"]
    vacia = auth.post("/api/empresas", json={"razonSocial": f"Vacía {sufijo}", "municipio": "Cali"})
    assert vacia.status_code == 200, vacia.text
    eid = next(
        e["id"] for e in auth.get("/api/empresas").json() if e["razonSocial"] == f"Vacía {sufijo}"
    )
    assert eliminar(auth, "empresas", [eid]).status_code == 200
