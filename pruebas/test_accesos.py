"""Solicitudes de acceso con Google, roles y centro de notificaciones.

Google se simula solo en el intercambio OAuth (`intercambio`): la ruta real del callback, la
base de datos, las sesiones, los permisos y las notificaciones son los de producción.
"""

import os
import subprocess
import sys
import threading
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from starlette.requests import Request

from app.config import config
from app.db import todos, transaccion, uno
from app.main import crear_app
from app.seguridad import auth


@pytest.fixture
def client(monkeypatch):
    config.cache_clear()
    identidad = {}

    def intercambio_simulado(request, finalidad):
        if not identidad:
            raise auth.HTTPException(400, "Solicitud OAuth no válida")
        return {"correo": identidad["correo"], "nombre": identidad.get("nombre", ""), "sub": "s"}, {}

    monkeypatch.setattr(auth, "intercambio", intercambio_simulado)
    with TestClient(crear_app(), base_url=config().app_url, follow_redirects=False) as c:
        c.headers["Origin"] = config().app_url
        c.identidad = identidad
        yield c


def con_google(client, correo, nombre=""):
    """Simula volver de Google con una identidad verificada. Devuelve la redirección."""
    client.cookies.clear()
    client.identidad.clear()
    client.identidad.update(correo=correo, nombre=nombre)
    return client.get("/api/auth/google/callback?state=x&code=y")


def como_admin(client):
    client.cookies.clear()
    r = client.get("/api/auth/google")  # AUTH_DEV_BYPASS: el administrador configurado
    assert r.status_code == 302 and client.cookies.get("nexus_sesion")


def avisos_de(correo, clave_prefijo=""):
    with transaccion() as c:
        return todos(
            c,
            "SELECT n.* FROM notificaciones n JOIN administradores a ON a.id=n.destinatario_id "
            "WHERE a.correo=%s AND n.clave LIKE %s",
            (correo, clave_prefijo + "%"),
        )


def solicitud_de(correo):
    with transaccion() as c:
        return uno(c, "SELECT * FROM solicitudes_acceso WHERE correo=%s", (correo,), False)


def nuevo_correo():
    return f"externa-{uuid4().hex[:10]}@gmail.example"


def test_recorrido_aprobacion_completo(client):
    admin = config().admin_correo
    externa = nuevo_correo()
    # 1) Primera visita: solicitud pendiente, sin cookie de sesión ni datos privados.
    r = con_google(client, externa, "Ana Externa")
    assert r.headers["location"].endswith("/login?solicitud=enviada")
    assert not client.cookies.get("nexus_sesion")
    assert client.get("/api/auth/session").status_code == 401
    assert client.get("/api/panel/resumen").status_code == 401
    sol = solicitud_de(externa)
    assert sol["estado"] == "pendiente" and sol["nombre"] == "Ana Externa"
    avisos = avisos_de(admin, f"solicitud:{sol['id']}")
    assert len(avisos) == 1 and avisos[0]["enlace"] == f"/solicitudes-acceso?id={sol['id']}"
    # 2) Intento repetido: estado actual, sin otra solicitud ni otro aviso.
    r = con_google(client, externa, "Ana Externa")
    assert r.headers["location"].endswith("/login?solicitud=pendiente")
    with transaccion() as c:
        assert uno(c, "SELECT count(*) AS n FROM solicitudes_acceso WHERE correo=%s", (externa,))["n"] == 1
    avisos = avisos_de(admin, f"solicitud:{sol['id']}")
    assert len(avisos) == 1 and avisos[0]["repeticiones"] == 1
    # 3) El administrador la ve primero entre las pendientes, con el aviso enlazado.
    como_admin(client)
    bandeja = client.get("/api/notificaciones").json()
    aviso = next(a for a in bandeja["items"] if a["titulo"] == "Nueva solicitud de acceso" and externa in a["descripcion"])
    assert aviso["enlace"].startswith("/solicitudes-acceso") and not aviso["leida"]
    lista = client.get("/api/solicitudes-acceso").json()
    assert lista[0]["estado"] == "pendiente"
    fila = next(x for x in lista if x["correo"] == externa)
    assert fila["nombre"] == "Ana Externa" and "decididaPor" not in fila
    r = client.post(f"/api/solicitudes-acceso/{fila['id']}/aprobar", json={})
    assert r.status_code == 200, r.text
    decidida = r.json()["solicitud"]
    assert decidida["estado"] == "aprobada" and decidida["decididaPor"]["correo"] == admin
    assert decidida["decididaEn"]
    # Historial y auditoría en la misma transacción que la decisión.
    with transaccion() as c:
        assert uno(c, "SELECT count(*) AS n FROM registro_auditoria WHERE entidad='solicitudes_acceso' AND entidad_id=%s AND accion='aprobar_solicitud_acceso'", (str(fila["id"]),))["n"] >= 1
    # 4) La persona vuelve a entrar como «Usuario» y ve su aviso en el primer ingreso.
    r = con_google(client, externa, "Ana Externa")
    assert r.headers["location"] == config().app_url + "/"
    sesion = client.get("/api/auth/session").json()
    assert sesion["cuenta"]["rol"] == "usuario" and sesion["cuenta"]["rolEtiqueta"] == "Usuario"
    assert sesion["permisos"]["pantallas"] == ["panel"]
    bandeja = client.get("/api/notificaciones").json()
    assert bandeja["sinLeer"] == 1
    assert bandeja["items"][0]["titulo"] == "Tu acceso fue aprobado"
    assert client.get("/api/panel/resumen").status_code == 200
    assert client.get("/api/embudo").status_code == 200


def test_usuario_sin_permisos_de_administrador(client):
    externa = nuevo_correo()
    con_google(client, externa)
    como_admin(client)
    sol = solicitud_de(externa)
    assert client.post(f"/api/solicitudes-acceso/{sol['id']}/aprobar", json={}).status_code == 200
    # Aviso de otra persona (del administrador) para probar el aislamiento de la bandeja.
    ajeno = avisos_de(config().admin_correo)[0]["id"]
    otra = nuevo_correo()
    con_google(client, otra)
    otra_sol = solicitud_de(otra)
    con_google(client, externa)
    assert client.get("/api/auth/session").json()["cuenta"]["rol"] == "usuario"
    # 403: sesión válida sin permiso (no 401).
    assert client.get("/api/solicitudes-acceso").status_code == 403
    r = client.post(f"/api/solicitudes-acceso/{otra_sol['id']}/aprobar", json={})
    assert r.status_code == 403 and r.json()["message"] == auth.SIN_PERMISO
    assert solicitud_de(otra)["estado"] == "pendiente"
    for ruta in ("contactos", "configuracion/marca", "integraciones", "auditoria", "campanas", "terminal"):
        assert client.get("/api/" + ruta).status_code == 403, ruta
    assert client.post("/api/campanas/x/pausar", json={}).status_code == 403
    assert client.put("/api/configuracion/marca", json={}).status_code == 403
    assert client.post("/api/integraciones/gmail/desconectar", json={}).status_code == 403
    assert client.get("/api/integraciones/gmail/autorizar").status_code == 403
    # Notificaciones ajenas: ni se ven ni se pueden marcar.
    ids = {a["id"] for a in client.get("/api/notificaciones").json()["items"]}
    assert str(ajeno) not in ids
    assert client.post(f"/api/notificaciones/{ajeno}/leer", json={}).status_code == 404
    client.post("/api/notificaciones/leer-todas", json={})
    with transaccion() as c:
        assert uno(c, "SELECT leida_en FROM notificaciones WHERE id=%s", (ajeno,))["leida_en"] is None
    # Sin cookie o con una sesión vencida: 401, distinto de 403.
    token = client.cookies.get("nexus_sesion")
    with transaccion() as c:
        c.execute("UPDATE sesiones SET creada_en=now()-interval '13 hours', expira_en=now()-interval '1 hour' WHERE token_hash=%s", (auth.digest(token),))
    r = client.get("/api/panel/resumen")
    assert r.status_code == 401 and "venció" in r.json()["message"]


def test_rechazo_y_reintento(client):
    admin = config().admin_correo
    externa = nuevo_correo()
    con_google(client, externa)
    sol = solicitud_de(externa)
    como_admin(client)
    r = client.post(f"/api/solicitudes-acceso/{sol['id']}/rechazar", json={})
    assert r.status_code == 200 and r.json()["solicitud"]["estado"] == "rechazada"
    for _ in range(2):
        r = con_google(client, externa)
        assert r.headers["location"].endswith("/login?solicitud=rechazada")
        assert not client.cookies.get("nexus_sesion")
    assert len(avisos_de(admin, f"solicitud:{sol['id']}")) == 1
    with transaccion() as c:
        assert not uno(c, "SELECT 1 AS si FROM administradores WHERE correo=%s", (externa,), False)
    # Aprobar después de rechazar no cambia la decisión.
    como_admin(client)
    r = client.post(f"/api/solicitudes-acceso/{sol['id']}/aprobar", json={})
    assert r.status_code == 409 and r.json()["solicitud"]["estado"] == "rechazada"


def peticion_con(token):
    scope = {"type": "http", "headers": [(b"cookie", f"nexus_sesion={token}".encode())]}
    return Request(scope)


def test_dos_decisiones_simultaneas(client):
    externa = nuevo_correo()
    con_google(client, externa)
    sol = solicitud_de(externa)
    # Segundo administrador activo con su propia sesión.
    segundo = f"admin2-{uuid4().hex[:8]}@nexusinnovacion.com"
    with transaccion() as c:
        c.execute("INSERT INTO administradores(correo,nombre,rol) VALUES(%s,'Segunda Admin','administrador')", (segundo,))
    tokens = []
    for correo in (config().admin_correo, segundo):
        r = con_google(client, correo)
        assert r.status_code == 302
        tokens.append(client.cookies.get("nexus_sesion"))
    from app.seguridad.accesos import decidir

    barrera = threading.Barrier(2)
    resultados = {}

    def actuar(nombre, token, decision):
        barrera.wait()
        resultados[nombre] = decidir(peticion_con(token), str(sol["id"]), decision)

    hilos = [
        threading.Thread(target=actuar, args=("a", tokens[0], "aprobar")),
        threading.Thread(target=actuar, args=("b", tokens[1], "rechazar")),
    ]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    exitos = [r for r in resultados.values() if isinstance(r, dict)]
    conflictos = [r for r in resultados.values() if not isinstance(r, dict)]
    assert len(exitos) == 1 and len(conflictos) == 1
    assert conflictos[0].status_code == 409
    import json

    cuerpo = json.loads(conflictos[0].body)
    final = solicitud_de(externa)
    assert cuerpo["solicitud"]["estado"] == final["estado"] == exitos[0]["solicitud"]["estado"]
    assert "Se conserva esa decisión" in cuerpo["message"]
    with transaccion() as c:
        n = uno(c, "SELECT count(*) AS n FROM registro_auditoria WHERE entidad='solicitudes_acceso' AND entidad_id=%s AND accion IN ('aprobar_solicitud_acceso','rechazar_solicitud_acceso') AND antes IS NULL", (str(sol["id"]),))["n"]
        cuentas = uno(c, "SELECT count(*) AS n FROM administradores WHERE correo=%s", (externa,))["n"]
    assert n == 1  # una sola decisión auditada (la del disparador lleva antes/despues)
    assert cuentas == (1 if final["estado"] == "aprobada" else 0)


def test_administrador_existente_y_desactivado(client):
    from app.main import asegurar_administrador

    admin = config().admin_correo
    r = con_google(client, admin)
    assert r.headers["location"] == config().app_url + "/"
    assert client.get("/api/auth/session").json()["cuenta"]["rol"] == "administrador"
    assert client.get("/api/contactos").status_code == 200
    # Cuenta administrativa desactivada: ni sesión ni solicitud nueva.
    inactiva = f"inactiva-{uuid4().hex[:8]}@nexusinnovacion.com"
    with transaccion() as c:
        c.execute("INSERT INTO administradores(correo,nombre,rol,activo) VALUES(%s,'Antigua','administrador',false)", (inactiva,))
    r = con_google(client, inactiva)
    assert r.headers["location"].endswith("/login?error=acceso_denegado")
    assert not client.cookies.get("nexus_sesion") and not solicitud_de(inactiva)
    # El arranque no reactiva al administrador configurado si fue desactivado a propósito.
    with transaccion() as c:
        c.execute("UPDATE administradores SET activo=false WHERE correo=%s", (admin,))
        asegurar_administrador(c, admin)
        assert uno(c, "SELECT activo,rol FROM administradores WHERE correo=%s", (admin,)) == {"activo": False, "rol": "administrador"}
        c.execute("UPDATE administradores SET activo=true WHERE correo=%s", (admin,))


def test_avisos_leidos_persisten(client):
    externa = nuevo_correo()
    con_google(client, externa)
    como_admin(client)
    bandeja = client.get("/api/notificaciones").json()
    aviso = next(a for a in bandeja["items"] if externa in a["descripcion"])
    assert client.post(f"/api/notificaciones/{aviso['id']}/leer", json={}).status_code == 200
    antes = bandeja["sinLeer"]
    client.get("/api/auth/logout")
    assert client.get("/api/notificaciones").status_code == 401
    como_admin(client)
    bandeja = client.get("/api/notificaciones").json()
    assert next(a for a in bandeja["items"] if a["id"] == aviso["id"])["leida"]
    assert bandeja["sinLeer"] == antes - 1
    assert client.post("/api/notificaciones/leer-todas", json={}).json()["ok"]
    como_admin(client)
    assert client.get("/api/notificaciones").json()["sinLeer"] == 0
    assert client.post("/api/notificaciones/no-es-uuid/leer", json={}).status_code == 404


def test_fallos_repetidos_se_agrupan(client):
    from app.notificaciones import avisar_fallo_tarea

    admin = config().admin_correo
    motivo = "Motivo de prueba " + uuid4().hex
    with transaccion("proceso:trabajador") as c:
        for _ in range(3):
            avisar_fallo_tarea(c, "sincronizar_ga4", "Importar visitas", motivo)
            avisar_fallo_tarea(c, "descubrir_empresas", "Búsqueda de empresas", motivo)
    integ = [a for a in avisos_de(admin, "integracion:sincronizar_ga4:") if motivo in a["descripcion"]]
    tarea = [a for a in avisos_de(admin, "tarea:descubrir_empresas:") if motivo in a["descripcion"]]
    assert len(integ) == 1 and integ[0]["repeticiones"] == 3 and integ[0]["tipo"] == "fallo_integracion"
    assert integ[0]["enlace"] == "/configuracion"
    assert len(tarea) == 1 and tarea[0]["repeticiones"] == 3 and tarea[0]["enlace"] == "/descubrimiento"
    # Un incidente que había cesado vuelve a aparecer sin leer.
    with transaccion() as c:
        c.execute("UPDATE notificaciones SET leida_en=now(), ultima_vez=now()-interval '7 hours' WHERE id=%s", (tarea[0]["id"],))
        avisar_fallo_tarea(c, "descubrir_empresas", "Búsqueda de empresas", motivo)
        r = uno(c, "SELECT leida_en,repeticiones FROM notificaciones WHERE id=%s", (tarea[0]["id"],))
    assert r["leida_en"] is None and r["repeticiones"] == 1


def test_migracion_sobre_base_existente():
    """0007 con datos → 0008 → 0007 → 0008, en una base aparte."""
    base = os.environ.get("TEST_ADMIN_DATABASE_URL") or os.environ["DATABASE_URL"]
    params = conninfo_to_dict(base)
    params["dbname"] = "postgres"
    admin_url = make_conninfo(**params)
    nombre = "nexus_mig_" + uuid4().hex
    with psycopg.connect(admin_url, autocommit=True) as c:
        c.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(nombre)))
    params["dbname"] = nombre
    url = make_conninfo(**params)
    from sqlalchemy.engine import URL

    alembic_url = URL.create(
        "postgresql", username=params.get("user"), password=params.get("password"),
        host=params.get("host"), port=int(params.get("port", "5432")), database=nombre,
    ).render_as_string(hide_password=False)
    entorno = {**os.environ, "DATABASE_URL": alembic_url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], check=True, env=entorno)

    try:
        alembic("upgrade", "0007")
        with psycopg.connect(url) as c:
            c.execute("INSERT INTO nexus.administradores(correo,nombre) VALUES('a@x.example','A'),('b@x.example','B')")
            c.execute("UPDATE nexus.administradores SET activo=false WHERE correo='b@x.example'")
        alembic("upgrade", "head")
        with psycopg.connect(url) as c:
            filas = c.execute("SELECT correo::text,rol,activo FROM nexus.administradores ORDER BY correo").fetchall()
            assert filas == [("a@x.example", "administrador", True), ("b@x.example", "administrador", False)]
            c.execute("INSERT INTO nexus.administradores(correo,nombre) VALUES('u@x.example','U')")
            assert c.execute("SELECT rol FROM nexus.administradores WHERE correo='u@x.example'").fetchone()[0] == "usuario"
        alembic("downgrade", "0007")
        with psycopg.connect(url) as c:
            assert c.execute("SELECT activo FROM nexus.administradores WHERE correo='u@x.example'").fetchone()[0] is False
        alembic("upgrade", "head")
    finally:
        with psycopg.connect(admin_url, autocommit=True) as c:
            c.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(nombre)))
