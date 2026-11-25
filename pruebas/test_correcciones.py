"""Pruebas de regresión de la lista de defectos (puntos 01-25)."""
# ruff: noqa: F811
import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httplib2
from googleapiclient.errors import HttpError
from psycopg.types.json import Jsonb
from pydantic import SecretStr
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.api.campanas import aprobar, nueva_propuesta
from app.api.lecturas import secuencia
from app.api.medicion import metricas
from app.config import config
from app.db import actualizar, insertar, todos, transaccion, uno
from app.dominio.reglas import BOGOTA
from app.esquemas.serializar import serializar
from app.integraciones.gmail import GmailFalso
from app.motor.envios import RelojFalso, programar
from app.motor.rastreador import registrar, sincronizar
from app.publico.rutas import firma_baja
from app.seguridad.auth import cifrar

VENTANA = {"dias": [1, 2, 3, 4, 5], "desde": "08:00", "hasta": "17:00"}


def admin_id(c):
    return str(uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))["id"])


def aislar_campanas(c):
    """Las pruebas comparten base: se pausan campañas activas de otras pruebas."""
    c.execute("UPDATE campanas SET estado='pausada' WHERE estado='activa'")
    c.execute("UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE estado='activa'")
    c.execute(
        "UPDATE mensajes SET estado='cancelado' WHERE estado='programado'"
    )


def crear_campana(c, admin, nombre, contactos=2, ventana=None, activar=True, ahora=None):
    ahora = ahora or datetime.now(timezone.utc)
    c.execute("SELECT set_config('nexus.administrador_id',%s,true)", (admin,))
    e = insertar(c, "empresas", {"razon_social": nombre + " SAS", "municipio": "Palmira"})
    ids, correos = [], []
    for i in range(contactos):
        correo = f"p{i}@{uuid4().hex}.example"
        ct = insertar(
            c,
            "contactos",
            {
                "empresa_id": e["id"],
                "nombre": f"{nombre} {i}",
                "correo": correo,
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
                "prueba": "Fixture",
                "aprobada_por": admin,
                "aprobada_en": ahora,
            },
        )
        ids.append(str(ct["id"]))
        correos.append(correo)
    seg = insertar(
        c,
        "segmentos",
        {
            "nombre": nombre,
            "reglas": Jsonb([{"campo": "contactoIds", "operador": "es", "valor": ",".join(ids)}]),
            "propuesto_por": "admin",
            "estado": "aprobado",
            "aprobado_por": admin,
            "aprobado_en": ahora,
        },
    )
    v = ventana or VENTANA
    seq = insertar(
        c,
        "secuencias",
        {
            "nombre": nombre,
            "objetivo": "Diagnóstico",
            "llamada_accion": "Ver",
            "ventana_dias": v["dias"],
            "ventana_desde": v["desde"],
            "ventana_hasta": v["hasta"],
        },
    )
    insertar(
        c,
        "pasos",
        {
            "secuencia_id": seq["id"],
            "orden": 1,
            "asunto": "Diagnóstico",
            "cuerpo": "Hola {{nombre}}, le invitamos a {{enlace}}. {{firma}} {{baja}}",
        },
    )
    cam = insertar(
        c,
        "campanas",
        {
            "nombre": nombre,
            "segmento_id": seg["id"],
            "secuencia_id": seq["id"],
            "estado": "en_revision",
            "tope_diario": 50,
        },
    )
    prop = nueva_propuesta(
        c,
        "estratega_redactor",
        "campana",
        "Prueba " + nombre,
        serializar(secuencia(c, seq)),
        {"objetivo_campana_id": cam["id"]},
    )
    aprobar(c, prop, admin)
    if activar:
        actualizar(c, "campanas", str(cam["id"]), {"estado": "activa"})
    inscripciones = [
        insertar(
            c,
            "inscripciones",
            {
                "campana_id": cam["id"],
                "contacto_id": cid,
                "estado": "activa" if activar else "pausada",
                "proximo_envio": ahora if activar else None,
            },
        )
        for cid in ids
    ]
    cam = uno(c, "SELECT * FROM campanas WHERE id=%s", (cam["id"],))
    return cam, ids, correos, inscripciones


def mensaje_enviado(c, cam, ins, correo, cuando, hilo=None):
    """Envío ya registrado. Use fechas pasadas: la separación global entre envíos toma
    el último envío de toda la base, y otras pruebas usan relojes falsos de noviembre."""
    return insertar(
        c,
        "mensajes",
        {
            "tipo": "secuencia",
            "contacto_id": ins["contacto_id"],
            "inscripcion_id": ins["id"],
            "aprobacion_id": cam["aprobacion_vigente_id"],
            "paso_orden": 1,
            "clave_idempotencia": uuid4().hex,
            "destinatario": correo,
            "asunto": "Diagnóstico",
            "cuerpo": "Mensaje de prueba con texto suficiente para la regla.",
            "estado": "enviado",
            "programado_para": cuando,
            "enviado_en": cuando,
            "gmail_message_id": "m-" + uuid4().hex,
            "gmail_thread_id": hilo or "t-" + uuid4().hex,
        },
    )


def lead_por_enlace(c, mensaje, cuando):
    ref = uuid4().hex
    insertar(
        c,
        "enlaces_seguimiento",
        {"referencia": ref, "mensaje_id": mensaje["id"], "url_destino": "https://nexus.example"},
    )
    insertar(
        c,
        "envios_formulario",
        {
            "id_externo": uuid4().hex,
            "formulario": "diagnostico",
            "autorizacion_aceptada": False,
            "referencia_opaca": ref,
            "recibido_en": cuando,
        },
    )


def bogota(fecha, hora="10:00"):
    return datetime.fromisoformat(f"{fecha}T{hora}:00-05:00")


# --- 01, 02 y 22: importación CSV ------------------------------------------------


def test_importacion_conteos_y_seleccion(auth):
    with transaccion() as c:
        e1 = insertar(c, "empresas", {"razon_social": "Importadora Uno", "municipio": "Cali"})
        e2 = insertar(c, "empresas", {"razon_social": "Importadora Dos", "municipio": "Cali"})
    existente = f"ya@{uuid4().hex}.example"
    assert (
        auth.post(
            "/api/contactos", json={"empresaId": str(e1["id"]), "nombre": "Ya", "correo": existente}
        ).status_code
        == 200
    )
    nuevo = f"nuevo@{uuid4().hex}.example"
    filas = [
        {"empresaId": str(e2["id"]), "nombre": "Nuevo", "correo": nuevo.upper(), "origen": "referencia",
         "baseLegal": {"tipo": "corporativo_generico", "aprobadaPorAdmin": True}},
        {"empresaId": str(e2["id"]), "nombre": "Repetido", "correo": existente, "origen": "referencia"},
        {"empresaId": str(e2["id"]), "nombre": "Mismo archivo", "correo": nuevo, "origen": "referencia"},
        {"empresaId": str(e2["id"]), "nombre": "Sin arroba", "correo": "correo-malo", "origen": "referencia"},
        {"empresaId": str(e2["id"]), "nombre": "  ", "correo": f"x@{uuid4().hex}.example", "origen": "referencia"},
    ]
    r = auth.post("/api/contactos/importar", json={"contactos": filas})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "creados": 1, "omitidos": 2, "rechazados": 2}
    creado = next(x for x in auth.get("/api/contactos").json() if x.get("correo") == nuevo)
    # Se usan exactamente la empresa, el origen y la base legal enviados; nunca aprobada.
    assert creado["empresaId"] == str(e2["id"]) and creado["origen"] == "referencia"
    assert creado["baseLegal"] == {"tipo": "corporativo_generico", "aprobadaPorAdmin": False}
    r = auth.post(
        "/api/contactos/importar",
        json={"contactos": [{**filas[0], "correo": f"z@{uuid4().hex}.example",
                             "baseLegal": {"tipo": "autorizacion_formulario"}}]},
    )
    assert r.status_code == 400


# --- 05: conversiones y visitas de días sin envíos ------------------------------------


def test_conversion_en_dia_sin_envios(auth):
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "Conversión " + uuid4().hex[:6])
        m = mensaje_enviado(c, cam, ins[0], correos[0], bogota("2026-11-02"))
        lead_por_enlace(c, m, bogota("2026-11-03", "11:00"))
        empresa = uno(c, "SELECT empresa_id FROM contactos WHERE id=%s", (ids[0],))["empresa_id"]
        insertar(
            c,
            "oportunidades",
            {
                "empresa_id": empresa,
                "contacto_id": ids[0],
                "etapa": "cliente",
                "campana_origen_id": cam["id"],
                "cerrada_en": bogota("2026-11-03", "16:00"),
            },
        )
        insertar(
            c,
            "metricas_web_diarias",
            {
                "fecha": "2026-11-03",
                "canal": "correo",
                "fuente": "nexus-pm",
                "medio": "email",
                "campana_utm": "cam-" + str(cam["id"])[:8],
                "sesiones": 5,
            },
        )
        rows = [
            r
            for r in metricas(c, {"desde": "2026-11-02", "hasta": "2026-11-03"})
            if r.get("campana_id") == cam["id"]
        ]
    por_dia = {str(r["fecha"]): r for r in rows}
    assert por_dia["2026-11-02"]["enviados"] == 1
    martes = por_dia["2026-11-03"]
    assert (martes["enviados"], martes["leads"], martes["clientes"], martes["visitas"]) == (0, 1, 1, 5)


# --- 06 y 12: costo por lead con dos campañas el mismo día ---------------------------


def test_costos_con_dos_campanas_el_mismo_dia(auth):
    dia = "2026-10-06"
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        a, _, correos_a, ins_a = crear_campana(c, admin, "Costo A " + uuid4().hex[:6])
        b, _, correos_b, ins_b = crear_campana(c, admin, "Costo B " + uuid4().hex[:6])
        for cam, ins, correos in ((a, ins_a, correos_a), (b, ins_b, correos_b)):
            m = mensaje_enviado(c, cam, ins[0], correos[0], bogota(dia, "09:00"))
            lead_por_enlace(c, m, bogota(dia, "15:00"))
    # La fecha real del gasto se elige al registrarlo.
    r = auth.post("/api/informes/costos", json={"costoUSD": 10, "fecha": dia, "concepto": "Pauta"})
    assert r.status_code == 200 and r.json()["fecha"] == dia
    futuro = (datetime.now(BOGOTA).date() + timedelta(days=2)).isoformat()
    assert auth.post("/api/informes/costos", json={"costoUSD": 1, "fecha": futuro}).status_code == 400
    assert auth.post("/api/informes/costos", json={"costoUSD": 1, "fecha": "06/10/2026"}).status_code == 400
    rows = auth.get(f"/api/informes/metricas?desde={dia}&hasta={dia}").json()
    assert sum(1 for r in rows if r["costoUSD"] is not None) == 1
    costo = sum(r["costoUSD"] or 0 for r in rows)
    leads = sum(r.get("leads", 0) for r in rows)
    assert costo == 10 and leads >= 2
    # Por fecha: el costo del día se divide entre todos sus leads, no solo los de una fila.
    assert costo / leads <= 5


# --- 07: horario global incompatible -------------------------------------------------


def test_horario_global_incompatible_aisla_campana(auth):
    martes = RelojFalso(bogota("2026-11-24", "09:00"))
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        manana, _, _, _ = crear_campana(
            c, admin, "Mañana " + uuid4().hex[:6], ventana={**VENTANA, "hasta": "12:00"},
            ahora=martes.ahora(),
        )
        tarde, _, _, _ = crear_campana(
            c, admin, "Tarde " + uuid4().hex[:6], ventana={**VENTANA, "desde": "13:00"},
            ahora=martes.ahora(),
        )
        limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        actualizar(c, "integraciones", "gmail", {"estado": "conectada", "credenciales_cifradas": cifrar("falso")})
    nuevos = {**limites, "ventana": {**limites["ventana"], "hasta": "12:00"}}
    r = auth.put("/api/configuracion/limites", json=nuevos)
    assert r.status_code == 409 and tarde["nombre"] in r.json()["message"]
    assert manana["nombre"] not in r.json()["message"]
    # Estado heredado: el horario global ya quedó incompatible antes de esta versión.
    with transaccion() as c:
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(nuevos),))
    programar(martes, GmailFalso())
    with transaccion() as c:
        estados = {
            r["nombre"]: r["estado"]
            for r in todos(c, "SELECT nombre,estado FROM campanas WHERE id=ANY(%s)", ([manana["id"], tarde["id"]],))
        }
        programados = uno(
            c,
            "SELECT count(*) AS n FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id WHERE i.campana_id=%s AND m.estado='programado'",
            (manana["id"],),
        )["n"]
    assert estados[tarde["nombre"]] == "pausada" and estados[manana["nombre"]] == "activa"
    assert programados == 2  # el lote de la otra campaña no se revirtió
    detalle = next(x for x in auth.get("/api/campanas").json() if x["id"] == str(tarde["id"]))
    assert "horario" in detalle["motivoPausa"]
    assert auth.post(f"/api/campanas/{tarde['id']}/reanudar", json={}).status_code == 400
    # Con confirmación explícita se guarda y se pausan las incompatibles.
    with transaccion() as c:
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(limites),))
    r = auth.put("/api/configuracion/limites?pausarIncompatibles=true", json=nuevos)
    assert r.status_code == 200 and tarde["nombre"] in r.json()["pausadas"]
    with transaccion() as c:
        c.execute("UPDATE configuracion SET valor=%s WHERE clave='limites'", (Jsonb(limites),))
        actualizar(c, "integraciones", "gmail", {"estado": "desconectada", "credenciales_cifradas": None})
        aislar_campanas(c)


# --- 09: auditoría con filtros en el servidor ----------------------------------------


def test_auditoria_busca_y_exporta_mas_alla_de_500(auth):
    marca = uuid4().hex[:8]
    with transaccion() as c:
        c.execute(
            "INSERT INTO registro_auditoria(fecha,actor,accion,entidad,entidad_id) VALUES(now()-interval '30 days','admin',%s,'pruebas','antigua')",
            ("antigua_" + marca,),
        )
        c.execute(
            "INSERT INTO registro_auditoria(actor,accion,entidad,entidad_id) SELECT 'admin',%s,'pruebas',g::text FROM generate_series(1,600) g",
            ("relleno_" + marca,),
        )
        # 23:30 en Bogotá del día 20 es el día 21 en UTC.
        c.execute(
            "INSERT INTO registro_auditoria(fecha,actor,accion,entidad,entidad_id) VALUES('2026-11-20T23:30:00-05:00','admin',%s,'pruebas','noche')",
            ("noche_" + marca,),
        )
    recientes = auth.get("/api/auditoria").json()
    assert len(recientes) == 500 and all(x["accion"] != "antigua_" + marca for x in recientes)
    r = auth.get("/api/auditoria/pagina?accion=ANTIGUA_" + marca).json()
    assert r["total"] == 1 and r["items"][0]["entidadId"] == "antigua"
    r = auth.get(f"/api/auditoria/pagina?accion=relleno_{marca}&tamano=50&pagina=12").json()
    assert r["total"] == 600 and len(r["items"]) == 50 and r["pagina"] == 12
    r = auth.get(f"/api/auditoria/pagina?accion=noche_{marca}&desde=2026-11-20&hasta=2026-11-20").json()
    assert r["total"] == 1
    csv = auth.get(f"/api/auditoria/exportar?accion=relleno_{marca}")
    assert csv.status_code == 200 and csv.headers["content-type"].startswith("text/csv")
    assert len(csv.text.strip().splitlines()) == 601
    assert auth.get("/api/auditoria/pagina?desde=21-11-2026").status_code == 400


# --- 03 y 04: desconexión real con proveedor de prueba -------------------------------


def firmar(cuerpo):
    raw = json.dumps(cuerpo).encode()
    fecha = str(int(time.time()))
    secreto = config().formulario_webhook_secreto.get_secret_value()
    return raw, {
        "X-Nexus-Fecha": fecha,
        "X-Nexus-Firma": hmac.new(secreto.encode(), fecha.encode() + b"." + raw, hashlib.sha256).hexdigest(),
        "Content-Type": "application/json",
    }


def test_desconectar_integraciones_reales(auth, monkeypatch):
    llamadas = []

    class Respuesta:
        status_code = 200

    def revocar(url, data, timeout):
        llamadas.append((url, data))
        return Respuesta()

    import app.integraciones.conexiones as conexiones

    monkeypatch.setattr(conexiones.httpx, "post", revocar)
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, _, _, _ = crear_campana(c, admin, "Gmail " + uuid4().hex[:6])
        actualizar(c, "integraciones", "gmail", {"estado": "conectada", "credenciales_cifradas": cifrar("token-falso")})
    r = auth.post("/api/integraciones/gmail/desconectar", json={})
    assert r.status_code == 200 and r.json()["estado"] == "desconectada"
    assert "revocada" in r.json()["detalle"] and llamadas[0][1] == {"token": "token-falso"}
    with transaccion() as c:
        assert uno(c, "SELECT estado FROM campanas WHERE id=%s", (cam["id"],))["estado"] == "pausada"
        assert uno(c, "SELECT credenciales_cifradas FROM integraciones WHERE id='gmail'")["credenciales_cifradas"] is None

    def formulario():
        return {
            "id": uuid4().hex,
            "formulario": "contacto",
            "nombre": "Persona",
            "correo": f"web@{uuid4().hex}.example",
            "empresa": "Empresa web",
            "enviadoEn": "2026-11-20T10:00:00-05:00",
            "autorizacion": {"aceptada": False},
        }

    assert auth.post("/api/integraciones/formulario/desconectar", json={}).status_code == 200
    cuerpo = formulario()
    raw, headers = firmar(cuerpo)
    r = auth.post("/api/webhooks/formulario", content=raw, headers=headers)
    assert r.status_code == 503
    with transaccion() as c:
        assert not uno(c, "SELECT id FROM contactos WHERE correo=%s", (cuerpo["correo"],), False)
        assert not uno(c, "SELECT id FROM envios_formulario WHERE id_externo=%s", (cuerpo["id"],), False)
    r = auth.post("/api/integraciones/formulario/conectar", json={})
    assert r.json()["estado"] == "conectada"
    raw, headers = firmar(cuerpo)
    assert auth.post("/api/webhooks/formulario", content=raw, headers=headers).status_code == 200
    with transaccion() as c:
        assert uno(c, "SELECT id FROM contactos WHERE correo=%s", (cuerpo["correo"],), False)


# --- 15: confirmación de baja --------------------------------------------------------


def test_baja_solo_confirma_si_se_registra(auth):
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "Baja " + uuid4().hex[:6])
        m = mensaje_enviado(c, cam, ins[0], correos[0], bogota("2026-10-10"))
    token = firma_baja().dumps(str(m["id"]))
    r = auth.post("/baja/" + token)
    assert r.status_code == 200 and "Baja confirmada" in r.text
    with transaccion() as c:
        assert uno(c, "SELECT id FROM supresiones WHERE correo=%s", (correos[0],), False)
    r = auth.post("/baja/" + token)
    assert r.status_code == 200 and "ya estaba registrada" in r.text
    for invalido in (firma_baja().dumps(str(uuid4())), "no-es-un-token"):
        r = auth.post("/baja/" + invalido)
        assert r.status_code == 400 and "No recibirá" not in r.text and "BAJA" in r.text


# --- 16: respuestas solo en HTML -----------------------------------------------------


def b64(texto):
    return base64.urlsafe_b64encode(texto.encode()).decode()


def test_respuesta_solo_html_de_baja(auth):
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "HTML " + uuid4().hex[:6])
        hilo = "t-" + uuid4().hex
        mensaje_enviado(c, cam, ins[0], correos[0], bogota("2026-10-10"), hilo)
        gm = {
            "id": "html-" + uuid4().hex,
            "threadId": hilo,
            "payload": {
                "mimeType": "text/html",
                "headers": [{"name": "From", "value": "Persona <" + correos[0] + ">"}],
                "body": {"data": b64('<div dir="ltr">BAJA</div><div class="gmail_quote">El lunes escribió: hola</div>')},
            },
        }
        assert registrar(c, gm)
        r = uno(c, "SELECT texto,clasificacion FROM respuestas WHERE gmail_message_id=%s", (gm["id"],))
        assert r["texto"] == "BAJA" and r["clasificacion"] == "baja"
        assert uno(c, "SELECT id FROM supresiones WHERE correo=%s", (correos[0],), False)


# --- 17: recuperación tras una desconexión larga de Gmail ----------------------------


class Ejecutar:
    def __init__(self, fn):
        self.fn = fn

    def execute(self):
        return self.fn()


def no_encontrado():
    raise HttpError(httplib2.Response({"status": 404}), b"{}")


class GmailApiFalsa:
    def __init__(self, mensajes, borrados=()):
        self.mensajes = mensajes
        self.borrados = set(borrados)
        self.consultas = []
        self.lecturas = []

    def users(self):
        return self

    def getProfile(self, userId):
        return Ejecutar(lambda: {"historyId": "999"})

    def history(self):
        class Historial:
            def list(self, **kwargs):
                return Ejecutar(no_encontrado)

        return Historial()

    def messages(self):
        api = self

        class Mensajes:
            def list(self, userId, q, pageToken, maxResults):
                api.consultas.append(q)
                ids = [{"id": i, "threadId": m["threadId"]} for i, m in api.mensajes.items()]
                ids += [{"id": i, "threadId": "otro"} for i in api.borrados]
                return Ejecutar(lambda: {"messages": ids})

            def get(self, userId, id, format, metadataHeaders=None):
                api.lecturas.append((id, format))
                if id in api.borrados:
                    return Ejecutar(no_encontrado)
                return Ejecutar(lambda: api.mensajes[id])

        return Mensajes()


def test_recupera_respuestas_tras_desconexion_larga(auth):
    ahora = datetime.now(timezone.utc)
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "Gmail largo " + uuid4().hex[:6])
        hilo = "t-" + uuid4().hex
        mensaje_enviado(c, cam, ins[0], correos[0], ahora - timedelta(days=14), hilo)
        ultima = ahora - timedelta(days=12)
        c.execute(
            "UPDATE integraciones SET cursor_sincronizacion=%s,ultima_sincronizacion=%s WHERE id='gmail'",
            (Jsonb({"historyId": "1"}), ultima),
        )
        respuesta = {
            "id": "r-" + uuid4().hex,
            "threadId": hilo,
            "internalDate": str(int((ahora - timedelta(days=9)).timestamp() * 1000)),
            "payload": {
                "mimeType": "text/plain",
                "headers": [{"name": "From", "value": correos[0]}],
                "body": {"data": b64("Me interesa conocer el diagnóstico")},
            },
        }
        ajeno = {
            "id": "a-" + uuid4().hex,
            "threadId": "otro-hilo",
            "payload": {"mimeType": "text/plain", "headers": [{"name": "From", "value": "boletin@ejemplo.com"}]},
        }
        api = GmailApiFalsa({respuesta["id"]: respuesta, ajeno["id"]: ajeno}, borrados=["borrado-1"])
        assert sincronizar(c, api)
        assert uno(c, "SELECT id FROM respuestas WHERE gmail_message_id=%s", (respuesta["id"],), False)
        estado = uno(c, "SELECT cursor_sincronizacion,ultima_sincronizacion FROM integraciones WHERE id='gmail'")
    desde = int((ultima - timedelta(hours=2)).timestamp())
    assert api.consultas == [f"after:{desde} -in:sent -in:chats"]  # no solo 7 días
    assert (ajeno["id"], "full") not in api.lecturas  # mensajes ajenos solo con metadatos
    assert estado["cursor_sincronizacion"] == {"historyId": "999"}
    # Una segunda pasada no duplica la respuesta registrada.
    with transaccion() as c:
        c.execute("UPDATE integraciones SET cursor_sincronizacion=%s WHERE id='gmail'", (Jsonb({"historyId": "1"}),))
        api2 = GmailApiFalsa({respuesta["id"]: respuesta})
        assert sincronizar(c, api2)
        assert (respuesta["id"], "full") not in api2.lecturas
        assert uno(c, "SELECT count(*) AS n FROM respuestas WHERE gmail_message_id=%s", (respuesta["id"],))["n"] == 1
        c.execute("UPDATE integraciones SET cursor_sincronizacion=NULL,ultima_sincronizacion=NULL WHERE id='gmail'")


# --- 18: informe semanal con el proveedor activo -------------------------------------


def test_informe_semanal_con_cada_proveedor(auth, monkeypatch):
    from app.agentes.ejecutor import ClaudeFalso, ejecutar
    from app.agentes.modelos import Informe
    from app.tareas.programador import programar as programar_tareas

    cfg = config()
    lunes = [bogota("2027-01-04", "07:30"), bogota("2027-01-11", "07:30")]
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "gemini_api_key", SecretStr("prueba-no-real"))
    with transaccion() as c:
        c.execute("UPDATE integraciones SET estado='desconectada' WHERE id='claude'")
        programar_tareas(c, lunes[0])
        assert uno(c, "SELECT id FROM tareas WHERE clave_unica='informe:2027-01-04'", (), False)
    salida = {"recomendaciones": [{"texto": "Probar otro asunto", "sustento": {"enviados": 40}}]}
    assert ejecutar("analista", {"metricas": []}, Informe, ClaudeFalso(salida)).recomendaciones
    with transaccion() as c:
        assert uno(c, "SELECT categoria FROM costos ORDER BY creado_en DESC LIMIT 1")["categoria"] == "otro"
    monkeypatch.setattr(cfg, "agente_proveedor", "claude")
    monkeypatch.setattr(cfg, "anthropic_api_key", SecretStr("prueba-no-real"))
    monkeypatch.setattr(cfg, "claude_precios_json", json.dumps({cfg.claude_modelo_mayor: {"entrada": 1, "salida": 2}}))
    with transaccion() as c:
        programar_tareas(c, lunes[1])
        assert not uno(c, "SELECT id FROM tareas WHERE clave_unica='informe:2027-01-11'", (), False)
        c.execute("UPDATE integraciones SET estado='conectada',credenciales_cifradas=%s WHERE id='claude'", (cifrar("x"),))
        programar_tareas(c, lunes[1])
        assert uno(c, "SELECT id FROM tareas WHERE clave_unica='informe:2027-01-11'", (), False)
        c.execute("UPDATE integraciones SET estado='desconectada',credenciales_cifradas=NULL WHERE id='claude'")
    assert ejecutar("analista", {"metricas": []}, Informe, ClaudeFalso(salida)).recomendaciones
    with transaccion() as c:
        assert uno(c, "SELECT categoria FROM costos ORDER BY creado_en DESC LIMIT 1")["categoria"] == "api_claude"


# --- 20 y 21: rendimiento ------------------------------------------------------------


def test_listados_con_consultas_constantes(auth, monkeypatch):
    import app.api.lecturas as lecturas

    contador = {"n": 0}
    original = lecturas.todos

    def contar(*args, **kwargs):
        contador["n"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(lecturas, "todos", contar)

    def consultas(recurso):
        contador["n"] = 0
        with transaccion() as c:
            lecturas.lista(c, recurso)
        return contador["n"]

    def cargar(n):
        with transaccion("admin") as c:
            for i in range(n):
                e = insertar(c, "empresas", {"razon_social": f"Carga {uuid4().hex}", "municipio": "Yumbo"})
                insertar(c, "contactos", {"empresa_id": e["id"], "nombre": f"Carga {i}", "origen": "manual"})
                insertar(c, "segmentos", {"nombre": f"Carga {i}", "propuesto_por": "admin"})

    cargar(1)
    antes = {r: consultas(r) for r in ("empresas", "contactos", "segmentos", "campanas", "propuestas")}
    cargar(15)
    despues = {r: consultas(r) for r in antes}
    assert antes == despues


def test_listados_paginados_opcionales(auth):
    with transaccion("admin") as c:
        for i in range(3):
            insertar(c, "empresas", {"razon_social": f"Página {uuid4().hex}", "municipio": "Cali"})
    completo = auth.get("/api/empresas").json()
    r = auth.get("/api/empresas?limite=2&desplazamiento=1")
    assert r.status_code == 200 and int(r.headers["x-total-count"]) == len(completo)
    assert [x["id"] for x in r.json()] == [x["id"] for x in completo[1:3]]
    assert auth.get("/api/empresas?limite=x").status_code == 400
    # Recorrer todas las páginas devuelve cada fila exactamente una vez, aun con
    # fechas de creación repetidas (desempate estable por id).
    with transaccion("admin") as c:
        for i in range(4):
            insertar(c, "segmentos", {"nombre": f"Mismo instante {i}", "reglas": Jsonb([]),
                                      "justificacion": "prueba", "propuesto_por": "admin"})
    todos_ids = [x["id"] for x in auth.get("/api/segmentos").json()]
    vistos: list[str] = []
    for desde in range(0, len(todos_ids), 2):
        r = auth.get(f"/api/segmentos?limite=2&desplazamiento={desde}")
        assert int(r.headers["x-total-count"]) == len(todos_ids)
        vistos += [x["id"] for x in r.json()]
    assert sorted(vistos) == sorted(todos_ids)


def test_puntajes_diarios_en_un_lote(auth):
    from app.dominio.calidad import calcular_lote
    from app.tareas.programador import programar as programar_tareas

    with transaccion() as c:
        insertar(c, "empresas", {"razon_social": f"Puntaje {uuid4().hex}", "municipio": "Cali"})
        antes = uno(c, "SELECT count(*) AS n FROM tareas WHERE tipo='calcular_puntaje'")["n"]
        for _ in range(3):
            programar_tareas(c, bogota("2027-02-02", "07:00"))
        assert uno(c, "SELECT count(*) AS n FROM tareas WHERE tipo='calcular_puntaje'")["n"] == antes
        tarea = uno(c, "SELECT id FROM tareas WHERE clave_unica='puntajes:2027-02-02'")
        empresas = uno(c, "SELECT count(*) AS n FROM empresas WHERE fusionada_en_id IS NULL")["n"]
    calcular_lote(str(tarea["id"]))
    with transaccion() as c:
        sin_puntaje = uno(
            c,
            "SELECT count(*) AS n FROM empresas e WHERE fusionada_en_id IS NULL AND NOT EXISTS (SELECT 1 FROM puntajes p WHERE p.empresa_id=e.id)",
        )["n"]
        filas = uno(c, "SELECT count(*) AS n FROM puntajes")["n"]
        otra = uno(c, "INSERT INTO tareas(tipo,datos) VALUES('calcular_puntajes','{}') RETURNING id")
    assert empresas > 0 and sin_puntaje == 0
    calcular_lote(str(otra["id"]))
    with transaccion() as c:
        assert uno(c, "SELECT count(*) AS n FROM puntajes")["n"] == filas  # sin filas repetidas


# --- Extras ---------------------------------------------------------------------------


def test_reanudar_conserva_espera_del_seguimiento(auth):
    from app.motor.pausas import proximo_al_reanudar

    enviado = bogota("2026-10-15", "10:00")  # jueves
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "Reanudar " + uuid4().hex[:6], contactos=1)
        mensaje_enviado(c, cam, ins[0], correos[0], enviado)
        c.execute("UPDATE inscripciones SET paso_actual=2 WHERE id=%s", (ins[0]["id"],))
        fila = uno(c, "SELECT * FROM inscripciones WHERE id=%s", (ins[0]["id"],))
        sec = {"ventanaEnvio": VENTANA, "pasos": [{"orden": 1, "esperaDiasHabiles": 0}, {"orden": 2, "esperaDiasHabiles": 3}]}
        proximo = proximo_al_reanudar(c, fila, sec, VENTANA, bogota("2026-10-16", "09:00"))
    # Tres días hábiles después del jueves 15 (viernes, lunes, martes), no de inmediato.
    assert proximo.astimezone(BOGOTA).date().isoformat() == "2026-10-20"


def test_oauth_cualquier_dominio_verificado_solicita(client):
    """Ya no se filtra por dominio: cualquier correo verificado puede pedir acceso."""
    from app.seguridad.auth import resolver_identidad

    correo = f"persona-{uuid4().hex[:8]}@otro-dominio.example"
    r = resolver_identidad(correo, "Persona Externa", "sub-1")
    assert r.headers["location"].endswith("/login?solicitud=enviada")
    assert "nexus_sesion" not in r.headers.get("set-cookie", "")


def test_gmail_callback_vuelve_a_configuracion(client):
    r = client.get("/api/integraciones/gmail/callback?error=access_denied")
    assert r.status_code == 302 and r.headers["location"].endswith("/configuracion?gmail=cancelado")
    r = client.get("/api/integraciones/gmail/callback?state=falso&code=x")
    assert r.headers["location"].endswith("/configuracion?gmail=solicitud")


def test_duplicados_fusion_y_descarte(auth):
    with transaccion("admin") as c:
        a = insertar(c, "empresas", {"razon_social": "Fusión Ejemplo SAS", "municipio": "Buga"})
        b = insertar(c, "empresas", {"razon_social": "Fusion Ejemplo", "municipio": "Buga"})
        d = insertar(c, "empresas", {"razon_social": "Fusión Ejemplo Ltda", "municipio": "Buga"})
        from app.dominio.calidad import duplicados

        duplicados(c, str(b["id"]))
        pares = todos(
            c,
            "SELECT * FROM posibles_duplicados WHERE estado='pendiente' AND %s IN (principal_id,duplicado_id)",
            (b["id"],),
        )
    assert len(pares) == 2
    par = next(p for p in pares if {p["principal_id"], p["duplicado_id"]} == {a["id"], b["id"]})
    assert auth.post(f"/api/duplicados/{par['id']}/resolver", json={}).status_code == 200
    fusionada = str(par["duplicado_id"])
    listado = auth.get("/api/duplicados").json()
    # Ningún par pendiente vuelve a mostrar la ficha que ya se fusionó.
    assert all(fusionada not in (x["principalId"], x["duplicadoId"]) for x in listado)
    assert str(par["id"]) not in {x["id"] for x in listado}
    with transaccion() as c:
        assert uno(c, "SELECT fusionada_en_id FROM empresas WHERE id=%s", (fusionada,))["fusionada_en_id"]
        assert uno(c, "SELECT id FROM empresas WHERE id=%s", (d["id"],))
    with transaccion() as c:
        e = insertar(c, "empresas", {"razon_social": "Descarte Ejemplo", "municipio": "Tuluá"})
        f = insertar(c, "empresas", {"razon_social": "Descarte Ejemplos", "municipio": "Tuluá"})
        duplicados(c, str(f["id"]))
        par2 = uno(c, "SELECT id FROM posibles_duplicados WHERE %s IN (principal_id,duplicado_id)", (e["id"],))
    assert auth.post(f"/api/duplicados/{par2['id']}/descartar", json={}).status_code == 200
    assert auth.post(f"/api/duplicados/{par2['id']}/descartar", json={}).status_code == 400


def test_responder_exige_gmail_conectado(auth):
    with transaccion("admin") as c:
        admin = admin_id(c)
        aislar_campanas(c)
        cam, ids, correos, ins = crear_campana(c, admin, "Responder " + uuid4().hex[:6], contactos=1)
        hilo = "t-" + uuid4().hex
        mensaje_enviado(c, cam, ins[0], correos[0], bogota("2026-10-12"), hilo)
        r = insertar(
            c,
            "respuestas",
            {
                "contacto_id": ids[0],
                "campana_id": cam["id"],
                "gmail_message_id": "g-" + uuid4().hex,
                "gmail_thread_id": hilo,
                "remitente": correos[0],
                "recibida_en": bogota("2026-10-13"),
                "texto": "Me interesa",
            },
        )
        actualizar(c, "integraciones", "gmail", {"estado": "desconectada", "credenciales_cifradas": None})
    ruta = f"/api/respuestas/{r['id']}/enviar-respuesta"
    res = auth.post(ruta, json={"texto": "Gracias, le escribimos pronto."})
    assert res.status_code == 400 and "Gmail" in res.json()["message"]
    with transaccion() as c:
        assert uno(c, "SELECT estado FROM respuestas WHERE id=%s", (r["id"],))["estado"] == "pendiente"
        actualizar(c, "integraciones", "gmail", {"estado": "conectada", "credenciales_cifradas": cifrar("x")})
    assert auth.post(ruta, json={"texto": "Gracias, le escribimos pronto."}).status_code == 200
    with transaccion() as c:
        assert uno(
            c, "SELECT count(*) AS n FROM mensajes WHERE en_respuesta_a=%s AND tipo='respuesta_manual'", (r["id"],)
        )["n"] == 1
        # No dejar envíos programados con la hora real: otras pruebas usan relojes falsos.
        c.execute("UPDATE mensajes SET estado='cancelado' WHERE en_respuesta_a=%s", (r["id"],))
        actualizar(c, "integraciones", "gmail", {"estado": "desconectada", "credenciales_cifradas": None})


def test_verificar_correo_a_pedido_y_pendientes_al_conectar_hunter(auth, monkeypatch):
    """Los correos guardados sin verificador no quedan «desconocido» para siempre."""
    from app.integraciones import hunter
    import app.integraciones.conexiones as conexiones

    with transaccion("admin") as c:
        admin = admin_id(c)
        c.execute("SELECT set_config('nexus.administrador_id',%s,true)", (admin,))
        e = insertar(c, "empresas", {"razon_social": "Verificación " + uuid4().hex[:6], "municipio": "Palmira"})
        ids = []
        for i in range(2):
            ct = insertar(
                c,
                "contactos",
                {"empresa_id": e["id"], "nombre": f"Pendiente {i}", "correo": f"p{i}@{uuid4().hex}.example", "origen": "manual"},
            )
            insertar(
                c,
                "bases_legales",
                {"contacto_id": ct["id"], "tipo": "autorizacion_registrada", "texto": "Consentimiento ficticio", "prueba": "Fixture", "aprobada_por": admin, "aprobada_en": datetime.now(timezone.utc)},
            )
            ids.append(str(ct["id"]))
        sin_correo = insertar(c, "contactos", {"empresa_id": e["id"], "nombre": "Sin correo", "origen": "manual"})
        assert uno(c, "SELECT motivo_no_elegible(%s) AS m", (ids[0],))["m"] == "Se requiere un correo válido"

    def tareas(ident):
        with transaccion() as c:
            return uno(c, "SELECT count(*) AS n FROM tareas WHERE tipo='verificar_correo' AND estado='pendiente' AND datos->>'contactoId'=%s", (ident,))["n"]

    # A pedido, desde la ficha del contacto.
    antes = tareas(ids[0])
    r = auth.post(f"/api/contactos/{ids[0]}/verificar-correo", json={})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert tareas(ids[0]) == antes + 1
    r = auth.post(f"/api/contactos/{sin_correo['id']}/verificar-correo", json={})
    assert r.status_code == 400 and r.json()["message"] == "El contacto no tiene correo"
    assert auth.post(f"/api/contactos/{uuid4()}/verificar-correo", json={}).status_code == 404

    # Al conectar Hunter se encolan todos los pendientes.
    class Respuesta:
        def raise_for_status(self):
            return None

    monkeypatch.setattr(config(), "hunter_api_key", SecretStr("hunter-de-prueba"))
    monkeypatch.setattr(conexiones.httpx, "get", lambda *a, **k: Respuesta())
    antes = [tareas(i) for i in ids]
    r = auth.post("/api/integraciones/verificador/conectar", json={})
    assert r.status_code == 200 and r.json()["estado"] == "conectada", r.text
    assert "Se verificarán" in r.json()["detalle"]
    assert [tareas(i) for i in ids] == [n + 1 for n in antes]

    # El resultado lo pone el verificador: con «valid» el contacto pasa a ser elegible.
    with transaccion("proceso:verificador") as c:
        hunter.verificar(c, ids[1], hunter.HunterFalso({"status": "valid"}))
    with transaccion() as c:
        assert uno(c, "SELECT estado_correo FROM contactos WHERE id=%s", (ids[1],))["estado_correo"] == "valido"
        assert uno(c, "SELECT motivo_no_elegible(%s) AS m", (ids[1],))["m"] is None
        c.execute("UPDATE tareas SET estado='cancelada', terminada_en=now() WHERE tipo='verificar_correo' AND estado='pendiente'")
    auth.post("/api/integraciones/verificador/desconectar", json={})
