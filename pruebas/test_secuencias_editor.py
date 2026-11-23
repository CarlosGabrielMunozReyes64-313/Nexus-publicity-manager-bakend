# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)
"""Editor de secuencias: borrador, revisión, aprobación, prueba, archivo y migración 0005."""

import os
import subprocess
import sys
from typing import Any
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401
from test_propuestas_agentes import proveedor_listo, segmento_con_contactos
from test_rutas_correo import agente, cancelar_todo, ejecutar_tarea, ruta, ultima_tarea  # noqa: F401

from app.api import secuencias as sec
from app.api.campanas import aprobar, nueva_propuesta
from app.api.lecturas import secuencia
from app.config import config
from app.db import actualizar, insertar, todos, transaccion, uno
from app.esquemas.serializar import serializar
from app.integraciones.gmail import GmailFalso
from app.seguridad.auth import cifrar

CUERPO = "Hola {{nombre}}, le invitamos al diagnóstico gratuito: {{enlace}}\n{{firma}}\n{{baja}}"


def admin_id(c: Any) -> str:
    a = str(
        uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))["id"]
    )
    c.execute("SELECT set_config('nexus.administrador_id',%s,true)", (a,))
    return a


def crear_campana(activa: bool = False, contactos: int = 2) -> dict[str, Any]:
    """Segmento aprobado, secuencia de 2 correos, campaña aprobada (y activa si se pide)."""
    with transaccion("admin") as c:
        admin = admin_id(c)
        e = insertar(
            c, "empresas", {"razon_social": f"Editor {uuid4().hex}", "municipio": "Palmira"}
        )
        ids = []
        for i in range(contactos):
            ct = insertar(
                c,
                "contactos",
                {
                    "empresa_id": e["id"],
                    "nombre": f"Ficticio {i}",
                    "correo": f"persona{i}@{uuid4().hex}.example",
                    "estado_correo": "valido",
                    "origen": "manual",
                },
            )
            insertar(
                c,
                "bases_legales",
                {
                    "contacto_id": ct["id"],
                    "tipo": "autorizacion_registrada",
                    "texto": "Consentimiento ficticio",
                    "prueba": "Fixture de prueba",
                    "aprobada_por": admin,
                    "aprobada_en": "now()",
                },
            )
            ids.append(ct)
        seg = insertar(
            c,
            "segmentos",
            {
                "nombre": f"Segmento editor {uuid4().hex[:6]}",
                "reglas": Jsonb(
                    [
                        {
                            "campo": "contactoIds",
                            "operador": "es",
                            "valor": ",".join(str(x["id"]) for x in ids),
                        }
                    ]
                ),
                "propuesto_por": "admin",
                "estado": "aprobado",
                "aprobado_por": admin,
                "aprobado_en": "now()",
            },
        )
        seq = insertar(
            c,
            "secuencias",
            {
                "nombre": "Secuencia editor",
                "objetivo": "Diagnóstico",
                "llamada_accion": "Completar el diagnóstico",
                "ventana_dias": [2, 3, 4],
                "ventana_desde": "08:00",
                "ventana_hasta": "10:30",
            },
        )
        for n in (1, 2):
            insertar(
                c,
                "pasos",
                {
                    "secuencia_id": seq["id"],
                    "orden": n,
                    "asunto": "Diagnóstico gratuito" if n == 1 else None,
                    "cuerpo": CUERPO,
                    "espera_dias_habiles": 0 if n == 1 else 4,
                    "mismo_hilo": n > 1,
                },
            )
        cam = insertar(
            c,
            "campanas",
            {
                "nombre": "Campaña editor",
                "segmento_id": seg["id"],
                "secuencia_id": seq["id"],
                "estado": "en_revision",
            },
        )
        prop = nueva_propuesta(
            c,
            "estratega_redactor",
            "campana",
            "Ruta de prueba",
            serializar(secuencia(c, seq)),
            {"objetivo_campana_id": cam["id"]},
        )
        aprobar(c, prop, admin)
        mensaje = None
        if activa:
            actualizar(c, "campanas", str(cam["id"]), {"estado": "activa"})
            cam = uno(c, "SELECT * FROM campanas WHERE id=%s", (cam["id"],))
            inscripciones = [
                insertar(
                    c,
                    "inscripciones",
                    {
                        "campana_id": cam["id"],
                        "contacto_id": ct["id"],
                        "estado": "activa",
                        "proximo_envio": "now()",
                    },
                )
                for ct in ids
            ]
            mensaje = insertar(
                c,
                "mensajes",
                {
                    "tipo": "secuencia",
                    "contacto_id": ids[0]["id"],
                    "inscripcion_id": inscripciones[0]["id"],
                    "aprobacion_id": cam["aprobacion_vigente_id"],
                    "paso_orden": 1,
                    "clave_idempotencia": f"prueba:{uuid4()}",
                    "destinatario": ids[0]["correo"],
                    "asunto": "Diagnóstico gratuito",
                    "cuerpo": "Hola, cuerpo ya personalizado para la prueba del editor.",
                    "programado_para": "now()",
                },
            )
        cam = uno(c, "SELECT * FROM campanas WHERE id=%s", (cam["id"],))
    return {
        "seq": str(seq["id"]),
        "cam": cam,
        "prop": str(prop["id"]),
        "contactos": [str(x["id"]) for x in ids],
        "mensaje": str(mensaje["id"]) if mensaje else None,
    }


def gmail(estado: str) -> None:
    with transaccion("admin") as c:
        valores: dict[str, Any] = {"estado": estado}
        if estado == "conectada":
            valores["credenciales_cifradas"] = cifrar("falso")
        actualizar(c, "integraciones", "gmail", valores)


@pytest.fixture
def sin_gmail():
    gmail("desconectada")
    yield
    gmail("desconectada")


def borrador(auth, seq: str) -> dict[str, Any]:
    r = auth.get(f"/api/secuencias/{seq}/borrador")
    assert r.status_code == 200, r.text
    return r.json()


def con_cambio(contenido: dict[str, Any], orden: int, cuerpo: str) -> dict[str, Any]:
    nuevo = {k: v for k, v in contenido.items() if k != "notasAgente"}
    nuevo["pasos"] = [dict(p) for p in contenido["pasos"]]
    nuevo["pasos"][orden - 1]["cuerpo"] = cuerpo
    return nuevo


def leer(seq: str, sql: str, *params: Any) -> Any:
    with transaccion("admin") as c:
        return todos(c, sql, params or (seq,))


# 1 ------------------------------------------------------------------------------------
def test_borrador_de_secuencia_activa_no_cambia_los_envios(auth, sin_gmail):
    x = crear_campana(activa=True)
    b = borrador(auth, x["seq"])
    assert b["version"] == 1 and b["autor"] == "agente"
    nuevo = con_cambio(
        b["contenido"], 1, "Hola {{nombre}}, texto nuevo del borrador.\n{{firma}}\n{{baja}}"
    )
    r = auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    # Guardar lo mismo otra vez no crea otra versión.
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    with transaccion("admin") as c:
        cam = uno(c, "SELECT * FROM campanas WHERE id=%s", (x["cam"]["id"],))
        ins = todos(c, "SELECT estado FROM inscripciones WHERE campana_id=%s", (cam["id"],))
        m = uno(c, "SELECT estado FROM mensajes WHERE id=%s", (x["mensaje"],))
        pasos = todos(
            c, "SELECT cuerpo FROM pasos WHERE secuencia_id=%s ORDER BY orden", (x["seq"],)
        )
    assert cam["estado"] == "activa"
    assert cam["aprobacion_vigente_id"] == x["cam"]["aprobacion_vigente_id"]
    assert {i["estado"] for i in ins} == {"activa"}
    assert m["estado"] == "programado"
    assert pasos[0]["cuerpo"] == CUERPO  # la secuencia guarda lo aprobado
    s = auth.get(f"/api/secuencias/{x['seq']}").json()
    assert s["estadoEditor"] == "cambios_sin_aprobar"
    assert s["versionBorrador"] == 2 and s["versionAprobada"] == 1
    assert s["campanaId"] == str(cam["id"]) and s["campanaEstado"] == "activa"
    assert s["archivada"] is False and "actualizadaEn" in s
    # PUT /secuencias/:id (frontend anterior) también guarda un borrador.
    viejo = {**s, "pasos": [dict(p) for p in s["pasos"]]}
    viejo["pasos"][1]["cuerpo"] = "Hola {{nombre}}, desde el editor anterior.\n{{firma}}\n{{baja}}"
    assert auth.put(f"/api/secuencias/{x['seq']}", json=viejo).status_code == 200
    assert borrador(auth, x["seq"])["version"] == 3
    with transaccion("admin") as c:
        assert uno(c, "SELECT estado FROM campanas WHERE id=%s", (cam["id"],))["estado"] == "activa"


# 2 ------------------------------------------------------------------------------------
def test_borrador_con_errores_se_guarda_pero_no_se_aprueba(auth, sin_gmail):
    x = crear_campana()
    b = borrador(auth, x["seq"])
    malo = con_cambio(b["contenido"], 2, "Sin pie. Resultado garantizado para todos.")
    r = auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": malo})
    assert r.status_code == 200, r.text
    rev = auth.get(f"/api/secuencias/{x['seq']}/revision").json()
    assert rev["puedeAprobar"] is False and rev["hayCambiosSinAprobar"] is True
    assert "Correo 2: falta {{baja}}" in rev["errores"]
    assert "Palabra prohibida: garantizado" in rev["errores"]
    assert rev["elegibles"] == 2 and rev["gmailConectado"] is False
    assert any("Gmail no está conectado" in a for a in rev["advertencias"])
    r = auth.post(f"/api/secuencias/{x['seq']}/aprobar", json={})
    assert r.status_code == 400
    assert r.json()["message"] == ". ".join(rev["bloqueos"] + rev["errores"])
    # Una forma ilegible sí se rechaza (400), sin crear versión.
    r = auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": {"pasos": "x"}})
    assert r.status_code == 400 and "formato esperado" in r.json()["message"]
    assert borrador(auth, x["seq"])["version"] == 2


# 3 ------------------------------------------------------------------------------------
def test_aprobar_copia_congela_y_no_crea_mensajes(auth, sin_gmail):
    x = crear_campana(activa=True)
    b = borrador(auth, x["seq"])
    nuevo = con_cambio(
        b["contenido"], 2, "Hola {{nombre}}, segundo correo aprobado.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    with transaccion("admin") as c:
        antes = uno(c, "SELECT count(*) AS n FROM mensajes")["n"]
    r = auth.post(f"/api/secuencias/{x['seq']}/aprobar", json={})
    assert r.status_code == 200, r.text
    with transaccion("admin") as c:
        cam = uno(c, "SELECT * FROM campanas WHERE id=%s", (x["cam"]["id"],))
        a = uno(
            c, "SELECT * FROM aprobaciones_campana WHERE id=%s", (cam["aprobacion_vigente_id"],)
        )
        pasos = todos(
            c, "SELECT cuerpo FROM pasos WHERE secuencia_id=%s ORDER BY orden", (x["seq"],)
        )
        despues = uno(c, "SELECT count(*) AS n FROM mensajes")["n"]
        ins = todos(c, "SELECT estado FROM inscripciones WHERE campana_id=%s", (cam["id"],))
    assert cam["aprobacion_vigente_id"] != x["cam"]["aprobacion_vigente_id"]
    assert len(a["huella"]) == 64
    assert [p["cuerpo"] for p in pasos] == [p["cuerpo"] for p in nuevo["pasos"]]
    assert (
        a["contenido_congelado"]["secuencia"]["pasos"][1]["cuerpo"] == nuevo["pasos"][1]["cuerpo"]
    )
    assert despues == antes  # aprobar no crea mensajes
    # Estaba activa: queda aprobada con los envíos pausados (disparadores actuales).
    assert cam["estado"] == "aprobada" and {i["estado"] for i in ins} == {"pausada"}
    s = auth.get(f"/api/secuencias/{x['seq']}").json()
    assert s["estadoEditor"] == "aprobada" and s["versionAprobada"] == 2
    rev = auth.get(f"/api/secuencias/{x['seq']}/revision").json()
    assert rev["hayCambiosSinAprobar"] is False and sec.SIN_CAMBIOS in rev["bloqueos"]
    assert auth.post(f"/api/secuencias/{x['seq']}/aprobar", json={}).status_code == 400


def test_aprobar_y_reanudar(auth, sin_gmail):
    x = crear_campana(activa=True)
    b = borrador(auth, x["seq"])
    nuevo = con_cambio(
        b["contenido"], 1, "Hola {{nombre}}, versión para reanudar.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    # Sin Gmail: se aprueba, no se reanuda, y se informa el motivo.
    r = auth.post(f"/api/secuencias/{x['seq']}/aprobar-y-reanudar", json={})
    assert r.status_code == 400 and r.json()["message"] == "Gmail está desconectado"
    s = auth.get(f"/api/secuencias/{x['seq']}").json()
    assert s["campanaEstado"] == "aprobada" and s["versionAprobada"] == 2
    # Ya no está activa: aprobar y reanudar no aplica.
    r = auth.post(f"/api/secuencias/{x['seq']}/aprobar-y-reanudar", json={})
    assert r.status_code == 409

    y = crear_campana(activa=True)
    gmail("conectada")
    b = borrador(auth, y["seq"])
    nuevo = con_cambio(
        b["contenido"], 1, "Hola {{nombre}}, cambio con reanudación.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{y['seq']}/borrador", json={"secuencia": nuevo})
    r = auth.post(f"/api/secuencias/{y['seq']}/aprobar-y-reanudar", json={})
    assert r.status_code == 200, r.text
    with transaccion("admin") as c:
        cam = uno(c, "SELECT estado FROM campanas WHERE id=%s", (y["cam"]["id"],))
        ins = todos(c, "SELECT estado FROM inscripciones WHERE campana_id=%s", (y["cam"]["id"],))
    assert cam["estado"] == "activa" and {i["estado"] for i in ins} == {"activa"}
    assert auth.get(f"/api/secuencias/{y['seq']}").json()["estadoEditor"] == "activa"


# 4 ------------------------------------------------------------------------------------
def test_cambios_de_marca_y_segmento_no_tapan_un_borrador(auth, sin_gmail):
    x = crear_campana()
    y = crear_campana()
    b = borrador(auth, x["seq"])
    nuevo = con_cambio(
        b["contenido"], 1, "Hola {{nombre}}, borrador más nuevo.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    marca = auth.get("/api/configuracion/marca").json()
    r = auth.put("/api/configuracion/marca", json={**marca, "tono": marca["tono"] + " "})
    assert r.status_code == 200, r.text
    bx = borrador(auth, x["seq"])
    assert (
        bx["version"] == 2 and bx["contenido"]["pasos"][0]["cuerpo"] == nuevo["pasos"][0]["cuerpo"]
    )
    # La campaña sin borrador recibe una copia de lo aprobado para aprobarla otra vez.
    by = borrador(auth, y["seq"])
    assert by["version"] == 2 and by["contenido"]["pasos"][0]["cuerpo"] == CUERPO
    s = auth.get(f"/api/secuencias/{y['seq']}").json()
    assert s["estadoEditor"] == "cambios_sin_aprobar" and s["campanaEstado"] == "en_revision"
    assert auth.post(f"/api/secuencias/{y['seq']}/aprobar", json={}).status_code == 200
    assert auth.get(f"/api/secuencias/{y['seq']}").json()["estadoEditor"] == "aprobada"
    # Cambiar el segmento tampoco tapa el borrador.
    r = auth.patch(
        f"/api/segmentos/{x['cam']['segmento_id']}", json={"justificacion": "Ajuste " + uuid4().hex}
    )
    assert r.status_code == 200, r.text
    assert borrador(auth, x["seq"])["version"] == 2


# 5 ------------------------------------------------------------------------------------
def test_vista_previa_sin_guardar_y_envio_de_prueba(auth, sin_gmail, monkeypatch):
    x = crear_campana()
    b = borrador(auth, x["seq"])
    sin_guardar = con_cambio(
        b["contenido"], 1, "Hola {{nombre}}, texto aún sin guardar.\n{{firma}}\n{{baja}}"
    )
    r = auth.post(
        f"/api/secuencias/{x['seq']}/previsualizar",
        json={"contactoId": x["contactos"][0], "secuencia": sin_guardar},
    )
    assert r.status_code == 200, r.text
    assert "texto aún sin guardar" in r.json()["pasos"][0]["cuerpo"]
    assert "{{" not in r.json()["pasos"][0]["cuerpo"]
    assert borrador(auth, x["seq"])["version"] == 1  # no se guardó nada
    r = auth.post(
        f"/api/secuencias/{x['seq']}/previsualizar", json={"contactoId": x["contactos"][0]}
    )
    assert CUERPO.split(",")[1].strip()[:20] in r.json()["pasos"][0]["cuerpo"]

    r = auth.post(f"/api/secuencias/{x['seq']}/prueba", json={"contactoId": x["contactos"][0]})
    assert r.status_code == 400 and "Conecte Gmail" in r.json()["message"]
    gmail("conectada")
    falso = GmailFalso()
    enviados: list[Any] = []
    monkeypatch.setattr(
        falso, "enviar", lambda m, h: enviados.append(m) or {"id": "x", "threadId": "t"}
    )
    monkeypatch.setattr(sec, "proveedor_gmail", lambda c: falso)
    with transaccion("admin") as c:
        mensajes_antes = uno(c, "SELECT count(*) AS n FROM mensajes")["n"]
    r = auth.post(
        f"/api/secuencias/{x['seq']}/prueba",
        json={"contactoId": x["contactos"][0], "orden": 1, "secuencia": sin_guardar},
    )
    assert r.status_code == 200, r.text
    assert r.json()["destinatario"] == config().admin_correo and r.json()["enviados"] == 1
    m = enviados[0]
    assert m["To"] == config().admin_correo
    assert m["Subject"] == "[Prueba] Diagnóstico gratuito"
    texto = m.get_body(preferencelist=("plain",)).get_content()
    assert "texto aún sin guardar" in texto and "\n#" in texto and "/r/" not in texto
    with transaccion("admin") as c:
        assert uno(c, "SELECT count(*) AS n FROM mensajes")["n"] == mensajes_antes
        usadas = uno(c, "SELECT count(*) AS n FROM registro_auditoria WHERE accion='envio_prueba'")[
            "n"
        ]
        # Límite diario: se completan 19 y una prueba de 2 correos ya no cabe.
        for _ in range(19 - usadas):
            c.execute(
                "INSERT INTO registro_auditoria(actor,accion,entidad,entidad_id) "
                "VALUES('admin','envio_prueba','secuencias',%s)",
                (x["seq"],),
            )
    r = auth.post(f"/api/secuencias/{x['seq']}/prueba", json={"contactoId": x["contactos"][0]})
    assert r.status_code == 429 and "quedan 1" in r.json()["message"]
    assert len(enviados) == 1


# Rutas de crear, duplicar, archivar, versiones y otra versión ---------------------------
def test_crear_duplicar_archivar_y_restaurar(auth, sin_gmail):
    x = crear_campana()
    seg = str(x["cam"]["segmento_id"])
    r = auth.post("/api/secuencias", json={"nombre": "x", "segmentoId": seg})
    assert r.status_code == 400
    r = auth.post(
        "/api/secuencias",
        json={
            "nombre": "Nueva en blanco",
            "segmentoId": seg,
            "objetivo": "Agendar una llamada",
            "llamadaAccion": "Responder este correo",
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True, r.text
    nueva = r.json()["id"]
    s = auth.get(f"/api/secuencias/{nueva}").json()
    assert s["estadoEditor"] == "borrador" and s["campanaEstado"] == "borrador"
    assert s["versionBorrador"] == 1 and "versionAprobada" not in s
    b = borrador(auth, nueva)
    assert b["autor"] == "admin" and "{{enlace}}" in b["contenido"]["pasos"][0]["cuerpo"]
    assert auth.get(f"/api/secuencias/{nueva}/revision").json()["errores"] == []

    r = auth.post(f"/api/secuencias/{x['seq']}/duplicar", json={})
    assert r.status_code == 200, r.text
    copia = auth.get(f"/api/secuencias/{r.json()['id']}").json()
    assert copia["nombre"] == "Copia de Secuencia editor" and copia["segmentoId"] == seg
    assert copia["estadoEditor"] == "borrador"

    # Historial y restauración.
    nuevo = con_cambio(
        borrador(auth, x["seq"])["contenido"], 1, "Hola {{nombre}}, v2.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    vs = auth.get(f"/api/secuencias/{x['seq']}/versiones").json()
    assert [v["version"] for v in vs] == [1, 2]
    assert vs[0]["decision"] == "aprobada" and vs[1]["autor"] == "admin" and "decision" not in vs[1]
    assert (
        auth.post(f"/api/secuencias/{x['seq']}/versiones/2/restaurar", json={}).status_code == 400
    )
    r = auth.post(f"/api/secuencias/{x['seq']}/versiones/1/restaurar", json={})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert borrador(auth, x["seq"])["contenido"]["pasos"][0]["cuerpo"] == CUERPO

    # Archivar: no si está activa; oculta de la lista y bloquea aprobar y guardar.
    activa = crear_campana(activa=True)
    assert auth.post(f"/api/secuencias/{activa['seq']}/archivar", json={}).status_code == 409
    assert auth.post(f"/api/secuencias/{nueva}/archivar", json={}).status_code == 200
    ids = [s["id"] for s in auth.get("/api/secuencias").json()]
    assert nueva not in ids and x["seq"] in ids
    todas = auth.get("/api/secuencias?archivadas=true").json()
    assert next(s for s in todas if s["id"] == nueva)["estadoEditor"] == "archivada"
    rev = auth.get(f"/api/secuencias/{nueva}/revision").json()
    assert "La secuencia está archivada" in rev["bloqueos"]
    r = auth.put(f"/api/secuencias/{nueva}/borrador", json={"secuencia": b["contenido"]})
    assert r.status_code == 409


def test_otra_version_ia_rechaza_el_borrador_y_encola(auth, monkeypatch, sin_gmail):
    proveedor_listo(monkeypatch)
    x = crear_campana()
    nuevo = con_cambio(
        borrador(auth, x["seq"])["contenido"], 1, "Hola {{nombre}}, v2.\n{{firma}}\n{{baja}}"
    )
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    r = auth.post(f"/api/secuencias/{x['seq']}/otra-version-ia", json={"comentario": "ab"})
    assert r.status_code == 400
    try:
        r = auth.post(
            f"/api/secuencias/{x['seq']}/otra-version-ia", json={"comentario": "Más directo"}
        )
        assert r.status_code == 200, r.text
        vs = auth.get(f"/api/secuencias/{x['seq']}/versiones").json()
        assert vs[-1]["decision"] == "rechazada" and vs[-1]["comentario"] == "Más directo"
        with transaccion("admin") as c:
            t = uno(
                c,
                "SELECT datos FROM tareas WHERE tipo='revisar_campana' ORDER BY creada_en DESC LIMIT 1",
            )
        assert t["datos"]["secuenciaId"] == x["seq"] and t["datos"]["comentario"] == "Más directo"
        r = auth.post(f"/api/secuencias/{x['seq']}/otra-version-ia", json={"comentario": "Otra"})
        assert r.status_code == 409
    finally:
        with transaccion("admin") as c:
            c.execute(
                "UPDATE tareas SET estado='cancelada',terminada_en=now() "
                "WHERE estado IN ('pendiente','en_proceso') AND tipo='revisar_campana'"
            )


# 9 ------------------------------------------------------------------------------------
def test_migracion_0005_duplica_compartidas_y_baja(auth):
    env = {**os.environ}
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "0004"], check=True, env=env, capture_output=True)
    try:
        with transaccion("admin") as c:
            cols = {
                r["column_name"]
                for r in todos(
                    c,
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='nexus' AND table_name='pasos'",
                )
            }
            assert "formato" not in cols
            seg = insertar(c, "segmentos", {"nombre": "Migración", "propuesto_por": "admin"})
            seq = insertar(
                c, "secuencias", {"nombre": "Compartida", "objetivo": "x", "llamada_accion": "y"}
            )
            insertar(
                c,
                "pasos",
                {"secuencia_id": seq["id"], "orden": 1, "asunto": "Hola", "cuerpo": CUERPO},
            )
            a = insertar(
                c, "campanas", {"nombre": "A", "segmento_id": seg["id"], "secuencia_id": seq["id"]}
            )
            b = insertar(
                c, "campanas", {"nombre": "B", "segmento_id": seg["id"], "secuencia_id": seq["id"]}
            )
        subprocess.run([*alembic, "upgrade", "head"], check=True, env=env, capture_output=True)
        with transaccion("admin") as c:
            ca = uno(c, "SELECT secuencia_id FROM campanas WHERE id=%s", (a["id"],))["secuencia_id"]
            cb = uno(c, "SELECT secuencia_id FROM campanas WHERE id=%s", (b["id"],))["secuencia_id"]
            # Una conserva la original (la más antigua; aquí empatan) y la otra recibe la copia.
            assert ca != cb and seq["id"] in (ca, cb)
            copia_id = cb if ca == seq["id"] else ca
            copia = todos(c, "SELECT cuerpo,formato FROM pasos WHERE secuencia_id=%s", (copia_id,))
            assert copia == [{"cuerpo": CUERPO, "formato": "texto"}]
            for cam in (a, b):
                p = uno(
                    c,
                    "SELECT p.id,v.version,v.autor,v.contenido FROM propuestas p JOIN propuesta_versiones v "
                    "ON v.propuesta_id=p.id WHERE p.tipo='campana' AND p.objetivo_campana_id=%s",
                    (cam["id"],),
                )
                assert p["version"] == 1 and p["autor"] == "admin"
                assert p["contenido"]["pasos"][0]["cuerpo"] == CUERPO
            activos = todos(
                c, "SELECT version FROM prompts WHERE agente='estratega_redactor' AND activo"
            )
            assert [r["version"] for r in activos] == [3]
            # Las restricciones nuevas existen y se cumplen.
            with pytest.raises(Exception):
                with c.transaction():
                    c.execute("UPDATE pasos SET formato='html' WHERE secuencia_id=%s", (seq["id"],))
        subprocess.run([*alembic, "downgrade", "0004"], check=True, env=env, capture_output=True)
        with transaccion("admin") as c:
            assert [
                r["version"]
                for r in todos(
                    c, "SELECT version FROM prompts WHERE agente='estratega_redactor' AND activo"
                )
            ] == [2]
    finally:
        subprocess.run([*alembic, "upgrade", "head"], check=True, env=env, capture_output=True)


def test_mejora_crea_borrador_y_segmento_aprueba_su_propuesta(auth, sin_gmail):
    x = crear_campana()
    with transaccion("admin") as c:
        mejora = nueva_propuesta(
            c,
            "analista",
            "mejora",
            "Nombre más claro",
            {"objetivo": "secuencia", "id": x["seq"], "cambios": {"nombre": "Secuencia mejorada"}},
        )
        seg = insertar(
            c, "segmentos", {"nombre": f"Propuesto {uuid4().hex[:6]}", "propuesto_por": "agente"}
        )
        p_seg = nueva_propuesta(
            c,
            "segmentador",
            "segmento",
            "Segmento sugerido",
            {},
            {"objetivo_segmento_id": seg["id"]},
        )
    assert auth.post(f"/api/propuestas/{mejora['id']}/aprobar", json={}).status_code == 200
    assert borrador(auth, x["seq"])["contenido"]["nombre"] == "Secuencia mejorada"
    s = auth.get(f"/api/secuencias/{x['seq']}").json()
    assert s["nombre"] == "Secuencia editor" and s["estadoEditor"] == "cambios_sin_aprobar"
    assert auth.post(f"/api/segmentos/{seg['id']}/aprobar", json={}).status_code == 200
    p = auth.get(f"/api/propuestas/{p_seg['id']}").json()
    assert p["estado"] == "aprobada" and p["versiones"][-1]["decision"] == "aprobada"


# 6 y 7 --------------------------------------------------------------------------------
def test_solicitudes_a_la_ia_desde_secuencias(auth, agente, sin_gmail):
    sin_aprobar = segmento_con_contactos(elegibles=True, aprobado=False)
    sin_elegibles = segmento_con_contactos(elegibles=False)
    listo = segmento_con_contactos(elegibles=True)
    base = {"segmentoId": listo, "objetivo": "Que completen el diagnóstico RSE Express"}
    try:
        falso = agente(ruta(3))
        r = auth.post("/api/secuencias/ia", json={**base, "numeroCorreos": 6})
        assert r.status_code == 400 and "entre 2 y 5" in r.json()["message"]
        r = auth.post("/api/secuencias/ia", json={**base, "segmentoId": sin_aprobar})
        assert r.status_code == 409 and r.json()["message"] == "El segmento requiere aprobación"
        r = auth.post("/api/secuencias/ia", json={**base, "segmentoId": sin_elegibles})
        assert r.status_code == 409 and "contactos elegibles" in r.json()["message"]
        r = auth.post("/api/secuencias/ia", json={**base, "notas": "Mencionar ingenios"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        r = auth.post("/api/secuencias/ia", json=base)
        assert r.status_code == 409
        assert r.json()["message"] == "Ya hay una secuencia en preparación para este segmento"
        tarea = ultima_tarea()
        sol = next(x for x in auth.get("/api/secuencias/solicitudes").json() if x["id"] == tarea)
        assert sol["estado"] == "en_cola" and sol["segmentoId"] == listo
        assert "secuenciaId" not in sol and "propuestaId" not in sol
        ejecutar_tarea(tarea)
        sol = next(x for x in auth.get("/api/secuencias/solicitudes").json() if x["id"] == tarea)
        assert sol["estado"] == "lista" and "terminadaEn" in sol
        s = auth.get(f"/api/secuencias/{sol['secuenciaId']}").json()
        assert s["estadoEditor"] == "borrador" and s["campanaEstado"] == "en_revision"
        b = borrador(auth, sol["secuenciaId"])
        assert b["autor"] == "agente" and b["contenido"]["notasAgente"]["general"]
        assert {p["formato"] for p in b["contenido"]["pasos"]} == {"texto"}
        # Sin datos personales en la entrada del agente.
        texto = " ".join(falso.entradas)
        assert "Ficticio" not in texto and "@" not in texto.split("<datos_no_confiables>")[-1]

        # Fallo sin reintentos y reintento manual.
        otro = segmento_con_contactos(elegibles=True)
        agente(ruta(2))
        auth.post("/api/secuencias/ia", json={**base, "segmentoId": otro})
        fallida = ultima_tarea()
        ejecutar_tarea(fallida)
        sol = next(x for x in auth.get("/api/secuencias/solicitudes").json() if x["id"] == fallida)
        assert sol["estado"] == "fallida"
        assert sol["error"].startswith("La secuencia propuesta no cumple:")
        with transaccion("admin") as c:
            t = uno(c, "SELECT intentos,max_intentos FROM tareas WHERE id=%s", (fallida,))
        assert t["intentos"] == t["max_intentos"] == 1
        agente(ruta(3))
        r = auth.post(f"/api/secuencias/solicitudes/{fallida}/reintentar", json={})
        assert r.status_code == 200 and r.json() == {"ok": True}
        nueva = ultima_tarea()
        assert nueva != fallida
        ejecutar_tarea(nueva)
        sol = next(x for x in auth.get("/api/secuencias/solicitudes").json() if x["id"] == nueva)
        assert sol["estado"] == "lista" and sol["secuenciaId"]
        r = auth.post(f"/api/secuencias/solicitudes/{nueva}/reintentar", json={})
        assert r.status_code == 409

        # Otra versión de la IA: la solicitud aparece con su secuencia y crea un borrador.
        seq = sol["secuenciaId"]
        agente(ruta(3))
        auth.post(f"/api/secuencias/{seq}/otra-version-ia", json={"comentario": "Más directo"})
        revisar = ultima_tarea("revisar_campana")
        ejecutar_tarea(revisar)
        sol = next(x for x in auth.get("/api/secuencias/solicitudes").json() if x["id"] == revisar)
        assert sol["tipo"] == "revisar_campana" and sol["estado"] == "lista"
        assert sol["secuenciaId"] == seq
        vs = auth.get(f"/api/secuencias/{seq}/versiones").json()
        assert vs[0]["decision"] == "rechazada" and vs[-1]["autor"] == "agente"
    finally:
        cancelar_todo()


# 8 ------------------------------------------------------------------------------------
HTML_SUCIO = (
    '<p onclick="x()">Hola {{nombre}}, <strong>le invitamos</strong> al '
    '<a href="{{enlace}}">diagnóstico</a>.</p><script>alert(1)</script>'
    "<img src=x onerror=alert(1)>"
    '<p><a href="javascript:alert(1)">malo</a> y <a href="http://inseguro.example">inseguro</a></p>'
    "<ul><li>Uno</li><li>Dos</li></ul><p>{{firma}}<br>{{baja}}</p>"
)


class GmailQueGuarda(GmailFalso):
    def __init__(self) -> None:
        super().__init__()
        self.mensajes: list[Any] = []

    def enviar(self, mensaje: Any, hilo: str | None) -> dict[str, str]:
        self.mensajes.append(mensaje)
        r = {"id": "html-" + uuid4().hex, "threadId": hilo or "hilo-" + uuid4().hex}
        self.enviados[str(mensaje["X-Nexus-Idempotencia"])] = r
        return r


def test_formato_html_limpio_texto_derivado_y_multipart(auth, sin_gmail, monkeypatch):
    from datetime import datetime, timedelta

    import psycopg

    import app.api.datos as datos_mod
    from app.motor.envios import RelojFalso, enviar, programar

    # Un martes dentro de la ventana (mar-jue 08:00-10:30), después de todo lo anterior.
    reloj = RelojFalso(datetime.fromisoformat("2026-12-01T08:15:00-05:00"))
    monkeypatch.setattr(datos_mod, "ahora", lambda: reloj.ahora())
    x = crear_campana(activa=True, contactos=1)
    cam = x["cam"]["id"]
    b = borrador(auth, x["seq"])
    nuevo = con_cambio(b["contenido"], 1, "texto que el servidor reemplaza")
    nuevo["pasos"][0].update(formato="html", cuerpoHtml=HTML_SUCIO)
    r = auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})
    assert r.status_code == 200, r.text

    # El servidor limpia el HTML con la lista blanca y deriva el texto simple.
    p1 = borrador(auth, x["seq"])["contenido"]["pasos"][0]
    assert p1["formato"] == "html"
    for prohibido in ("<script", "alert", "onerror", "onclick", "<img", "javascript:", "http://"):
        assert prohibido not in p1["cuerpoHtml"]
    assert '<a href="{{enlace}}">diagnóstico</a>' in p1["cuerpoHtml"]
    assert p1["cuerpo"] == (
        "Hola {{nombre}}, le invitamos al diagnóstico ({{enlace}}).\n\n"
        "malo y inseguro\n\n- Uno\n- Dos\n\n{{firma}}\n{{baja}}"
    )
    assert borrador(auth, x["seq"])["contenido"]["pasos"][1]["formato"] == "texto"

    # Un HTML sin {{baja}} se guarda como borrador pero no se puede aprobar.
    sin_baja = con_cambio(b["contenido"], 1, "x")
    sin_baja["pasos"][0].update(formato="html", cuerpoHtml="<p>Hola {{nombre}}</p><p>{{firma}}</p>")
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": sin_baja})
    rev = auth.get(f"/api/secuencias/{x['seq']}/revision").json()
    assert rev["puedeAprobar"] is False
    assert any("{{baja}}" in e for e in rev["errores"])
    auth.put(f"/api/secuencias/{x['seq']}/borrador", json={"secuencia": nuevo})

    # Las restricciones de la base siguen vigentes (no se debilitó ninguna).
    with transaccion("admin") as c:
        paso = uno(c, "SELECT id FROM pasos WHERE secuencia_id=%s AND orden=1", (x["seq"],))
    for cambio in (
        "formato='html', cuerpo_html=NULL",
        "formato='html', cuerpo_html='<p>sin baja {{firma}}</p>'",
        "formato='texto', cuerpo_html='<p>{{firma}} {{baja}}</p>'",
        "formato='rtf'",
    ):
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaccion("admin") as c:
                admin_id(c)
                c.execute(f"UPDATE pasos SET {cambio} WHERE id=%s", (paso["id"],))

    # Aprobar y activar: el motor envía multipart/alternative con el mismo enlace.
    assert auth.post(f"/api/secuencias/{x['seq']}/aprobar", json={}).status_code == 200
    with transaccion("admin") as c:
        c.execute(
            "UPDATE inscripciones SET estado='pausada', proximo_envio=NULL "
            "WHERE estado='activa' AND campana_id<>%s",
            (cam,),
        )
        c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa' AND id<>%s", (cam,))
        c.execute("UPDATE mensajes SET estado='cancelado' WHERE estado='programado'")
        lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        lim.update(separacionSegundos=[1, 1])
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(lim),))
    gmail("conectada")
    r = auth.post(f"/api/campanas/{cam}/reanudar", json={})
    assert r.status_code == 200, r.text
    falso = GmailQueGuarda()
    programar(reloj, falso)
    with transaccion("admin") as c:
        m = uno(
            c,
            "SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id "
            "WHERE i.campana_id=%s AND m.paso_orden=1 AND m.estado='programado'",
            (cam,),
        )
        ref = uno(c, "SELECT referencia FROM enlaces_seguimiento WHERE mensaje_id=%s", (m["id"],))
    assert m["cuerpo_html"] and "{{" not in m["cuerpo_html"] and "{{" not in m["cuerpo"]
    reloj.instante += timedelta(hours=1)
    enviar(str(m["id"]), reloj, falso)
    assert len(falso.mensajes) == 1
    correo = falso.mensajes[0]
    assert correo.get_content_type() == "multipart/alternative"
    texto = correo.get_body(preferencelist=("plain",)).get_content()
    html = correo.get_body(preferencelist=("html",)).get_content()
    enlace = config().app_url + "/r/" + ref["referencia"]
    assert enlace in texto and f'<a href="{enlace}">diagnóstico</a>' in html
    assert html.startswith("<!DOCTYPE html>") and "<script" not in html
    assert "<strong>le invitamos</strong>" in html and "<li>Uno</li>" in html
    assert "/baja/" in texto and "/baja/" in html
    gmail("desconectada")


# 9 ------------------------------------------------------------------------------------
MEJORADO = {
    "asunto": "Un diagnóstico corto para {{empresa}}",
    "cuerpo": "Hola {{nombre}}, se lo resumo: el diagnóstico es gratuito y toma 15 minutos. "
    "{{enlace}}\n{{firma}}\n{{baja}}",
    "porQue": "Lo acorté y dejé el enlace al principio.",
}


def descartar(auth, seq: str, tarea: str) -> Any:
    # Como toda mutación de la API, DELETE va con Content-Type: application/json.
    return auth.request("DELETE", f"/api/secuencias/{seq}/sugerencias/{tarea}", json={})


def test_mejorar_un_correo_con_ia(auth, agente, sin_gmail):
    x = crear_campana()
    seq = x["seq"]
    base = f"/api/secuencias/{seq}/correos"
    try:
        falso = agente(MEJORADO)
        r = auth.post(f"{base}/1/mejorar", json={"instruccion": "x"})
        assert r.status_code == 400 and "entre 3 y 300" in r.json()["message"]
        r = auth.post(f"{base}/9/mejorar", json={"instruccion": "Más corto"})
        assert r.status_code == 404
        # Con los cambios sin guardar: la IA parte de lo que hay en pantalla.
        b = borrador(auth, seq)
        en_pantalla = con_cambio(
            b["contenido"],
            1,
            "Hola {{nombre}}, texto en pantalla sin guardar.\n{{firma}}\n{{baja}}",
        )
        r = auth.post(
            f"{base}/1/mejorar", json={"instruccion": "Más corto", "secuencia": en_pantalla}
        )
        assert r.status_code == 200 and r.json() == {"ok": True}
        r = auth.post(f"{base}/1/mejorar", json={"instruccion": "Más corto"})
        assert r.status_code == 409 and r.json()["message"] == "La IA ya está mejorando este correo"
        tarea = ultima_tarea("reescribir_correo")
        [sug] = auth.get(f"/api/secuencias/{seq}/sugerencias").json()
        assert sug["estado"] == "en_cola" and sug["orden"] == 1
        assert sug["original"]["cuerpo"].startswith("Hola {{nombre}}, texto en pantalla")
        assert "cuerpo" not in sug and "porQue" not in sug

        ejecutar_tarea(tarea)
        [sug] = auth.get(f"/api/secuencias/{seq}/sugerencias").json()
        assert sug["estado"] == "lista"
        assert {k: sug[k] for k in ("asunto", "cuerpo", "porQue")} == MEJORADO
        # Sin datos personales para la IA y sin tocar el borrador ni lo aprobado.
        texto = " ".join(falso.entradas)
        assert "texto en pantalla sin guardar" in texto and "Más corto" in texto
        assert "Ficticio" not in texto and "@" not in texto.split("<datos_no_confiables>")[-1]
        assert borrador(auth, seq)["version"] == b["version"]
        assert auth.get(f"/api/secuencias/{seq}").json()["pasos"][0]["cuerpo"] == CUERPO

        # Un correo siguiente del mismo hilo no lleva asunto en la sugerencia.
        agente({**MEJORADO, "asunto": "No debe usarse"})
        auth.post(f"{base}/2/mejorar", json={"instruccion": "Más cercano"})
        ejecutar_tarea(ultima_tarea("reescribir_correo"))
        sug2 = next(
            s for s in auth.get(f"/api/secuencias/{seq}/sugerencias").json() if s["orden"] == 2
        )
        assert sug2["estado"] == "lista" and "asunto" not in sug2

        # Descartar la oculta; una que no existe da 404.
        r = descartar(auth, seq, tarea)
        assert r.status_code == 200 and r.json() == {"ok": True}
        ids = [s["id"] for s in auth.get(f"/api/secuencias/{seq}/sugerencias").json()]
        assert tarea not in ids
        r = descartar(auth, seq, str(uuid4()))
        assert r.status_code == 404

        # Salida que no cumple: dos correcciones y falla sin reintentos; se puede reintentar.
        malo = {**MEJORADO, "cuerpo": "Hola {{nombre}}, sin la línea de baja. {{firma}} {{xyz}}"}
        falso = agente(malo)
        auth.post(f"{base}/1/mejorar", json={"instruccion": "Más formal"})
        fallida = ultima_tarea("reescribir_correo")
        ejecutar_tarea(fallida)
        assert len(falso.entradas) == 3
        sug = next(
            s for s in auth.get(f"/api/secuencias/{seq}/sugerencias").json() if s["id"] == fallida
        )
        assert sug["estado"] == "fallida"
        assert sug["error"].startswith("La sugerencia no cumple: Falta {{baja}}")
        assert "Variable desconocida: {{xyz}}" in sug["error"]
        agente(MEJORADO)
        r = auth.post(f"/api/secuencias/solicitudes/{fallida}/reintentar", json={})
        assert r.status_code == 200
        nueva = ultima_tarea("reescribir_correo")
        ejecutar_tarea(nueva)
        sug = next(
            s for s in auth.get(f"/api/secuencias/{seq}/sugerencias").json() if s["id"] == nueva
        )
        assert sug["estado"] == "lista" and sug["instruccion"] == "Más formal"

        # Descartar una pendiente la cancela.
        auth.post(f"{base}/2/mejorar", json={"instruccion": "Más breve"})
        pendiente = ultima_tarea("reescribir_correo")
        descartar(auth, seq, pendiente)
        with transaccion("admin") as c:
            assert uno(c, "SELECT estado FROM tareas WHERE id=%s", (pendiente,))["estado"] == (
                "cancelada"
            )
    finally:
        cancelar_todo()
        with transaccion("admin") as c:
            c.execute(
                "UPDATE tareas SET estado='cancelada' WHERE tipo='reescribir_correo' "
                "AND estado IN ('pendiente','en_proceso')"
            )
