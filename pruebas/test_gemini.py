# ruff: noqa: F811
import json
from datetime import datetime
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.agentes.descubrimiento import programar_descubrimiento
from app.agentes.ejecutor import ClaudeFalso, costo
from app.agentes.gemini import GeminiReal
from app.agentes.modelos import Descubrimiento
from app.agentes.tareas import correr
from app.config import config
from app.db import encolar, insertar, todos, transaccion, uno
from app.dominio.reglas import BOGOTA


def test_gemini_busqueda_y_json(client, monkeypatch):
    monkeypatch.setattr(config(), "gemini_api_key", SecretStr("prueba-no-real"))
    requests = []

    def handler(req):
        body = json.loads(req.content)
        requests.append(body)
        assert req.headers["x-goog-api-key"] == "prueba-no-real"
        assert "key=" not in str(req.url)
        candidate = {"finishReason": "STOP", "content": {"parts": [{"text": '{"empresas":[]}'}]}}
        if "tools" in body:
            candidate["groundingMetadata"] = {
                "groundingChunks": [{"web": {"uri": "https://empresa.co"}}],
                "webSearchQueries": ["empresas Colombia"],
            }
        return httpx.Response(
            200,
            json={
                "candidates": [candidate],
                "usageMetadata": {
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 20,
                    "thoughtsTokenCount": 5,
                },
            },
        )

    result, usage = GeminiReal(httpx.MockTransport(handler)).ejecutar(
        "gemini-3.5-flash-lite", "Investiga", "Colombia", Descubrimiento, 8192
    )
    assert result.empresas == []
    assert requests[0]["tools"] == [{"google_search": {}}]
    assert "responseJsonSchema" in requests[1]["generationConfig"]
    assert usage == {"input_tokens": 200, "output_tokens": 50, "gemini_search_queries": 1}
    assert costo(usage, {"entrada": 0.30, "salida": 2.50}) == pytest.approx(0.014185)


@pytest.mark.parametrize("mode", ["ungrounded", "truncated", "http"])
def test_gemini_rechaza_sin_fuentes_y_errores(client, monkeypatch, mode):
    monkeypatch.setattr(config(), "gemini_api_key", SecretStr("secreto-no-filtrar"))

    def handler(req):
        if mode == "http":
            return httpx.Response(429, text="secreto-no-filtrar")
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {
                        "finishReason": "MAX_TOKENS" if mode == "truncated" else "STOP",
                        "content": {"parts": []},
                    }
                ]
            },
        )

    with pytest.raises(ValueError) as exc:
        GeminiReal(httpx.MockTransport(handler), dormir=lambda s: None).ejecutar(
            "gemini-3.5-flash-lite", "", "", Descubrimiento, 8192
        )
    assert "secreto-no-filtrar" not in str(exc.value)


def error_google(status, *, cuota=None, valor="15", espera=None, razon=None):
    detalles = []
    if cuota:
        detalles.append(
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [
                    {
                        "quotaMetric": "generativelanguage.googleapis.com/generate_content_requests",
                        "quotaId": cuota,
                        "quotaValue": valor,
                    }
                ],
            }
        )
    if espera:
        detalles.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": espera})
    if razon:
        detalles.append({"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": razon})
    return httpx.Response(
        status,
        json={"error": {"code": status, "message": "secreto-no-filtrar", "details": detalles}},
    )


def respuesta_ok(texto='{"empresas":[]}', fuentes=True):
    candidate = {"finishReason": "STOP", "content": {"parts": [{"text": texto}]}}
    if fuentes:
        candidate["groundingMetadata"] = {
            "groundingChunks": [{"web": {"uri": "https://empresa.co"}}],
            "webSearchQueries": ["empresas Colombia"],
        }
    return httpx.Response(
        200,
        json={
            "candidates": [candidate],
            "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 20},
        },
    )


def test_gemini_limite_por_minuto_espera_y_continua(client, monkeypatch):
    monkeypatch.setattr(config(), "gemini_api_key", SecretStr("prueba"))
    respuestas = [
        error_google(429, cuota="GenerateRequestsPerMinutePerProjectPerModel", espera="7s"),
        respuesta_ok(),
        respuesta_ok(),
    ]
    pausas = []
    result, usage = GeminiReal(
        httpx.MockTransport(lambda req: respuestas.pop(0)), dormir=pausas.append
    ).ejecutar("gemini-3.5-flash-lite", "", "Colombia", Descubrimiento, 8192)
    assert result.empresas == [] and pausas == [7.0] and not respuestas
    assert usage["gemini_search_queries"] == 1


@pytest.mark.parametrize(
    "respuesta,mensaje",
    [
        (
            error_google(429, cuota="GenerateRequestsPerDayPerProjectPerModel-FreeTier"),
            "cuota diaria del plan gratuito",
        ),
        (
            error_google(
                429, cuota="GenerateRequestsPerMinutePerProjectPerModel-FreeTier", valor="0"
            ),
            "no incluye cuota",
        ),
        (
            httpx.Response(
                429,
                json={
                    "error": {
                        "code": 429,
                        "message": "You exceeded your current quota, please check your plan and "
                        "billing details.",
                        "status": "RESOURCE_EXHAUSTED",
                    }
                },
            ),
            "no está disponible en el plan gratuito",
        ),
        (error_google(400, razon="API_KEY_INVALID"), "GEMINI_API_KEY no es válida"),
        (error_google(404), "GEMINI_MODELO no existe"),
    ],
)
def test_gemini_errores_definitivos_no_reintentan(client, monkeypatch, respuesta, mensaje):
    from app.agentes.errores import FalloProveedor

    monkeypatch.setattr(config(), "gemini_api_key", SecretStr("secreto-no-filtrar"))
    llamadas = []

    def handler(req):
        llamadas.append(req)
        return respuesta

    with pytest.raises(FalloProveedor) as exc:
        GeminiReal(httpx.MockTransport(handler), dormir=lambda s: None).ejecutar(
            "gemini-3.5-flash-lite", "", "", Descubrimiento, 8192
        )
    assert mensaje in str(exc.value) and "secreto-no-filtrar" not in str(exc.value)
    assert not exc.value.reintentable and exc.value.costo_conocido and len(llamadas) == 1


def test_busqueda_reintenta_tras_429_sin_chocar_con_el_tope(auth, monkeypatch):
    """Un 429 no consume la reserva: el reintento funciona y la pantalla explica la espera."""
    from app.agentes.errores import FalloProveedor
    from app.agentes.gemini import CUOTA_DIARIA_GRATIS, LIMITE_MINUTO
    from app.tareas.trabajador import procesar

    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "gemini_api_key", SecretStr("prueba"))
    salidas: list = []

    class Proveedor:
        def ejecutar(self, modelo, prompt, entrada, schema, max_tokens):
            salida = salidas.pop(0)
            if isinstance(salida, Exception):
                raise salida
            return schema.model_validate(salida), {"input_tokens": 1000, "output_tokens": 100}

    monkeypatch.setattr("app.agentes.gemini.GeminiReal", Proveedor)

    def nueva():
        with transaccion() as c:
            b = insertar(c, "busquedas_descubrimiento", {"consulta": "Empresas de Palmira"})
            clave = str(uuid4())
            encolar(c, "descubrir_empresas", {"busquedaId": str(b["id"])}, clave)
            return str(b["id"]), str(
                uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))["id"]
            )

    def intentar(tarea):
        with transaccion() as c:
            t = uno(
                c,
                "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
                "intentos=intentos+1 WHERE id=%s RETURNING *",
                (tarea,),
            )
        procesar(t)
        with transaccion() as c:
            return uno(c, "SELECT * FROM tareas WHERE id=%s", (tarea,))

    busqueda, tarea = nueva()
    salidas.append(FalloProveedor(LIMITE_MINUTO, {}, reintentable=True))
    assert intentar(tarea)["estado"] == "pendiente"
    with transaccion() as c:
        run = uno(c, "SELECT * FROM ejecuciones_agente WHERE tarea_id=%s", (tarea,))
        assert run["estado"] == "error" and float(run["costo_usd"]) == 0
        b = uno(c, "SELECT * FROM busquedas_descubrimiento WHERE id=%s", (busqueda,))
        assert b["estado"] == "en_cola" and b["error"].startswith("Intento 1 de ")
        assert "límite de solicitudes por minuto" in b["error"] and "Nuevo intento" in b["error"]
    # Antes este reintento fallaba con «Tope por tarea alcanzado» por la reserva retenida.
    salidas.append({"empresas": []})
    assert intentar(tarea)["estado"] == "completada"
    with transaccion() as c:
        b = uno(c, "SELECT * FROM busquedas_descubrimiento WHERE id=%s", (busqueda,))
        assert b["estado"] == "terminada" and b["error"] is None

    # Una cuota diaria agotada no se arregla en 1 o 5 minutos: falla ya, con el motivo.
    busqueda, tarea = nueva()
    salidas.append(FalloProveedor(CUOTA_DIARIA_GRATIS, {}, reintentable=False))
    t = intentar(tarea)
    assert t["estado"] == "fallida" and t["intentos"] == 1
    with transaccion() as c:
        b = uno(c, "SELECT * FROM busquedas_descubrimiento WHERE id=%s", (busqueda,))
        assert b["estado"] == "error" and "cuota diaria del plan gratuito" in b["error"]


def test_programacion_opcional_idempotente_y_nacional(auth, monkeypatch):
    cfg = config()
    now = datetime(2034, 2, 6, 9, tzinfo=BOGOTA)
    with transaccion() as c:
        programar_descubrimiento(c, now)
        assert not todos(
            c, "SELECT * FROM tareas WHERE clave_unica LIKE 'descubrimiento-auto:2034-02-06:%%'"
        )
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "gemini_api_key", SecretStr("prueba"))
    monkeypatch.setattr(cfg, "descubrimiento_automatico", True)
    with transaccion() as c:
        programar_descubrimiento(c, now)
        programar_descubrimiento(c, now)
        tasks = todos(
            c, "SELECT * FROM tareas WHERE clave_unica LIKE 'descubrimiento-auto:2034-02-06:%%'"
        )
        assert len(tasks) == 4
        assert sum(t["datos"]["limite"] for t in tasks) == 20
        assert len({t["datos"]["busquedaId"] for t in tasks}) == 4


def test_auto_importa_evidencia_sin_aprobar_y_deduplica(auth, monkeypatch):
    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "descubrimiento_limite_diario", 1)
    suffix = uuid4().hex[:10]
    host = f"empresa-{suffix}.co"
    name = f"Empresa {suffix}"
    monkeypatch.setattr(
        "app.agentes.tareas.lector_web.leer",
        lambda url: (
            f"{name} Colombia Cali ventas@{host} Otra Perú ventas@extranjera.co Excede límite ventas@limite.co"
        ),
    )

    def found(domain, nombre, pais="Colombia", email=None):
        values = {
            "razonSocial": nombre,
            "pais": pais,
            "municipio": "Cali",
            "correoCorporativo": email or "ventas@" + domain,
        }
        return {
            "razonSocial": nombre,
            "sitioWeb": "https://" + domain,
            "sector": None,
            "municipio": "Cali",
            "correoCorporativo": values["correoCorporativo"],
            "datos": [
                {"campo": k, "valor": v, "url": "https://" + domain + "/contacto", "cita": v}
                for k, v in values.items()
            ],
        }

    def task(companies):
        with transaccion() as c:
            b = insertar(c, "busquedas_descubrimiento", {"consulta": "Colombia"})
            payload = {"busquedaId": str(b["id"]), "autoImportar": True, "limite": 5}
            key = str(uuid4())
            encolar(c, "descubrir_empresas", payload, key)
            t = uno(c, "SELECT * FROM tareas WHERE clave_unica=%s", (key,))
        correr("descubrir_empresas", payload, str(t["id"]), ClaudeFalso({"empresas": companies}))
        return payload, str(t["id"])

    payload, ident = task(
        [
            found(host, name),
            found("extranjera.co", "Otra", "Perú"),
            found("limite.co", "Excede límite"),
        ]
    )
    correr("descubrir_empresas", payload, ident, ClaudeFalso({"empresas": []}))
    monkeypatch.setattr(cfg, "descubrimiento_limite_diario", 20)
    task([found(host, name)])
    with transaccion() as c:
        assert uno(c, "SELECT count(*) AS n FROM empresas WHERE dominio=%s", (host,))["n"] == 1
        assert not uno(
            c,
            "SELECT id FROM empresas WHERE dominio IN ('extranjera.co','limite.co')",
            requerido=False,
        )
        ct = uno(c, "SELECT * FROM contactos WHERE correo=%s", ("ventas@" + host,))
        assert ct["etapa"] == "identificado"
        b = uno(c, "SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente", (ct["id"],))
        assert b["tipo"] == "pendiente" and b["aprobada_en"] is None
        assert uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (ct["id"],))["motivo"] is not None
        assert (
            uno(
                c,
                "SELECT count(*) AS n FROM evidencias WHERE empresa_id=%s AND estado='verificado'",
                (ct["empresa_id"],),
            )["n"]
            >= 3
        )


def test_cita_no_respalda_valor_inventado():
    from app.agentes.descubrimiento import corroborados

    assert (
        corroborados(
            [
                {
                    "campo": "correoCorporativo",
                    "valor": "ventas@inventado.co",
                    "cita": "Bienvenidos a nuestra empresa",
                    "estado": "verificado",
                }
            ]
        )
        == {}
    )


def test_fuentes_consulta_cada_pagina_una_vez(monkeypatch):
    from app.agentes.modelos import Dato
    from app.agentes.tareas import fuentes

    calls = []

    def leer(url):
        calls.append(url)
        return "Empresa Colombia"

    monkeypatch.setattr("app.agentes.tareas.lector_web.leer", leer)
    result = fuentes(
        [
            Dato(campo="razonSocial", valor="Empresa", cita="Empresa", url="https://empresa.co"),
            Dato(campo="pais", valor="Colombia", cita="Colombia", url="https://empresa.co"),
        ],
        "empresa.co",
    )
    assert len(calls) == 1
    assert all(r["estado"] == "verificado" for r in result)


def test_prueba_de_conexion_avisa_si_falta_la_busqueda(client):
    from app.agentes.gemini import probar_conexion

    def handler(req):
        if "tools" in json.loads(req.content):
            return httpx.Response(
                429,
                json={"error": {"code": 429, "message": "check your plan and billing details"}},
            )
        return respuesta_ok(fuentes=False)

    detalle = probar_conexion("prueba", "gemini-3.5-flash-lite", httpx.MockTransport(handler))
    assert detalle.startswith("Gemini responde, pero la prueba de búsqueda en Google")
    assert "plan gratuito" in detalle
    ok = probar_conexion("prueba", "m", httpx.MockTransport(lambda req: respuesta_ok()))
    assert ok.startswith("Conexión comprobada con Gemini, incluida la búsqueda")
    with pytest.raises(ValueError, match="no existe"):
        probar_conexion("prueba", "m", httpx.MockTransport(lambda req: error_google(404)))
