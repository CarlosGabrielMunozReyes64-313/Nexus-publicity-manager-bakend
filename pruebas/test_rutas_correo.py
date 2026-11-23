# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)
"""Rutas de correo: solicitud al agente, revisión, edición, versiones y aprobación."""

import json
import os
import subprocess
import sys
from typing import Any
from uuid import uuid4

import pytest
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401
from test_propuestas_agentes import proveedor_listo, segmento_con_contactos

from app.agentes import ejecutor
from app.agentes.errores import FalloProveedor
from app.db import insertar, transaccion, uno

FIRMA = "{{firma}}\n{{baja}}"
CUERPOS = [
    "Hola {{nombre}}, en {{empresa}} quizá ya les piden información de sostenibilidad. "
    "Le ofrecemos el diagnóstico gratuito RSE Express: {{enlace}}\n" + FIRMA,
    "Hola {{nombre}}, los cuestionarios de proveedores suelen preguntar por residuos y "
    "condiciones laborales. Puede empezar aquí: {{enlace}}\n" + FIRMA,
    "Hola {{nombre}}, este es mi último mensaje. Si le sirve, respóndame y conversamos "
    "20 minutos.\n" + FIRMA,
    "Hola {{nombre}}, un recordatorio breve sobre el diagnóstico gratuito: {{enlace}}\n" + FIRMA,
    "Hola {{nombre}}, quedamos atentos si desea conversar sobre sostenibilidad.\n" + FIRMA,
]


def ruta(n: int = 3, asunto: str = "Diagnóstico gratuito para su empresa", **cambios: Any):
    """Salida válida del estratega con n correos (la del prompt v2)."""
    pasos = [
        {
            "orden": i,
            "asunto": asunto if i == 1 else None,
            "cuerpo": CUERPOS[i - 1],
            "esperaDiasHabiles": [0, 4, 5, 5, 5][i - 1],
            "mismoHilo": i > 1,
            "variables": [
                {"nombre": "nombre", "campoFuente": "nombre", "respaldo": "equipo"},
                {"nombre": "empresa", "campoFuente": "razonSocial", "respaldo": "su empresa"},
            ],
        }
        for i in range(1, n + 1)
    ]
    salida = {
        "nombre": "Diagnóstico RSE Express para proveedores",
        "resumen": "Tres correos en el mismo hilo para invitar al diagnóstico.",
        "notas": {
            "general": "El segmento vende a empresas grandes que piden información ESG.",
            "pasos": [
                {"orden": i, "proposito": f"Propósito {i}", "porQue": f"Motivo {i}"}
                for i in range(1, n + 1)
            ],
        },
        "secuencia": {
            "nombre": "Ruta RSE Express",
            "objetivo": "Que completen el diagnóstico RSE Express",
            "llamadaAccion": "Completar el diagnóstico",
            "metricasExito": ["diagnósticos completados"],
            "ventanaEnvio": {"dias": [2, 3, 4], "desde": "08:00", "hasta": "10:30"},
            "pasos": pasos,
        },
    }
    salida["secuencia"].update(cambios)
    return salida


class AgenteFalso:
    """Devuelve salidas en orden (o lanza la excepción) y guarda lo que recibió."""

    def __init__(self, *salidas: Any) -> None:
        self.salidas = list(salidas)
        self.entradas: list[str] = []

    def ejecutar(self, modelo, prompt, entrada, schema, max_tokens):
        self.entradas.append(entrada)
        salida = self.salidas.pop(0) if len(self.salidas) > 1 else self.salidas[0]
        if isinstance(salida, Exception):
            raise salida
        return schema.model_validate(salida), {"input_tokens": 100, "output_tokens": 100}


@pytest.fixture
def agente(monkeypatch):
    """Instala un agente falso en el proveedor real (lo usa el trabajador)."""
    proveedor_listo(monkeypatch)
    actual: dict[str, AgenteFalso] = {}

    def usar(*salidas: Any) -> AgenteFalso:
        actual["a"] = AgenteFalso(*salidas)
        monkeypatch.setattr(ejecutor, "ClaudeReal", lambda: actual["a"])
        return actual["a"]

    return usar


def ejecutar_tarea(tarea_id: str) -> None:
    """Toma la tarea como lo haría el trabajador y la procesa."""
    from app.tareas.trabajador import procesar

    with transaccion("admin") as c:
        t = uno(
            c,
            "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
            "intentos=intentos+1 WHERE id=%s RETURNING *",
            (tarea_id,),
        )
    procesar(t)


def ultima_tarea(tipo: str = "proponer_campana") -> str:
    with transaccion("admin") as c:
        return str(
            uno(
                c,
                "SELECT id FROM tareas WHERE tipo=%s ORDER BY creada_en DESC LIMIT 1",
                (tipo,),
            )["id"]
        )


def cancelar_todo() -> None:
    with transaccion("admin") as c:
        c.execute(
            "UPDATE tareas SET estado='cancelada',terminada_en=now() WHERE estado IN "
            "('pendiente','en_proceso') AND tipo IN ('proponer_campana','revisar_campana')"
        )


def solicitud(auth, tarea_id: str) -> dict[str, Any]:
    return next(s for s in auth.get("/api/propuestas/solicitudes").json() if s["id"] == tarea_id)


def crear_ruta(auth, agente, segmento: str | None = None, n: int = 3) -> dict[str, Any]:
    """Pide una ruta y la deja creada por el agente falso. Devuelve la propuesta."""
    segmento = segmento or segmento_con_contactos(elegibles=True)
    agente(ruta(n))
    r = auth.post(
        "/api/propuestas/rutas-correo",
        json={
            "segmentoId": segmento,
            "objetivo": "Que completen el diagnóstico RSE Express",
            "numeroCorreos": n,
        },
    )
    assert r.status_code == 200 and r.json() == {"ok": True}, r.text
    tarea = ultima_tarea()
    ejecutar_tarea(tarea)
    s = solicitud(auth, tarea)
    assert s["estado"] == "lista", s
    return auth.get(f"/api/propuestas/{s['propuestaId']}").json()


def editar(contenido: dict[str, Any], **por_paso: str) -> dict[str, Any]:
    nuevo = json.loads(json.dumps(contenido))
    for i, cuerpo in por_paso.items():
        nuevo["pasos"][int(i[1:]) - 1]["cuerpo"] = cuerpo
    return nuevo


def cuantos_mensajes() -> int:
    with transaccion("admin") as c:
        return int(uno(c, "SELECT count(*) AS n FROM mensajes")["n"])


# 1 ------------------------------------------------------------------------------------
def test_pedir_ruta_valida_y_encola(auth, monkeypatch):
    proveedor_listo(monkeypatch)
    sin_aprobar = segmento_con_contactos(elegibles=True, aprobado=False)
    sin_elegibles = segmento_con_contactos(elegibles=False)
    listo = segmento_con_contactos(elegibles=True)
    base = {"segmentoId": listo, "objetivo": "Agendar una llamada de 20 minutos"}
    try:
        for cuerpo, texto in (
            ({**base, "objetivo": "abc"}, "entre 5 y 200"),
            ({**base, "numeroCorreos": 6}, "entre 2 y 5"),
            ({**base, "numeroCorreos": 1}, "entre 2 y 5"),
            ({**base, "notas": "x" * 501}, "500"),
        ):
            r = auth.post("/api/propuestas/rutas-correo", json=cuerpo)
            assert r.status_code == 400 and texto in r.json()["message"], r.text
        r = auth.post("/api/propuestas/rutas-correo", json={**base, "segmentoId": sin_aprobar})
        assert r.status_code == 409 and r.json()["message"] == "El segmento requiere aprobación"
        r = auth.post("/api/propuestas/rutas-correo", json={**base, "segmentoId": sin_elegibles})
        assert r.status_code == 409
        assert (
            r.json()["message"]
            == "El segmento no tiene contactos elegibles con base legal aprobada"
        )

        r = auth.post("/api/propuestas/rutas-correo", json={**base, "notas": "Mencionar Palmira"})
        assert r.status_code == 200 and r.json() == {"ok": True}
        r = auth.post("/api/propuestas/rutas-correo", json=base)
        assert r.status_code == 409
        assert r.json()["message"] == "Ya hay una secuencia en preparación para este segmento"

        s = solicitud(auth, ultima_tarea())
        assert s["estado"] == "en_cola" and s["tipo"] == "proponer_campana"
        assert s["segmentoId"] == listo and s["objetivo"] == base["objetivo"]
        assert "propuestaId" not in s and "error" not in s and "terminadaEn" not in s
        with transaccion("admin") as c:
            datos = uno(c, "SELECT datos FROM tareas WHERE id=%s", (s["id"],))["datos"]
        assert datos["numeroCorreos"] == 3 and datos["notas"] == "Mencionar Palmira"

        segs = auth.get("/api/propuestas/rutas-correo/segmentos").json()
        por_id = {x["id"]: x for x in segs}
        assert por_id[listo]["elegibles"] == 1 and por_id[sin_elegibles]["elegibles"] == 0
        assert sin_aprobar not in por_id
    finally:
        cancelar_todo()


# 2 ------------------------------------------------------------------------------------
def test_el_agente_crea_la_ruta_con_notas(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        assert p["tipo"] == "campana" and p["estado"] == "pendiente" and p["versionActual"] == 1
        v = p["versiones"][0]
        assert v["autor"] == "agente" and "decision" not in v
        assert v["contenido"]["notasAgente"]["general"].startswith("El segmento")
        assert [x["orden"] for x in v["contenido"]["notasAgente"]["pasos"]] == [1, 2, 3]
        assert len(v["contenido"]["pasos"]) == 3
        assert p["ruta"]["campanaEstado"] == "en_revision"
        assert p["ruta"]["segmentoNombre"].startswith("Segmento ")
        cam = auth.get(f"/api/campanas/{p['objetivoCampanaId']}").json()
        assert cam["estado"] == "en_revision"
        assert len(auth.get(f"/api/secuencias/{cam['secuenciaId']}").json()["pasos"]) == 3
    finally:
        cancelar_todo()


# 3 ------------------------------------------------------------------------------------
def test_salida_invalida_dos_correcciones_y_falla_sin_reintentos(auth, agente):
    segmento = segmento_con_contactos(elegibles=True)
    try:
        mala = ruta(2)  # se piden 3 correos
        falso = agente(mala)
        auth.post(
            "/api/propuestas/rutas-correo",
            json={"segmentoId": segmento, "objetivo": "Que completen el diagnóstico"},
        )
        tarea = ultima_tarea()
        ejecutar_tarea(tarea)
        assert len(falso.entradas) == 3  # la original y dos correcciones
        assert "corregir" in falso.entradas[1] and "Se pidieron 3 correos" in falso.entradas[2]
        s = solicitud(auth, tarea)
        assert s["estado"] == "fallida" and "terminadaEn" in s
        assert s["error"] == "La secuencia propuesta no cumple: Se pidieron 3 correos y la secuencia tiene 2"
        with transaccion("admin") as c:
            t = uno(c, "SELECT estado,intentos,max_intentos FROM tareas WHERE id=%s", (tarea,))
        assert t["estado"] == "fallida" and t["intentos"] == t["max_intentos"] == 1

        # Una corrección que sí cumple termina bien.
        sin_baja = ruta(3)
        sin_baja["secuencia"]["pasos"][1]["cuerpo"] = "Hola {{nombre}}, texto sin pie. {{firma}}"
        falso = agente(sin_baja, ruta(3))
        assert auth.post(f"/api/propuestas/solicitudes/{tarea}/reintentar", json={}).json() == {
            "ok": True
        }
        nueva = ultima_tarea()
        assert nueva != tarea and solicitud(auth, nueva)["estado"] == "en_cola"
        ejecutar_tarea(nueva)
        assert len(falso.entradas) == 2 and "Paso 2: falta {{baja}}" in falso.entradas[1]
        assert solicitud(auth, nueva)["estado"] == "lista"
        r = auth.post(f"/api/propuestas/solicitudes/{nueva}/reintentar", json={})
        assert r.status_code == 409
    finally:
        cancelar_todo()


def test_limite_temporal_se_reintenta(auth, agente):
    segmento = segmento_con_contactos(elegibles=True)
    try:
        agente(FalloProveedor("Claude está saturado; se reintentará", reintentable=True))
        auth.post(
            "/api/propuestas/rutas-correo",
            json={"segmentoId": segmento, "objetivo": "Que completen el diagnóstico"},
        )
        tarea = ultima_tarea()
        ejecutar_tarea(tarea)
        with transaccion("admin") as c:
            t = uno(c, "SELECT estado,intentos,max_intentos FROM tareas WHERE id=%s", (tarea,))
        assert t["estado"] == "pendiente" and t["intentos"] == 1 and t["max_intentos"] > 1
        assert solicitud(auth, tarea)["estado"] == "en_cola"
    finally:
        cancelar_todo()


def test_segmento_revocado_falla_sin_reintentos(auth, agente):
    segmento = segmento_con_contactos(elegibles=True)
    try:
        falso = agente(ruta(3))
        auth.post(
            "/api/propuestas/rutas-correo",
            json={"segmentoId": segmento, "objetivo": "Que completen el diagnóstico"},
        )
        tarea = ultima_tarea()
        with transaccion("admin") as c:
            c.execute("UPDATE segmentos SET estado='propuesto' WHERE id=%s", (segmento,))
        ejecutar_tarea(tarea)
        s = solicitud(auth, tarea)
        assert s["estado"] == "fallida" and s["error"] == "El segmento requiere aprobación"
        assert falso.entradas == []  # no se gastó nada
    finally:
        cancelar_todo()


# 4 ------------------------------------------------------------------------------------
def test_el_agente_no_recibe_datos_personales(auth, agente):
    segmento = segmento_con_contactos(elegibles=True)
    with transaccion("admin") as c:
        ct = uno(
            c,
            "SELECT c.id,c.nombre,c.correo,c.empresa_id FROM contactos c WHERE c.id::text="
            "(SELECT reglas->0->>'valor' FROM segmentos WHERE id=%s)",
            (segmento,),
        )
        c.execute(
            "UPDATE contactos SET nombre='Rosalba Quintero',telefono='+57 300 555 0101',"
            "cargo='Gerente' WHERE id=%s",
            (ct["id"],),
        )
        insertar(
            c,
            "senales",
            {
                "empresa_id": ct["empresa_id"],
                "tipo": "proveedor_ingenio",
                "descripcion": "Proveedor de Ingenio Ficticio",
                "fuente": "manual",
            },
        )
    try:
        p = crear_ruta(auth, agente, segmento)
        falso = agente(ruta(3))  # para inspeccionar la entrada de una revisión
        auth.post(f"/api/propuestas/{p['id']}/rechazar", json={"comentario": "Más corto"})
        ejecutar_tarea(ultima_tarea("revisar_campana"))
        with transaccion("admin") as c:
            guardadas = [
                r["entrada"]
                for r in c.execute(
                    "SELECT entrada FROM ejecuciones_agente WHERE agente='estratega_redactor' "
                    "ORDER BY iniciada_en DESC LIMIT 2"
                ).fetchall()
            ]
        revision, primera = guardadas  # la más reciente es la revisión
        textos = [json.dumps(x, ensure_ascii=False) for x in guardadas] + falso.entradas
        for texto in textos:
            for dato in ("Rosalba", "Quintero", ct["correo"], "555 0101", str(ct["id"])):
                assert dato not in texto, dato
        assert primera["elegibles"] == 1
        assert primera["resumenElegibles"]["municipios"] == {"Palmira": 1}
        assert primera["resumenElegibles"]["senales"] == [
            {"tipo": "proveedor_ingenio", "contactos": 1}
        ]
        assert primera["datosVerificados"] == {
            "nombre": 0,
            "empresa": 0,
            "senal": 0,
            "cargo": 0,
            "municipio": 0,
        }
        assert primera["segmento"]["reglas"] == [
            {"campo": "contactos_elegidos_a_mano", "cantidad": 1}
        ]
        assert revision["comentario"] == "Más corto"
        assert len(revision["versionRechazada"]["pasos"]) == 3
    finally:
        cancelar_todo()


# 5 ------------------------------------------------------------------------------------
def test_editar_contenido_crea_version_admin_y_conserva_notas(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        contenido = p["versiones"][-1]["contenido"]
        r = auth.put(f"/api/propuestas/{p['id']}", json={})
        assert r.status_code == 400 and r.json()["message"] == "Indique el resumen o el contenido"
        malo = editar(contenido, p2="Texto sin pie de página ni firma, solo para probar.")
        malo["pasos"][0]["cuerpo"] += " Resultado garantizado."
        r = auth.put(f"/api/propuestas/{p['id']}", json={"contenido": malo})
        assert r.status_code == 400
        msg = r.json()["message"]
        assert "Paso 2: falta {{baja}}" in msg and "Palabra prohibida: garantizado" in msg
        assert ". " in msg

        bueno = editar(contenido, p2="Hola {{nombre}}, versión del administrador.\n" + FIRMA)
        r = auth.put(f"/api/propuestas/{p['id']}", json={"contenido": bueno})
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["version"] == 2
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        v2 = p["versiones"][-1]
        assert p["versionActual"] == 2 and v2["autor"] == "admin" and p["estado"] == "pendiente"
        notas = v2["contenido"]["notasAgente"]
        assert notas["general"].startswith("El segmento")
        assert notas["pasos"][1]["editadoPorAdmin"] is True
        assert (
            "editadoPorAdmin" not in notas["pasos"][0] and notas["pasos"][0]["porQue"] == "Motivo 1"
        )

        # Solo resumen: misma ruta, versión nueva.
        r = auth.put(f"/api/propuestas/{p['id']}", json={"resumen": "Resumen ajustado a mano"})
        assert r.status_code == 200
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p["versionActual"] == 3 and p["resumen"] == "Resumen ajustado a mano"
        assert p["versiones"][-1]["contenido"] == v2["contenido"]

        # Con la ruta ya aprobada, editar crea un borrador: lo aprobado sigue vigente.
        assert auth.post(f"/api/propuestas/{p['id']}/aprobar", json={}).status_code == 200
        cam = p["objetivoCampanaId"]
        antes = auth.get(f"/api/campanas/{cam}").json()
        assert antes["estado"] == "aprobada"
        r = auth.put(f"/api/propuestas/{p['id']}", json={"contenido": contenido})
        assert r.status_code == 200
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p["estado"] == "pendiente" and p["versionActual"] == 4
        despues = auth.get(f"/api/campanas/{cam}").json()
        assert despues["estado"] == "aprobada"
        assert despues["huellaAprobada"] == antes["huellaAprobada"]
    finally:
        cancelar_todo()


# 6 ------------------------------------------------------------------------------------
def test_la_version_del_admin_sobrevive_cambios_de_segmento_y_marca(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        contenido = p["versiones"][-1]["contenido"]
        bueno = editar(
            contenido, p1="Hola {{nombre}}, texto del administrador. {{enlace}}\n" + FIRMA
        )
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": bueno})
        cam = auth.get(f"/api/campanas/{p['objetivoCampanaId']}").json()

        marca = auth.get("/api/configuracion/marca").json()
        r = auth.put("/api/configuracion/marca", json={**marca, "tono": marca["tono"] + " "})
        assert r.status_code == 200, r.text
        r = auth.patch(
            f"/api/segmentos/{cam['segmentoId']}", json={"justificacion": "Ajuste " + uuid4().hex}
        )
        assert r.status_code == 200, r.text
        with transaccion("admin") as c:
            from app.api.campanas import revisar_pendientes

            revisar_pendientes(c)
        p2 = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p2["versionActual"] == 2
        assert p2["versiones"][-1]["contenido"]["pasos"][0]["cuerpo"] == bueno["pasos"][0]["cuerpo"]

        # Si después alguien cambia la secuencia viva, esa sí crea una versión nueva.
        seq = auth.get(f"/api/secuencias/{cam['secuenciaId']}").json()
        seq["pasos"][2]["cuerpo"] = "Hola {{nombre}}, cambio hecho en Secuencias.\n" + FIRMA
        r = auth.put(f"/api/secuencias/{cam['secuenciaId']}", json=seq)
        assert r.status_code == 200, r.text
        p3 = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p3["versionActual"] == 3
        assert (
            "cambio hecho en Secuencias" in p3["versiones"][-1]["contenido"]["pasos"][2]["cuerpo"]
        )
        assert p3["versiones"][-1]["contenido"]["notasAgente"]["pasos"][2]["editadoPorAdmin"]
    finally:
        cancelar_todo()


# 7 ------------------------------------------------------------------------------------
def test_vista_previa_usa_el_contenido_de_la_version(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        contenido = p["versiones"][-1]["contenido"]
        cambiado = json.loads(json.dumps(contenido))
        cambiado["pasos"][0]["asunto"] = "Asunto de la versión dos"
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": cambiado})
        contactos = auth.get(f"/api/propuestas/{p['id']}/elegibles").json()
        assert len(contactos) == 1 and set(contactos[0]) == {"id", "nombre", "empresa"}
        antes = cuantos_mensajes()
        actual = auth.post(
            f"/api/propuestas/{p['id']}/previsualizar", json={"contactoId": contactos[0]["id"]}
        ).json()
        v1 = auth.post(
            f"/api/propuestas/{p['id']}/previsualizar",
            json={"contactoId": contactos[0]["id"], "version": 1},
        ).json()
        assert actual["pasos"][0]["asunto"] == "Asunto de la versión dos"
        assert v1["pasos"][0]["asunto"] == "Diagnóstico gratuito para su empresa"
        assert "{{" not in actual["pasos"][1]["cuerpo"]
        assert "nombre" in actual["respaldo"]  # nombre sin evidencia verificada
        assert cuantos_mensajes() == antes
        r = auth.post(
            f"/api/propuestas/{p['id']}/previsualizar",
            json={"contactoId": contactos[0]["id"], "version": 9},
        )
        assert r.status_code == 404
    finally:
        cancelar_todo()


# 8 ------------------------------------------------------------------------------------
def test_revision_y_aprobar_coinciden(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        rev = auth.get(f"/api/propuestas/{p['id']}/revision").json()
        assert rev["puedeAprobar"] is True and rev["bloqueos"] == [] and rev["errores"] == []
        assert rev["elegibles"] == 1 and rev["duracionDiasHabiles"] == 9
        assert rev["ventanaValida"] is True and rev["gmailConectado"] is False
        assert rev["topeDiario"] >= 1 and rev["segmento"]["estado"] == "aprobado"
        assert any("Gmail no está conectado" in a for a in rev["advertencias"])
        assert "{{nombre}}: 1 de 1 contactos usarán el texto de respaldo" in rev["advertencias"]

        # Ventana fuera del horario global: bloqueo en la revisión y en aprobar.
        contenido = p["versiones"][-1]["contenido"]
        tarde = {**contenido, "ventanaEnvio": {"dias": [6], "desde": "20:00", "hasta": "21:00"}}
        largo = editar(tarde, p1=("palabra " * 130) + "{{enlace}} {{enlace}}\n" + FIRMA)
        largo["pasos"][0]["asunto"] = "OFERTA ESPECIAL!! " + "x" * 50
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": largo})
        rev = auth.get(f"/api/propuestas/{p['id']}/revision").json()
        assert rev["puedeAprobar"] is False and rev["ventanaValida"] is False
        assert rev["bloqueos"] == [
            "La ventana de envío de la secuencia no coincide con el horario global "
            "(Configuración → Límites y horarios)"
        ]
        avisos = " | ".join(rev["advertencias"])
        for texto in ("palabras", "caracteres", "mayúsculas", "signos", "más de un {{enlace}}"):
            assert texto in avisos, texto
        r = auth.post(f"/api/propuestas/{p['id']}/aprobar", json={})
        assert r.status_code == 400 and r.json()["message"] == ". ".join(rev["bloqueos"])

        # Segmento sin aprobar: mismo bloqueo en ambos lados.
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": contenido})
        cam = auth.get(f"/api/campanas/{p['objetivoCampanaId']}").json()
        with transaccion("admin") as c:
            c.execute("UPDATE segmentos SET estado='propuesto' WHERE id=%s", (cam["segmentoId"],))
        rev = auth.get(f"/api/propuestas/{p['id']}/revision").json()
        assert rev["bloqueos"] == ["El segmento requiere aprobación"]
        r = auth.post(f"/api/propuestas/{p['id']}/aprobar", json={})
        assert r.status_code == 400 and r.json()["message"] == "El segmento requiere aprobación"
    finally:
        cancelar_todo()


# 9 ------------------------------------------------------------------------------------
def test_aprobar_una_version_editada(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        contenido = p["versiones"][-1]["contenido"]
        bueno = editar(
            contenido, p3="Hola {{nombre}}, cierre escrito por el administrador.\n" + FIRMA
        )
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": bueno})
        antes = cuantos_mensajes()
        r = auth.post(f"/api/propuestas/{p['id']}/aprobar", json={})
        assert r.status_code == 200, r.text
        cam = auth.get(f"/api/campanas/{p['objetivoCampanaId']}").json()
        assert cam["estado"] == "aprobada"
        seq = auth.get(f"/api/secuencias/{cam['secuenciaId']}").json()
        assert [x["cuerpo"] for x in seq["pasos"]] == [x["cuerpo"] for x in bueno["pasos"]]
        with transaccion("admin") as c:
            a = uno(
                c,
                "SELECT contenido_congelado,huella FROM aprobaciones_campana WHERE campana_id=%s",
                (cam["id"],),
            )
        assert a["huella"]
        assert (
            a["contenido_congelado"]["secuencia"]["pasos"][2]["cuerpo"]
            == bueno["pasos"][2]["cuerpo"]
        )
        assert cuantos_mensajes() == antes  # aprobar no envía ni programa
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p["estado"] == "aprobada" and p["versiones"][-1]["decision"] == "aprobada"
        assert "decididaEn" in p["versiones"][-1]
        rev = auth.get(f"/api/propuestas/{p['id']}/revision").json()
        assert rev["puedeAprobar"] is False and "Propuesta ya aprobada" in rev["bloqueos"]
    finally:
        cancelar_todo()


# 10 -----------------------------------------------------------------------------------
def test_rechazar_restaurar_y_reintentar(auth, agente):
    try:
        p = crear_ruta(auth, agente)
        r = auth.post(
            f"/api/propuestas/{p['id']}/rechazar",
            json={"comentario": "Prefiero solo dos correos", "pedirNuevaVersion": False},
        )
        assert r.status_code == 200
        with transaccion("admin") as c:
            n = uno(
                c,
                "SELECT count(*) AS n FROM tareas WHERE tipo='revisar_campana' AND datos->>'propuestaId'=%s",
                (p["id"],),
            )["n"]
        assert n == 0
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        v1 = p["versiones"][0]
        assert v1["decision"] == "rechazada" and v1["comentario"] == "Prefiero solo dos correos"
        r = auth.post(f"/api/propuestas/{p['id']}/rechazar", json={"comentario": "Otra vez"})
        assert r.status_code == 400

        # Restaurar crea una versión nueva del administrador con el contenido elegido.
        bueno = editar(v1["contenido"], p1="Hola {{nombre}}, versión dos. {{enlace}}\n" + FIRMA)
        auth.put(f"/api/propuestas/{p['id']}", json={"contenido": bueno})
        r = auth.post(f"/api/propuestas/{p['id']}/restaurar", json={"version": 2})
        assert r.status_code == 400  # es la actual
        r = auth.post(f"/api/propuestas/{p['id']}/restaurar", json={"version": 1})
        assert r.status_code == 200 and r.json()["ok"] is True
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p["versionActual"] == 3 and p["versiones"][-1]["autor"] == "admin"
        assert p["versiones"][-1]["contenido"] == v1["contenido"]

        # Rechazo con nueva versión: el agente la crea como autor «agente».
        falso = agente(ruta(2))
        auth.post(f"/api/propuestas/{p['id']}/rechazar", json={"comentario": "Solo dos correos"})
        tarea = ultima_tarea("revisar_campana")
        s = solicitud(auth, tarea)
        assert s["tipo"] == "revisar_campana" and s["estado"] == "en_cola"
        assert s["propuestaId"] == p["id"]
        ejecutar_tarea(tarea)
        assert "Solo dos correos" in falso.entradas[0]
        assert "Prefiero solo dos correos" in falso.entradas[0]  # comentarios anteriores
        p = auth.get(f"/api/propuestas/{p['id']}").json()
        assert p["versionActual"] == 4 and p["estado"] == "pendiente"
        assert p["versiones"][-1]["autor"] == "agente"
        assert len(p["versiones"][-1]["contenido"]["pasos"]) == 2
        assert solicitud(auth, tarea)["estado"] == "lista"

        # Reintentar una revisión fallida.
        agente(FalloProveedor("Clave rechazada", reintentable=False))
        auth.post(f"/api/propuestas/{p['id']}/rechazar", json={"comentario": "Más directo"})
        tarea = ultima_tarea("revisar_campana")
        ejecutar_tarea(tarea)
        assert solicitud(auth, tarea)["estado"] == "fallida"
        assert (
            auth.post(f"/api/propuestas/solicitudes/{tarea}/reintentar", json={}).status_code == 200
        )
        assert solicitud(auth, ultima_tarea("revisar_campana"))["estado"] == "en_cola"
    finally:
        cancelar_todo()


# 11 -----------------------------------------------------------------------------------
def test_migracion_0004_un_prompt_activo_y_bajada(auth):
    def prompts() -> list[dict[str, Any]]:
        with transaccion("admin") as c:
            return list(
                c.execute(
                    "SELECT version,activo,texto FROM prompts WHERE agente='estratega_redactor' "
                    "ORDER BY version"
                ).fetchall()
            )

    # En la cabeza (0005) el activo es el v3; la 0004 deja el v2 (se prueba al bajar).
    activos = [p for p in prompts() if p["activo"]]
    assert len(activos) == 1 and activos[0]["version"] == 3
    texto = activos[0]["texto"]
    assert "texto simple" in texto and "reescribirCorreo" in texto
    for clave in (
        "RSE Express",
        "{{firma}}",
        "{{baja}}",
        "martes a jueves",
        "<datos_no_confiables>",
    ):
        assert clave in texto, clave

    env = {**os.environ}
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "0004"], check=True, env=env, capture_output=True)
    try:
        assert [p["version"] for p in prompts() if p["activo"]] == [2]
        subprocess.run([*alembic, "downgrade", "0003"], check=True, env=env, capture_output=True)
        assert [p["version"] for p in prompts() if p["activo"]] == [1]
    finally:
        subprocess.run([*alembic, "upgrade", "head"], check=True, env=env, capture_output=True)
    assert [p["version"] for p in prompts() if p["activo"]] == [3]


# 12 -----------------------------------------------------------------------------------
def test_ruta_anterior_de_segmentos_sigue_funcionando(auth, monkeypatch):
    proveedor_listo(monkeypatch)
    segmento = segmento_con_contactos(elegibles=True)
    try:
        r = auth.post(f"/api/segmentos/{segmento}/proponer-campana", json={})
        assert r.status_code == 200 and r.json()["tareaId"]
        s = solicitud(auth, r.json()["tareaId"])
        assert s["objetivo"] == "Que completen el diagnóstico RSE Express"
        assert s["numeroCorreos"] == 3
        r = auth.post(
            "/api/propuestas/rutas-correo",
            json={"segmentoId": segmento, "objetivo": "Agendar una llamada"},
        )
        assert r.status_code == 409
    finally:
        cancelar_todo()


def test_contrato_de_solicitudes_sin_nulos(auth, monkeypatch):
    proveedor_listo(monkeypatch)
    segmento = segmento_con_contactos(elegibles=True)
    try:
        auth.post(
            "/api/propuestas/rutas-correo",
            json={"segmentoId": segmento, "objetivo": "Que completen el diagnóstico"},
        )
        for s in auth.get("/api/propuestas/solicitudes").json():
            assert None not in s.values()
    finally:
        cancelar_todo()
