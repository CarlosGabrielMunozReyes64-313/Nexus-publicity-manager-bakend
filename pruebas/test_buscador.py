# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)
"""Descubrimiento con Tavily: consultas, filtrado de candidatos, errores y flujo completo."""

import json
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.agentes import buscador
from app.agentes.ejecutor import ClaudeFalso
from app.agentes.errores import FalloProveedor
from app.agentes.tareas import correr
from app.config import config
from app.db import encolar, insertar, todos, transaccion, uno


def test_consultas_reparten_tipos_y_municipios():
    criterios = {
        "consulta": "Busca empresas en Colombia de estos tipos: Empaques; Bebidas; Hoteles",
        "municipios": ["Palmira", "Candelaria"],
    }
    assert buscador.consultas(criterios, 4) == [
        "Empaques en Palmira, Colombia sitio web oficial",
        "Bebidas en Candelaria, Colombia sitio web oficial",
        "Hoteles en Palmira, Colombia sitio web oficial",
        "Empaques en Candelaria, Colombia sitio web oficial",
    ]
    assert buscador.consultas({"consulta": "Busca empresas en Colombia: Colegios"}, 4) == [
        "Colegios en Colombia sitio web oficial"
    ]
    auto = buscador.consultas(
        {"consulta": "Encuentra hasta 5 empresas en Antioquia, Colombia, de cualquier sector."},
        4,
    )
    assert len(auto) == 2 and all("Antioquia" in q for q in auto)


def test_candidatos_descartan_directorios_y_suman_contacto(monkeypatch):
    monkeypatch.setattr(config(), "tavily_api_key", SecretStr("tvly-prueba"))
    monkeypatch.setattr(config(), "tavily_consultas_max", 2)
    peticiones = []

    def handler(req):
        peticiones.append(req)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"url": "https://www.facebook.com/empaques", "score": 0.99, "content": "x"},
                    {"url": "https://paginasamarillas.com.co/e", "score": 0.9, "content": "x"},
                    {"url": "https://empaques.co/", "score": 0.5, "raw_content": "Inicio"},
                    {
                        "url": "https://www.empaques.co/nosotros",
                        "score": 0.8,
                        "raw_content": "Empaques del Valle S.A.S. Palmira",
                    },
                    {"url": "https://otra.co/", "score": 0.7, "content": "Otra empresa"},
                ]
            },
        )

    def leer(url):
        if url == "https://www.empaques.co/contacto":
            return "Escríbanos a ventas@empaques.co"
        raise FileNotFoundError("Fuente no encontrada")

    monkeypatch.setattr("app.agentes.buscador.lector_web.leer", leer)
    paginas, hechas = buscador.candidatos(
        {"consulta": "Busca empresas en Colombia: Empaques", "municipios": ["Palmira", "Cali"]},
        httpx.MockTransport(handler),
    )
    assert hechas == 2 and len(peticiones) == 2
    cuerpo = json.loads(peticiones[0].content)
    assert peticiones[0].headers["authorization"] == "Bearer tvly-prueba"
    assert cuerpo["country"] == "colombia" and "facebook.com" in cuerpo["exclude_domains"]
    urls = [p["url"] for p in paginas]
    # Un solo resultado por dominio (el de mayor puntaje), sin redes ni directorios.
    assert urls[:2] == ["https://www.empaques.co/nosotros", "https://otra.co/"]
    assert "https://www.empaques.co/contacto" in urls and len(urls) == 3
    assert paginas[0]["texto"] == "Empaques del Valle S.A.S. Palmira"


@pytest.mark.parametrize(
    "status,texto,reintentable",
    [
        (401, "TAVILY_API_KEY no es válida", False),
        (432, "se agotaron los créditos", False),
        (429, "demasiadas solicitudes", True),
        (502, "no está disponible temporalmente", True),
    ],
)
def test_errores_de_tavily(status, texto, reintentable):
    transport = httpx.MockTransport(lambda req: httpx.Response(status, json={"detail": "x"}))
    with httpx.Client(transport=transport) as cliente, pytest.raises(FalloProveedor) as exc:
        buscador.buscar(cliente, "tvly-secreta", "empresas")
    assert texto in str(exc.value) and exc.value.reintentable is reintentable
    assert "tvly-secreta" not in str(exc.value)


class Capturador(ClaudeFalso):
    def ejecutar(self, modelo, prompt, entrada, schema, max_tokens):
        self.entrada = entrada
        self.schema = schema
        return super().ejecutar(modelo, prompt, entrada, schema, max_tokens)


def test_descubrimiento_con_tavily_de_punta_a_punta(auth, monkeypatch):
    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "tavily_api_key", SecretStr("tvly-prueba"))
    monkeypatch.setattr(cfg, "buscador_descubrimiento", "auto")
    sufijo = uuid4().hex[:8]
    dominio = f"empaques-{sufijo}.co"
    nombre = f"Empaques {sufijo} S.A.S."
    pagina = f"{nombre} Fabricamos empaques en Palmira, Colombia. ventas@{dominio}"
    monkeypatch.setattr(
        "app.agentes.buscador.candidatos",
        lambda criterios: (
            [{"url": f"https://{dominio}/", "titulo": nombre, "texto": pagina}],
            3,
        ),
    )
    monkeypatch.setattr("app.agentes.tareas.lector_web.leer", lambda url: pagina)
    valores = {
        "razonSocial": nombre,
        "municipio": "Palmira",
        "correoCorporativo": f"ventas@{dominio}",
        "pais": "Colombia",
    }
    proveedor = Capturador(
        {
            "empresas": [
                {
                    "razonSocial": nombre,
                    "sitioWeb": f"https://{dominio}/",
                    "sector": None,
                    "municipio": "Palmira",
                    "correoCorporativo": f"ventas@{dominio}",
                    "datos": [
                        {"campo": k, "valor": v, "url": f"https://{dominio}/", "cita": v}
                        for k, v in valores.items()
                    ],
                }
            ]
        }
    )
    with transaccion() as c:
        b = insertar(
            c,
            "busquedas_descubrimiento",
            {"consulta": "Busca empresas en Colombia: Empaques", "municipios": ["Palmira"]},
        )
        clave = str(uuid4())
        encolar(c, "descubrir_empresas", {"busquedaId": str(b["id"])}, clave)
        tarea = str(uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))["id"])
    correr("descubrir_empresas", {"busquedaId": str(b["id"])}, tarea, proveedor)
    # El agente recibe las páginas con instrucciones fijas y no busca por su cuenta.
    assert proveedor.schema.__name__ == "DescubrimientoEnPaginas"
    assert proveedor.entrada.startswith(buscador.INSTRUCCIONES)
    assert dominio in proveedor.entrada
    with transaccion() as c:
        r = uno(c, "SELECT * FROM resultados_descubrimiento WHERE busqueda_id=%s", (b["id"],))
        assert r["razon_social"] == nombre and r["correo_corporativo"] == f"ventas@{dominio}"
        estados = {
            e["campo"]: e["estado"]
            for e in todos(c, "SELECT * FROM evidencias WHERE resultado_id=%s", (r["id"],))
        }
        assert estados["razonSocial"] == "verificado"
        busqueda = uno(c, "SELECT * FROM busquedas_descubrimiento WHERE id=%s", (b["id"],))
        assert busqueda["estado"] == "terminada" and busqueda["error"] is None


def test_sin_paginas_termina_sin_gastar_el_modelo(auth, monkeypatch):
    monkeypatch.setattr(config(), "tavily_api_key", SecretStr("tvly-prueba"))
    monkeypatch.setattr("app.agentes.buscador.candidatos", lambda criterios: ([], 2))
    with transaccion() as c:
        b = insertar(c, "busquedas_descubrimiento", {"consulta": "Busca empresas en Colombia: X"})
        clave = str(uuid4())
        encolar(c, "descubrir_empresas", {"busquedaId": str(b["id"])}, clave)
        tarea = str(uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))["id"])
    correr("descubrir_empresas", {"busquedaId": str(b["id"])}, tarea, ClaudeFalso({"empresas": []}))
    with transaccion() as c:
        assert (
            uno(c, "SELECT estado FROM busquedas_descubrimiento WHERE id=%s", (b["id"],))["estado"]
            == "terminada"
        )
        assert not uno(
            c, "SELECT count(*) AS n FROM ejecuciones_agente WHERE tarea_id=%s", (tarea,)
        )["n"]


def test_conectar_prueba_tavily_en_lugar_de_la_busqueda_del_proveedor(monkeypatch):
    from app.integraciones.conexiones import probar_proveedor_ia

    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "gemini_api_key", SecretStr("clave-gemini"))
    monkeypatch.setattr(cfg, "tavily_api_key", SecretStr("tvly-prueba"))
    llamadas = []

    def gemini(clave, modelo, transport=None, buscar=True):
        llamadas.append(buscar)
        return "Conexión comprobada con Gemini"

    monkeypatch.setattr("app.agentes.gemini.probar_conexion", gemini)
    monkeypatch.setattr("app.agentes.buscador.probar", lambda clave: None)
    assert probar_proveedor_ia()[1] == (
        "Conexión comprobada con Gemini; Descubrimiento usará Tavily (búsqueda comprobada)"
    )
    assert llamadas == [False]

    def sin_creditos(clave):
        raise FalloProveedor("Tavily: se agotaron los créditos", reintentable=False)

    monkeypatch.setattr("app.agentes.buscador.probar", sin_creditos)
    assert "pero la prueba de Tavily (Descubrimiento) falló" in probar_proveedor_ia()[1]
