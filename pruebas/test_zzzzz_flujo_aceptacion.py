"""Prueba de aceptación de extremo a extremo (sin contactar a nadie real).

Recorrido del encargo:
1. Contacto y empresa en la misma pantalla; importación CSV y XLSX con un correo repetido.
2. Segmento Y/O que cambia al modificar un atributo; lista estática que conserva sus
   miembros; exportación a Excel.
3. Plantilla → secuencia de dos pasos → aprobada para una lista: el trabajador envía el
   primer paso una sola vez y programa el segundo; repetir la aprobación y «reiniciar» el
   trabajador no duplica nada.
4. Baja desde el enlace del primer correo con la base secundaria desconectada: el segundo
   paso se cancela, reimportar no reactiva y una campaña nueva lo excluye.
5. Con la base secundaria disponible aparece un único registro de baja.

Destinatarios: dominios *.example (reservados, RFC 2606). Gmail es GmailFalso.
"""

import base64
import io
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from openpyxl import Workbook, load_workbook
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.types.json import Jsonb
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.config import config
from app.db import todos, transaccion, uno
from app.integraciones.gmail import GmailFalso
from app.motor.envios import RelojFalso, conciliar_pendientes, enviar, programar
from app.publico.rutas import firma_baja
from app.seguridad.auth import cifrar

CONTRATOS: list[dict] = []


def contrato(ruta: str, schema: str, data, array: bool = False):
    """Guarda la respuesta real para validarla con los esquemas Zod del frontend."""
    CONTRATOS.append({"ruta": ruta, "schema": schema, "data": data, "array": array})
    return data


def guardar_contratos() -> None:
    import json
    from pathlib import Path

    out = Path("pruebas/salidas")
    out.mkdir(parents=True, exist_ok=True)
    (out / "contratos_envios.json").write_text(json.dumps(CONTRATOS, ensure_ascii=False))


INICIO = datetime.fromisoformat("2026-12-01T08:30:00-05:00")  # martes, dentro de la ventana


def b64(contenido: bytes) -> str:
    return base64.b64encode(contenido).decode()


def xlsx(filas: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    for f in filas:
        ws.append(f)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def importar(auth, nombre: str, contenido: bytes, opciones: dict) -> dict:  # noqa: F811
    a = auth.post(
        "/api/contactos/importacion/analizar",
        json={"nombreArchivo": nombre, "contenido": b64(contenido)},
    )
    assert a.status_code == 200, a.text
    a = contrato("analizar", "analisisImportacion", a.json())
    mapeo = a["mapeoSugerido"]
    filas = [
        {"fila": f["fila"], "datos": {campo: f["valores"][i] for campo, i in mapeo.items()}}
        for f in a["filas"]
    ]
    pre = auth.post(
        "/api/contactos/importacion/previsualizar", json={"filas": filas, "opciones": opciones}
    )
    assert pre.status_code == 200, pre.text
    pre = contrato("previsualizar", "previaImportacion", pre.json())
    enviar_filas = [
        f
        for f, p in zip(filas, pre["filas"], strict=True)
        if p["accion"] in ("crear", "actualizar")
    ]
    res = {"creados": 0, "actualizados": 0, "omitidos": 0, "fallidos": 0, "filas": []}
    for i in range(0, len(enviar_filas), 500):
        r = auth.post(
            "/api/contactos/importacion/confirmar",
            json={"filas": enviar_filas[i : i + 500], "opciones": opciones},
        )
        assert r.status_code == 200, r.text
        contrato("confirmar", "resultadoImportacion", r.json())
        for k in ("creados", "actualizados", "omitidos", "fallidos"):
            res[k] += r.json()[k]
        res["filas"] += r.json()["filas"]
    return {"analisis": a, "previa": pre, **res}


def hacer_elegibles(ids: list[str]) -> None:
    """Simula al verificador (correo válido) y la aprobación de la base legal."""
    with transaccion("admin") as c:
        admin = uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))
        for cid in ids:
            c.execute("UPDATE contactos SET estado_correo='valido' WHERE id=%s", (cid,))
            c.execute(
                "UPDATE bases_legales SET tipo='autorizacion_registrada',texto='Ficticia',"
                "prueba='Prueba automática',aprobada_por=%s,aprobada_en=now() "
                "WHERE contacto_id=%s AND vigente",
                (admin["id"], cid),
            )


@pytest.fixture
def entorno(auth, monkeypatch):  # noqa: F811
    reloj = RelojFalso(INICIO)
    import app.api.datos as datos_mod

    monkeypatch.setattr(datos_mod, "ahora", lambda: reloj.ahora())
    with transaccion("admin") as c:
        c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
        c.execute(
            "UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE estado='activa'"
        )
        c.execute("UPDATE mensajes SET estado='cancelado' WHERE estado='programado'")
        lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        lim.update(topeDiarioNuevos=50, topePorDominio=2, separacionSegundos=[1, 1])
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(lim),))
        c.execute(
            "UPDATE integraciones SET estado='conectada',credenciales_cifradas=%s WHERE id='gmail'",
            (cifrar("falso"),),
        )
        c.execute(
            "INSERT INTO latidos_trabajador(trabajador,visto_en) VALUES('prueba',now()) "
            "ON CONFLICT (trabajador) DO UPDATE SET visto_en=now()"
        )
    yield reloj
    with transaccion("admin") as c:
        c.execute("UPDATE mensajes SET estado='cancelado' WHERE estado='programado'")
        c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
        c.execute(
            "UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE estado='activa'"
        )
        c.execute(
            "UPDATE integraciones SET estado='desconectada',credenciales_cifradas=NULL WHERE id='gmail'"
        )


@pytest.fixture
def base_bajas():
    """Base secundaria real y vacía, migrada con alembic_bajas.ini."""
    base = os.environ.get("TEST_ADMIN_DATABASE_URL") or os.environ["DATABASE_URL"]
    params = conninfo_to_dict(base)
    params["dbname"] = "postgres"
    admin = make_conninfo(**params)
    nombre = "nexus_bajas_" + uuid4().hex[:10]
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre)))
    params["dbname"] = nombre
    url = make_conninfo(**params)
    from sqlalchemy.engine import URL

    url_alembic = URL.create(
        "postgresql",
        username=params.get("user"),
        password=params.get("password"),
        host=params.get("host"),
        port=int(params.get("port", "5432")),
        database=nombre,
    ).render_as_string(hide_password=False)
    subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic_bajas.ini", "upgrade", "head"],
        check=True,
        env={**os.environ, "UNSUBSCRIBE_DATABASE_URL": url_alembic},
    )
    yield url_alembic
    with psycopg.connect(admin, autocommit=True) as c:
        c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(nombre)))
    del url


def usar_base_bajas(monkeypatch, url: str) -> None:
    monkeypatch.setenv("UNSUBSCRIBE_DATABASE_URL", url)
    config.cache_clear()


def test_recorrido_completo(auth, entorno, base_bajas, monkeypatch):  # noqa: F811, C901
    reloj = entorno
    sufijo = uuid4().hex[:8]
    gmail = GmailFalso()

    # ------------------------------------------------ 1. contacto + empresa, importaciones
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Ana Ficticia", "correo": f" Ana@Uno-{sufijo}.example "},
            "empresaNueva": {"razonSocial": f"Ácme Ficticia {sufijo}", "municipio": "Palmira"},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["empresaCreada"]
    empresa_acme = r.json()["empresaId"]
    ana = r.json()["id"]
    # Una empresa parecida exige confirmar; un contacto que falla no deja empresa creada.
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Otro", "correo": f"otro@uno-{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Acme Ficticia {sufijo}", "municipio": "Palmira"},
        },
    )
    assert r.status_code == 409 and "parecida" in r.json()["message"]
    antes = len(auth.get("/api/empresas").json())
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Repetida", "correo": f"ana@uno-{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Empresa Nueva {sufijo}", "municipio": "Cali"},
        },
    )
    assert r.status_code == 409  # correo repetido
    assert len(auth.get("/api/empresas").json()) == antes  # sin empresa huérfana

    r = auth.post("/api/listas", json={"nombre": f"Clientes potenciales {sufijo}"})
    assert r.status_code == 200, r.text
    lista = r.json()["id"]
    opciones = {"existentes": "omitir", "listaId": lista, "crearEmpresas": True}
    csv_txt = (
        "Nombre;Correo electrónico;Teléfono;Empresa;Municipio\n"
        f"José Pérez;  JOSE@Dos-{sufijo}.example ;3001234567;Ácme Ficticia {sufijo};Palmira\n"
        ";;;;\n"
        f"Lucía Núñez;lucia@tres-{sufijo}.example;+57 310 555 0000;Beta {sufijo};Cali\n"
        f"José Repetido;jose@dos-{sufijo}.example;;Beta {sufijo};Cali\n"
    ).encode("utf-8")
    res = importar(auth, "contactos.csv", csv_txt, opciones)
    assert res["analisis"]["filasVacias"] == 1
    assert res["previa"]["resumen"] == {"crear": 2, "actualizar": 0, "omitir": 1, "error": 0}
    repetida = next(f for f in res["previa"]["filas"] if f["accion"] == "omitir")
    assert repetida["fila"] == 5 and "fila 2" in repetida["motivo"]
    assert res["creados"] == 2
    libro = xlsx(
        [
            ["Nombre", "Email", "Celular", "Empresa", "Ciudad", "Cargo"],
            [
                "Mario Ruiz",
                f"mario@cuatro-{sufijo}.example",
                3157778888,
                f"Beta {sufijo}",
                "Cali",
                "@SUM(1+1)",
            ],
            [
                "Ana Otra Vez",
                f"ANA@uno-{sufijo}.example",
                None,
                f"Ácme Ficticia {sufijo}",
                "Palmira",
                "",
            ],
            [None, None, None, None, None, None],
        ]
    )
    res = importar(auth, "contactos.xlsx", libro, {**opciones, "existentes": "actualizar"})
    assert res["creados"] == 1 and res["actualizados"] == 1
    contactos = {
        x["correo"]: x
        for x in auth.get("/api/contactos").json()
        if x["correo"] and sufijo in x["correo"]
    }
    assert set(contactos) == {
        f"ana@uno-{sufijo}.example",
        f"jose@dos-{sufijo}.example",
        f"lucia@tres-{sufijo}.example",
        f"mario@cuatro-{sufijo}.example",
    }
    # Actualizar con celda vacía conserva el dato; el nombre sí se actualiza.
    assert contactos[f"ana@uno-{sufijo}.example"]["nombre"] == "Ana Otra Vez"
    assert contactos[f"mario@cuatro-{sufijo}.example"]["telefono"] == "+573157778888"
    assert contactos[f"jose@dos-{sufijo}.example"]["empresaId"] == empresa_acme
    ids = [x["id"] for x in contactos.values()]
    hacer_elegibles(ids)
    auth.post(f"/api/listas/{lista}/agregar", json={"contactoIds": [ana]})

    # ------------------------------------------------ 2. segmento, lista estática, exportar
    reglas = [
        {
            "grupo": "o",
            "reglas": [
                {"campo": "municipio", "operador": "es", "valor": "Palmira"},
                {"campo": "dominioCorreo", "operador": "contiene", "valor": f"tres-{sufijo}"},
            ],
        },
        {"campo": "baseLegal", "operador": "es", "valor": "aprobada"},
    ]
    prev = auth.post("/api/segmentos/previsualizar", json=reglas).json()
    # Palmira (Ana, José) O dominio tres (Lucía), Y base aprobada; con muestra.
    assert prev["conteo"] >= 3 and prev["muestra"]
    r = auth.post(
        "/api/segmentos",
        json={"nombre": f"Palmira o Tres {sufijo}", "reglas": reglas, "justificacion": "prueba"},
    )
    assert r.status_code == 200, r.text
    seg = next(
        s for s in auth.get("/api/segmentos").json() if s["nombre"] == f"Palmira o Tres {sufijo}"
    )
    r = auth.post(
        "/api/listas",
        json={"nombre": f"Foto {sufijo}", "segmentoId": seg["id"], "modo": "estatica"},
    )
    foto = r.json()["id"]
    vinculada = auth.post(
        "/api/listas",
        json={"nombre": f"Vinculada {sufijo}", "segmentoId": seg["id"], "modo": "vinculada"},
    ).json()["id"]
    ids_foto = {m["id"] for m in auth.get(f"/api/listas/{foto}/miembros").json()["miembros"]}
    assert {contactos[f"lucia@tres-{sufijo}.example"]["id"], ana} <= ids_foto
    # Cambiar un atributo (municipio de la empresa de Ana) cambia el segmento y la lista
    # vinculada, pero no la lista estática.
    from app.api.lecturas import ids_segmento

    with transaccion() as c:
        assert ana in ids_segmento(c, reglas)
    assert (
        auth.patch(f"/api/empresas/{empresa_acme}", json={"municipio": "Buga"}).status_code == 200
    )
    with transaccion() as c:
        ids_seg = set(ids_segmento(c, reglas))
    assert ana not in ids_seg and contactos[f"lucia@tres-{sufijo}.example"]["id"] in ids_seg
    ids_vinc = {m["id"] for m in auth.get(f"/api/listas/{vinculada}/miembros").json()["miembros"]}
    assert ana not in ids_vinc
    assert ids_foto == {
        m["id"] for m in auth.get(f"/api/listas/{foto}/miembros").json()["miembros"]
    }

    r = auth.get(f"/api/contactos/exportar?ambito=lista&listaId={lista}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/vnd.openxmlformats")
    wb = load_workbook(io.BytesIO(r.content))
    ws = wb.active
    encabezado = [c.value for c in ws[1]]
    filas = {row[1]: row for row in ws.iter_rows(min_row=2, values_only=True)}
    assert set(filas) == set(contactos)
    mario = filas[f"mario@cuatro-{sufijo}.example"]
    assert mario[encabezado.index("Teléfono")] == "+573157778888"
    assert mario[encabezado.index("Cargo")] == "'@SUM(1+1)"  # no se ejecuta como fórmula
    assert all(c.data_type == "s" for row in ws.iter_rows(min_row=2) for c in row if c.value)

    # ------------------------------------------------ 3. plantilla → secuencia → aprobar
    r = auth.post(
        "/api/plantillas",
        json={
            "nombre": f"Primer contacto {sufijo}",
            "asunto": "Una pregunta para {{empresa}}",
            "cuerpo": "Hola {{nombre}}, ¿conversamos? {{enlace}}\n\n{{firma}}",
        },
    )
    assert r.status_code == 200, r.text
    plantilla = r.json()["id"]
    assert (
        auth.post("/api/plantillas", json={"nombre": "x", "cuerpo": "Hola {{apodo}}"}).status_code
        == 400
    )
    vista = contrato(
        "vista",
        "vistaPlantilla",
        auth.post(f"/api/plantillas/{plantilla}/previsualizar", json={}).json(),
    )
    assert "Laura Gómez" in vista["texto"] and "Darme de baja" in vista["texto"]
    contenido = auth.get(f"/api/plantillas/{plantilla}/para-paso").json()
    assert "{{baja}}" in contenido["cuerpo"]
    r = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Secuencia {sufijo}",
            "objetivo": "Diagnóstico RSE",
            "llamadaAccion": "Hacer el diagnóstico",
            "listaId": lista,
        },
    )
    assert r.status_code == 200, r.text
    seq = r.json()["id"]
    borrador = auth.get(f"/api/secuencias/{seq}/borrador").json()["contenido"]
    borrador["pasos"] = [
        {
            "orden": 1,
            "asunto": contenido["asunto"],
            "cuerpo": contenido["cuerpo"],
            "esperaDiasHabiles": 0,
            "mismoHilo": False,
            "variables": [],
            "formato": "texto",
            "plantillaId": plantilla,
        },
        {
            "orden": 2,
            "cuerpo": "Hola {{nombre}}, le escribo de nuevo por si le sirve. {{firma}}\n{{baja}}",
            "esperaDiasHabiles": 1,
            "mismoHilo": True,
            "variables": [],
            "formato": "texto",
        },
    ]
    assert (
        auth.put(f"/api/secuencias/{seq}/borrador", json={"secuencia": borrador}).status_code == 200
    )
    # Editar la plantilla después no cambia lo que se aprueba (el paso copió su contenido).
    auth.put(
        f"/api/plantillas/{plantilla}",
        json={"nombre": "Editada", "asunto": "Otro asunto", "cuerpo": "Otro {{firma}} {{baja}}"},
    )
    rev = contrato(
        "revision", "revisionSecuencia", auth.get(f"/api/secuencias/{seq}/revision").json()
    )
    assert rev["puedeAprobar"], rev
    contrato("listas", "lista", auth.get("/api/listas").json(), True)
    contrato("miembros", "miembrosLista", auth.get(f"/api/listas/{lista}/miembros").json())
    contrato("plantillas", "plantilla", auth.get("/api/plantillas").json(), True)
    contrato(
        "para-paso", "plantillaParaPaso", auth.get(f"/api/plantillas/{plantilla}/para-paso").json()
    )
    contrato("secuencia", "secuencia", auth.get(f"/api/secuencias/{seq}").json())
    contrato("segmentos", "segmento", auth.get("/api/segmentos").json(), True)
    contrato(
        "coincidencias",
        "coincidenciaEmpresa",
        auth.post(
            "/api/empresas/coincidencias", json={"razonSocial": f"Acme Ficticia {sufijo}"}
        ).json(),
        True,
    )
    assert rev["elegibles"] == 4 and rev["audiencia"]["tipo"] == "lista"
    with ThreadPoolExecutor(max_workers=2) as ex:  # doble clic
        respuestas = list(
            ex.map(lambda _: auth.post(f"/api/secuencias/{seq}/aprobar", json={}), range(2))
        )
    codigos = sorted(x.status_code for x in respuestas)
    assert codigos == [200, 400], [x.text for x in respuestas]
    prog = contrato(
        "aprobar", "aprobada", next(x for x in respuestas if x.status_code == 200).json()
    )["programacion"]
    assert prog["activada"] and prog["inscritos"] == 4
    estado = auth.get(f"/api/secuencias/{seq}/envios").json()
    assert estado["campanaEstado"] == "activa"
    assert {d["estado"] for d in estado["destinatarios"]} == {"programado"}
    with transaccion() as c:
        cam = uno(c, "SELECT * FROM campanas WHERE secuencia_id=%s", (seq,))
        congelado = uno(
            c,
            "SELECT contenido_congelado FROM aprobaciones_campana WHERE id=%s",
            (cam["aprobacion_vigente_id"],),
        )["contenido_congelado"]
    assert congelado["secuencia"]["pasos"][0]["asunto"] == "Una pregunta para {{empresa}}"

    programar(reloj, gmail)
    programar(reloj, gmail)  # una segunda vuelta del programador no duplica
    with transaccion() as c:
        msgs = todos(
            c,
            "SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id "
            "WHERE i.campana_id=%s ORDER BY programado_para",
            (cam["id"],),
        )
    assert len(msgs) == 4 and {m["paso_orden"] for m in msgs} == {1}
    texto = msgs[0]["cuerpo"]
    assert "Rechazar / Darme de baja: http" in texto
    for m in msgs:
        reloj.instante += timedelta(seconds=5)
        with ThreadPoolExecutor(max_workers=2) as ex:  # dos trabajadores a la vez
            list(ex.map(lambda _, m=m: enviar(str(m["id"]), reloj, gmail), range(2)))
    assert gmail.llamadas == 4
    estado = contrato("envios", "envios", auth.get(f"/api/secuencias/{seq}/envios").json())
    assert {d["estado"] for d in estado["destinatarios"]} == {"aceptado"}
    assert estado["mensajes"] == {"enviado": 4}
    # Repetir la aprobación (sin cambios) y «reiniciar» el trabajador: nada se duplica.
    assert auth.post(f"/api/secuencias/{seq}/aprobar", json={}).status_code == 400
    assert auth.post(f"/api/secuencias/{seq}/programar", json={}).status_code == 200
    programar(reloj, gmail)
    for m in msgs:
        enviar(str(m["id"]), reloj, gmail)
    assert gmail.llamadas == 4

    # ------------------------------------------------ 4. baja con la secundaria apagada
    usar_base_bajas(monkeypatch, "postgresql://nadie:x@127.0.0.1:1/apagada")
    lucia = contactos[f"lucia@tres-{sufijo}.example"]["id"]
    m_lucia = next(m for m in msgs if str(m["contacto_id"]) == lucia)
    token = re.search(r"/baja/(\S+)", m_lucia["cuerpo"])[1]
    assert token == firma_baja().dumps(str(m_lucia["id"]))
    pagina = auth.get("/baja/" + token)
    assert pagina.status_code == 200 and "Confirmar baja" in pagina.text  # GET no da de baja
    assert not auth.get(f"/api/contactos/{lucia}").json()["excluido"]
    assert auth.post("/baja/" + token).status_code == 200
    assert auth.post("/baja/" + token).status_code == 200  # idempotente
    assert auth.get(f"/api/contactos/{lucia}").json()["excluido"]
    from app.integraciones.bajas import sincronizar

    assert sincronizar()["error"]  # secundaria apagada: queda pendiente
    sup = next(
        s
        for s in auth.get("/api/supresiones").json()
        if s["correo"] == f"lucia@tres-{sufijo}.example"
    )
    assert sup["sincronizacion"]["estado"] == "pendiente" and sup["sincronizacion"]["ultimoError"]
    # Día siguiente: segundo paso solo para los otros tres; el de Lucía no existe.
    reloj.instante = datetime.fromisoformat("2026-12-02T08:40:00-05:00")
    programar(reloj, gmail)
    with transaccion() as c:
        segundos = todos(
            c,
            "SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id "
            "WHERE i.campana_id=%s AND m.paso_orden=2 AND m.estado<>'cancelado'",
            (cam["id"],),
        )
        ins_lucia = uno(
            c,
            "SELECT * FROM inscripciones WHERE campana_id=%s AND contacto_id=%s",
            (cam["id"], lucia),
        )
        miembro = uno(
            c, "SELECT * FROM lista_miembros WHERE lista_id=%s AND contacto_id=%s", (lista, lucia)
        )
    assert len(segundos) == 3 and lucia not in {str(m["contacto_id"]) for m in segundos}
    assert ins_lucia["estado"] == "finalizada" and ins_lucia["motivo_salida"] == "baja"
    assert not miembro["activo"] and miembro["motivo_retiro"] == "baja"
    # Reimportar el correo dado de baja no lo reactiva.
    res = importar(
        auth,
        "otra.csv",
        f"nombre,correo\nLucía Núñez,lucia@tres-{sufijo}.example\n".encode(),
        {**opciones, "existentes": "actualizar"},
    )
    assert res["previa"]["filas"][0]["accion"] == "omitir"
    assert "baja" in res["previa"]["filas"][0]["motivo"]
    assert auth.get(f"/api/contactos/{lucia}").json()["excluido"]
    # Una campaña nueva con la misma audiencia la excluye.
    r = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Nueva {sufijo}",
            "objetivo": "Seguimiento",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    )
    rev2 = auth.get(f"/api/secuencias/{r.json()['id']}/revision").json()
    assert rev2["elegibles"] == 3 and rev2["candidatos"] == 3

    # ------------------------------------------------ 5. secundaria restablecida
    usar_base_bajas(monkeypatch, base_bajas)
    with transaccion() as c:
        c.execute("UPDATE sincronizacion_bajas SET proximo_intento=now() WHERE estado='pendiente'")
    assert sincronizar()["error"] is None
    assert sincronizar()["copiadas"] == 0
    with transaccion() as c:  # una confirmación repetida no crea otra fila
        c.execute(
            "UPDATE sincronizacion_bajas SET estado='pendiente',sincronizada_en=NULL "
            "WHERE correo=%s",
            (f"lucia@tres-{sufijo}.example",),
        )
    sincronizar()
    with psycopg.connect(base_bajas.replace("postgresql+psycopg://", "postgresql://")) as s:
        filas = s.execute(
            "SELECT correo, origen FROM bajas.usuarios_baja WHERE correo=%s",
            (f"lucia@tres-{sufijo}.example",),
        ).fetchall()
    assert filas == [(f"lucia@tres-{sufijo}.example", "enlace_baja")]
    sup = next(
        s
        for s in auth.get("/api/supresiones").json()
        if s["correo"] == f"lucia@tres-{sufijo}.example"
    )
    assert sup["sincronizacion"]["estado"] == "sincronizada"
    contrato("supresiones", "supresion", auth.get("/api/supresiones").json(), True)
    contrato("bajas", "resumenBajas", auth.get("/api/bajas/sincronizacion").json())
    contrato(
        "con-empresa",
        "contactoCreado",
        auth.post(
            "/api/contactos/con-empresa",
            json={
                "contacto": {"nombre": "Contrato", "correo": f"contrato@k-{sufijo}.example"},
                "empresaId": empresa_acme,
            },
        ).json(),
    )
    guardar_contratos()
    config.cache_clear()


def test_conciliacion_sin_reenvio(auth, entorno):  # noqa: F811
    """Gmail aceptó pero el proceso cayó antes de guardar: no se reenvía a ciegas."""
    reloj = entorno
    sufijo = uuid4().hex[:8]
    gmail = GmailFalso()
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Control", "correo": f"control@c-{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Control {sufijo}", "municipio": "Cali"},
        },
    )
    cid = r.json()["id"]
    hacer_elegibles([cid])
    lista = auth.post(
        "/api/listas", json={"nombre": f"Control {sufijo}", "contactoIds": [cid]}
    ).json()["id"]
    seq = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Control {sufijo}",
            "objetivo": "Probar",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    ).json()["id"]
    r = auth.post(f"/api/secuencias/{seq}/aprobar", json={})
    assert r.status_code == 200 and r.json()["programacion"]["inscritos"] == 1
    programar(reloj, gmail)
    with transaccion() as c:
        m = uno(
            c,
            "SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id "
            "JOIN campanas ca ON ca.id=i.campana_id WHERE ca.secuencia_id=%s",
            (seq,),
        )
        c.execute("UPDATE mensajes SET estado='enviando' WHERE id=%s", (m["id"],))
    reloj.instante += timedelta(minutes=5)
    # Gmail «aún no lo muestra» en la búsqueda: queda por conciliar, sin reenvío.
    original = gmail.buscar
    gmail.buscar = lambda clave: None  # type: ignore[method-assign]
    enviar(str(m["id"]), reloj, gmail)
    enviar(str(m["id"]), reloj, gmail)
    assert gmail.llamadas == 0
    estado = auth.get(f"/api/secuencias/{seq}/envios").json()
    assert estado["destinatarios"][0]["estado"] == "conciliar"
    assert any(d["accion"] == "conciliar" for d in estado["diagnostico"])
    # Más tarde Gmail sí lo encuentra: se registra como aceptado sin volver a enviar.
    gmail.buscar = lambda clave: {"id": "gmail-real-1", "threadId": "hilo-1"}  # type: ignore[method-assign]
    assert conciliar_pendientes(gmail, reloj) == 1
    gmail.buscar = original  # type: ignore[method-assign]
    estado = auth.get(f"/api/secuencias/{seq}/envios").json()
    assert estado["destinatarios"][0]["estado"] in ("aceptado", "completada")
    assert gmail.llamadas == 0


def test_supresion_justo_antes_de_entregar(auth, entorno):  # noqa: F811
    reloj = entorno
    sufijo = uuid4().hex[:8]
    gmail = GmailFalso()
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Último", "correo": f"ultimo@u-{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Último {sufijo}", "municipio": "Cali"},
        },
    )
    cid = r.json()["id"]
    hacer_elegibles([cid])
    lista = auth.post(
        "/api/listas", json={"nombre": f"Último {sufijo}", "contactoIds": [cid]}
    ).json()["id"]
    seq = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Último {sufijo}",
            "objetivo": "Probar",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    ).json()["id"]
    auth.post(f"/api/secuencias/{seq}/aprobar", json={})
    programar(reloj, gmail)
    with transaccion() as c:
        m = uno(
            c,
            "SELECT m.* FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id "
            "JOIN campanas ca ON ca.id=i.campana_id WHERE ca.secuencia_id=%s",
            (seq,),
        )
    import app.motor.envios as envios_mod

    real = envios_mod.puede_entregarse

    def baja_en_el_ultimo_instante(c, msg):
        with transaccion("proceso:bajas") as otra:
            otra.execute(
                "INSERT INTO supresiones(correo,motivo,origen) VALUES(%s,'Pidió la baja','enlace_baja')",
                (f"ultimo@u-{sufijo}.example",),
            )
        return real(c, msg)

    envios_mod.puede_entregarse = baja_en_el_ultimo_instante
    try:
        reloj.instante += timedelta(minutes=30)
        enviar(str(m["id"]), reloj, gmail)
    finally:
        envios_mod.puede_entregarse = real
    assert gmail.llamadas == 0
    with transaccion() as c:
        assert (
            uno(c, "SELECT estado FROM mensajes WHERE id=%s", (m["id"],))["estado"] == "cancelado"
        )


def test_sin_gmail_o_sin_elegibles_explica(auth, entorno):  # noqa: F811
    sufijo = uuid4().hex[:8]
    r = auth.post(
        "/api/contactos/con-empresa",
        json={
            "contacto": {"nombre": "Sin base", "correo": f"sinbase@s-{sufijo}.example"},
            "empresaNueva": {"razonSocial": f"Sin base {sufijo}", "municipio": "Cali"},
        },
    )
    lista = auth.post(
        "/api/listas", json={"nombre": f"Sin base {sufijo}", "contactoIds": [r.json()["id"]]}
    ).json()["id"]
    seq = auth.post(
        "/api/secuencias",
        json={
            "nombre": f"Sin base {sufijo}",
            "objetivo": "Probar",
            "llamadaAccion": "Ver",
            "listaId": lista,
        },
    ).json()["id"]
    r = auth.post(f"/api/secuencias/{seq}/aprobar", json={})
    assert r.status_code == 200
    p = r.json()["programacion"]
    assert not p["activada"] and "elegibles" in p["motivo"]
    estado = auth.get(f"/api/secuencias/{seq}/envios").json()
    assert estado["campanaEstado"] == "aprobada"
    assert any("elegibles" in d["texto"] for d in estado["diagnostico"])
    with transaccion() as c:
        c.execute(
            "UPDATE integraciones SET estado='desconectada',credenciales_cifradas=NULL WHERE id='gmail'"
        )
        c.execute("DELETE FROM latidos_trabajador")
    r = auth.post(f"/api/secuencias/{seq}/programar", json={})
    assert "Gmail no está conectado" in r.json()["programacion"]["motivo"]
    textos = " ".join(
        d["texto"] for d in auth.get(f"/api/secuencias/{seq}/envios").json()["diagnostico"]
    )
    assert "Gmail no está conectado" in textos and "trabajador no da señales" in textos
