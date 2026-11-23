# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)
"""Pedidos de propuestas al Segmentador y al Estratega-redactor, de punta a punta."""

from uuid import uuid4

from psycopg.types.json import Jsonb
from pydantic import SecretStr
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.agentes.ejecutor import ClaudeFalso
from app.agentes.tareas import correr
from app.api import rutas_correo
from app.config import config
from app.db import insertar, transaccion, uno

PRECIOS = '{"claude-sonnet-5-5":{"entrada":2,"salida":10},"claude-haiku-5-5":{"entrada":0.1,"salida":0.5}}'


def proveedor_listo(monkeypatch):
    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "claude")
    monkeypatch.setattr(cfg, "anthropic_api_key", SecretStr("prueba"))
    monkeypatch.setattr(cfg, "claude_modelo_mayor", "claude-sonnet-5-5")
    monkeypatch.setattr(cfg, "claude_precios_json", PRECIOS)


def segmento_con_contactos(elegibles: bool, aprobado: bool = True):
    """Segmento por ids con un contacto (con o sin base legal aprobada)."""
    with transaccion("admin") as c:
        admin = str(
            uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))["id"]
        )
        c.execute("SELECT set_config('nexus.administrador_id',%s,true)", (admin,))
        e = insertar(
            c, "empresas", {"razon_social": f"Prueba {uuid4().hex}", "municipio": "Palmira"}
        )
        ct = insertar(
            c,
            "contactos",
            {
                "empresa_id": e["id"],
                "nombre": "Ficticio",
                "correo": f"persona@{uuid4().hex}.example",
                "estado_correo": "valido",
                "origen": "manual",
            },
        )
        if elegibles:
            insertar(
                c,
                "bases_legales",
                {
                    "contacto_id": ct["id"],
                    "tipo": "autorizacion_registrada",
                    "texto": "Consentimiento ficticio",
                    "prueba": "Fixture de prueba",
                    "aprobada_por": admin,
                    "aprobada_en": "2026-11-01T09:00:00-05:00",
                },
            )
        extra = (
            {
                "estado": "aprobado",
                "aprobado_por": admin,
                "aprobado_en": "2026-11-01T09:00:00-05:00",
            }
            if aprobado
            else {}
        )
        seg = insertar(
            c,
            "segmentos",
            {
                "nombre": f"Segmento {uuid4().hex[:6]}",
                "reglas": Jsonb(
                    [{"campo": "contactoIds", "operador": "es", "valor": str(ct["id"])}]
                ),
                "propuesto_por": "admin",
                **extra,
            },
        )
        return str(seg["id"])


def cancelar_pedidos():
    with transaccion("admin") as c:
        c.execute(
            "UPDATE tareas SET estado='cancelada',terminada_en=now() WHERE estado='pendiente' "
            "AND tipo IN ('proponer_segmentos','proponer_campana')"
        )


def test_pedidos_validan_requisitos_antes_de_gastar(auth, monkeypatch):
    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "claude")
    monkeypatch.setattr(cfg, "anthropic_api_key", SecretStr(""))
    r = auth.post("/api/segmentos/proponer", json={})
    assert r.status_code == 409 and "ANTHROPIC_API_KEY" in r.json()["message"]

    proveedor_listo(monkeypatch)
    sin_aprobar = segmento_con_contactos(elegibles=True, aprobado=False)
    sin_elegibles = segmento_con_contactos(elegibles=False)
    listo = segmento_con_contactos(elegibles=True)
    try:
        r = auth.post("/api/segmentos/proponer", json={})
        assert r.status_code == 200 and r.json()["tareaId"]
        r = auth.post("/api/segmentos/proponer", json={})
        assert r.status_code == 409 and "en curso" in r.json()["message"]

        r = auth.post(f"/api/segmentos/{sin_aprobar}/proponer-campana", json={})
        assert r.status_code == 409 and "El segmento requiere aprobación" in r.json()["message"]
        r = auth.post(f"/api/segmentos/{sin_elegibles}/proponer-campana", json={})
        assert r.status_code == 409 and "contactos elegibles" in r.json()["message"]
        r = auth.post(f"/api/segmentos/{listo}/proponer-campana", json={})
        assert r.status_code == 200, r.text
        r = auth.post(f"/api/segmentos/{listo}/proponer-campana", json={})
        assert r.status_code == 409 and "en preparación" in r.json()["message"]

        pedidos = auth.get("/api/agentes/solicitudes").json()
        tipos = {p["tipo"]: p for p in pedidos}
        assert tipos["proponer_segmentos"]["estado"] == "pendiente"
        assert tipos["proponer_campana"]["segmentoId"] == listo
        assert tipos["proponer_campana"]["segmento"].startswith("Segmento ")
    finally:
        cancelar_pedidos()


def test_los_agentes_crean_segmento_y_campana_aprobables(auth, monkeypatch):
    proveedor_listo(monkeypatch)
    segmento = segmento_con_contactos(elegibles=True)
    try:
        tarea = auth.post("/api/segmentos/proponer", json={}).json()["tareaId"]
        correr(
            "proponer_segmentos",
            {},
            tarea,
            ClaudeFalso(
                {
                    "nombre": "Proveedores de ingenios en Palmira",
                    "justificacion": "Empresas de Palmira que venden a ingenios azucareros.",
                    "reglas": [{"campo": "municipio", "operador": "es", "valor": "Palmira"}],
                }
            ),
        )
        with transaccion("admin") as c:
            seg = uno(
                c,
                "SELECT * FROM segmentos WHERE nombre='Proveedores de ingenios en Palmira' "
                "ORDER BY creado_en DESC LIMIT 1",
            )
            assert seg["estado"] == "propuesto" and seg["propuesto_por"] == "agente"
            prop = uno(c, "SELECT * FROM propuestas WHERE objetivo_segmento_id=%s", (seg["id"],))
            assert prop["tipo"] == "segmento" and prop["estado"] == "pendiente"

        tarea = auth.post(f"/api/segmentos/{segmento}/proponer-campana", json={}).json()["tareaId"]
        cuerpo = (
            "Hola {{nombre}}, le compartimos un diagnóstico de sostenibilidad. {{firma}} {{baja}}"
        )
        correr(
            "proponer_campana",
            {"segmentoId": segmento},
            tarea,
            ClaudeFalso(
                {
                    "nombre": "Diagnóstico para proveedores",
                    "resumen": "Invitación a un diagnóstico gratuito.",
                    "notas": {
                        "general": "Proveedores que deben responder cuestionarios.",
                        "pasos": [{"orden": 1, "proposito": "Presentar", "porQue": "Breve"}],
                    },
                    "secuencia": {
                        "nombre": "Diagnóstico",
                        "objetivo": "Agendar diagnóstico",
                        "llamadaAccion": "Agendar",
                        "metricasExito": ["respuestas"],
                        "ventanaEnvio": {
                            "dias": [1, 2, 3, 4, 5],
                            "desde": "08:00",
                            "hasta": "17:00",
                        },
                        # Ruta pedida por la ruta anterior: 3 correos por defecto.
                        "pasos": [
                            {
                                "orden": i,
                                "asunto": "Diagnóstico de sostenibilidad" if i == 1 else None,
                                "cuerpo": cuerpo,
                                "esperaDiasHabiles": [0, 4, 5][i - 1],
                                "mismoHilo": i > 1,
                                "variables": [],
                            }
                            for i in (1, 2, 3)
                        ],
                    },
                }
            ),
        )
        with transaccion("admin") as c:
            cam = uno(
                c,
                "SELECT * FROM campanas WHERE segmento_id=%s ORDER BY creado_en DESC LIMIT 1",
                (segmento,),
            )
            assert cam["estado"] == "en_revision"
            prop = uno(c, "SELECT * FROM propuestas WHERE objetivo_campana_id=%s", (cam["id"],))
        # La propuesta del agente se puede aprobar sin pasos manuales extra.
        r = auth.post(f"/api/propuestas/{prop['id']}/aprobar", json={})
        assert r.status_code == 200, r.text
        assert auth.get(f"/api/campanas/{cam['id']}").json()["estado"] == "aprobada"
    finally:
        cancelar_pedidos()


def test_fallo_del_estratega_queda_visible(auth, monkeypatch):
    """Sin contactos elegibles al ejecutarse, el pedido falla ya y muestra el motivo."""
    from app.tareas.trabajador import procesar

    proveedor_listo(monkeypatch)
    segmento = segmento_con_contactos(elegibles=True)
    tarea = auth.post(f"/api/segmentos/{segmento}/proponer-campana", json={}).json()["tareaId"]
    with transaccion("admin") as c:
        # Entre el pedido y la ejecución se retiró la base legal del contacto.
        c.execute(
            "UPDATE bases_legales SET vigente=false,revocada_en=now() WHERE contacto_id::text="
            "(SELECT reglas->0->>'valor' FROM segmentos WHERE id=%s)",
            (segmento,),
        )
        t = uno(
            c,
            "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
            "intentos=intentos+1 WHERE id=%s RETURNING *",
            (tarea,),
        )
    procesar(t)
    pedido = next(p for p in auth.get("/api/agentes/solicitudes").json() if p["id"] == tarea)
    assert pedido["estado"] == "fallida" and pedido["intentos"] == 1
    assert pedido["error"] == rutas_correo.SIN_ELEGIBLES
