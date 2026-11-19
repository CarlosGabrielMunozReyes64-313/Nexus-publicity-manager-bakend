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
        GeminiReal(httpx.MockTransport(handler)).ejecutar(
            "gemini-3.5-flash-lite", "", "", Descubrimiento, 8192
        )
    assert "secreto-no-filtrar" not in str(exc.value)


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
