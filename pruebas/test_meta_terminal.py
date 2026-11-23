"""Meta diaria de contactos en Descubrimiento y Terminal del trabajador (migración 0007)."""
# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)

import os
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

import pytest
from pydantic import SecretStr
from test_base import client  # noqa: F401
from test_buscador import Capturador
from test_datos import auth  # noqa: F401

from app.agentes import descubrimiento
from app.agentes.errores import FalloProveedor
from app.agentes.gemini import SATURADO
from app.agentes.tareas import correr
from app.config import config
from app.db import encolar, insertar, todos, transaccion, uno
from app.dominio.reglas import BOGOTA
from app.esquemas.serializar import serializar
from app.tareas import bitacora

BASE = {
    "activa": True,
    "metaDiaria": 20,
    "maxBusquedas": 40,
    "tipos": ["Fabricantes de empaques"],
    "municipios": ["Palmira"],
    "sectores": [],
    "tamano": "",
}


@pytest.fixture
def ia(monkeypatch):
    cfg = config()
    monkeypatch.setattr(cfg, "agente_proveedor", "gemini")
    monkeypatch.setattr(cfg, "gemini_api_key", SecretStr("prueba"))
    monkeypatch.setattr(cfg, "descubrimiento_hora", 0)
    return cfg


@pytest.fixture
def limpiar_meta():
    yield
    with transaccion() as c:
        c.execute(
            "UPDATE tareas SET estado='cancelada',terminada_en=now(),tomada_por=NULL,tomada_en=NULL "
            "WHERE tipo='descubrir_empresas' AND datos->>'meta'='true' "
            "AND estado IN ('pendiente','en_proceso')"
        )
        c.execute(
            "UPDATE configuracion SET valor=valor||'{\"activa\": false}'::jsonb "
            "WHERE clave='descubrimiento'"
        )


def hechos() -> int:
    with transaccion() as c:
        return descubrimiento.contactos_hoy(c)


def programar() -> None:
    with transaccion() as c:
        descubrimiento.programar_meta(c, datetime.now(BOGOTA))


def tareas_meta() -> list[dict]:
    with transaccion() as c:
        return todos(
            c,
            "SELECT t.*, b.consulta, b.municipios FROM tareas t "
            "JOIN busquedas_descubrimiento b ON b.id::text=t.datos->>'busquedaId' "
            "WHERE t.tipo='descubrir_empresas' AND t.datos->>'meta'='true' "
            "AND t.datos->>'fecha'=%s ORDER BY t.creada_en",
            (datetime.now(BOGOTA).date().isoformat(),),
        )


def terminar(tarea_id, estado: str = "completada") -> None:
    with transaccion() as c:
        c.execute(
            "UPDATE tareas SET estado=%s,terminada_en=now(),tomada_por=NULL,tomada_en=NULL,"
            "intentos=greatest(intentos,1) WHERE id=%s",
            (estado, tarea_id),
        )


def test_meta_se_guarda_validada_y_reporta_el_dia(auth, limpiar_meta):
    r = auth.get("/api/descubrimiento/meta").json()
    assert r["activa"] is False and r["metaDiaria"] >= 1 and r["estado"].startswith("Apagada")
    assert {"contactos", "busquedas", "enCurso"} <= set(r["hoy"])
    assert r["creditosPorBusqueda"] == config().tavily_consultas_max
    for cambio, texto in (
        ({"tipos": []}, "al menos un tipo"),
        ({"metaDiaria": 0}, "metaDiaria"),
        ({"metaDiaria": True}, "metaDiaria"),
        ({"maxBusquedas": 501}, "maxBusquedas"),
        ({"tamano": "enorme"}, "Tamaño"),
        ({"activa": "sí"}, "encendida"),
        ({"municipios": [""]}, "municipios"),
    ):
        respuesta = auth.put("/api/descubrimiento/meta", json={**BASE, **cambio})
        assert respuesta.status_code == 400 and texto in respuesta.json()["message"]
    ok = auth.put(
        "/api/descubrimiento/meta",
        json={**BASE, "tipos": ["Fabricantes de empaques", "fabricantes  de empaques", "Hoteles"]},
    )
    assert ok.json() == {"ok": True}
    r = auth.get("/api/descubrimiento/meta").json()
    assert r["activa"] is True and r["tipos"] == ["Fabricantes de empaques", "Hoteles"]
    assert r["combinaciones"] == 2
    with transaccion() as c:
        assert (
            uno(
                c,
                "SELECT count(*) AS n FROM registro_auditoria WHERE accion='meta_descubrimiento_encendida'",
            )["n"]
            >= 1
        )


def test_meta_busca_un_tipo_y_municipio_a_la_vez_hasta_el_tope(auth, ia, limpiar_meta):
    meta = {
        **BASE,
        "metaDiaria": hechos() + 5,
        "maxBusquedas": len(tareas_meta()) + 3,
        "tipos": ["Fabricantes de empaques", "Hoteles"],
        "municipios": ["Palmira", "Candelaria"],
    }
    assert auth.put("/api/descubrimiento/meta", json=meta).status_code == 200
    antes = len(tareas_meta())
    programar()
    programar()  # Una sola búsqueda en curso a la vez.
    nuevas = tareas_meta()[antes:]
    assert len(nuevas) == 1
    primera = nuevas[0]
    assert primera["consulta"].startswith("Busca empresas en Colombia: ")
    assert len(primera["municipios"]) == 1 and primera["prioridad"] == 8
    estado = auth.get("/api/descubrimiento/meta").json()
    assert estado["hoy"]["enCurso"] is True and estado["estado"].startswith("Buscando «")
    terminar(primera["id"])
    programar()
    segunda = tareas_meta()[antes + 1]
    assert (segunda["consulta"], segunda["municipios"]) != (
        primera["consulta"],
        primera["municipios"],
    )
    terminar(segunda["id"])
    programar()
    terminar(tareas_meta()[antes + 2]["id"])
    # Tope de búsquedas del día: no lanza más y lo dice.
    programar()
    assert len(tareas_meta()) == antes + 3
    assert "Tope de" in auth.get("/api/descubrimiento/meta").json()["estado"]
    with transaccion() as c:
        lineas = [
            e["mensaje"]
            for e in todos(c, "SELECT mensaje FROM eventos_trabajador ORDER BY id DESC LIMIT 10")
        ]
    assert any(x.startswith("Meta diaria: ") and "Búsqueda " in x for x in lineas)


def test_meta_se_detiene_al_cumplirse_y_tras_tres_fallos(auth, ia, limpiar_meta):
    assert (
        auth.put(
            "/api/descubrimiento/meta",
            json={**BASE, "metaDiaria": max(1, hechos()), "maxBusquedas": 500},
        ).status_code
        == 200
    )
    if hechos() == 0:
        # Sin contactos de hoy todavía: se crea uno de Descubrimiento para cumplir la meta.
        with transaccion() as c:
            e = insertar(
                c, "empresas", {"razon_social": "Meta " + uuid4().hex[:6], "municipio": "Palmira"}
            )
            insertar(
                c,
                "contactos",
                {
                    "empresa_id": e["id"],
                    "nombre": "Contacto corporativo",
                    "correo": f"hola@{uuid4().hex[:8]}.example",
                    "origen": "descubrimiento",
                },
            )
    antes = len(tareas_meta())
    programar()
    assert len(tareas_meta()) == antes
    assert auth.get("/api/descubrimiento/meta").json()["estado"].startswith("Meta cumplida")

    # Tres fallos seguidos: pausa en lugar de gastar el tope en errores.
    assert (
        auth.put(
            "/api/descubrimiento/meta",
            json={**BASE, "metaDiaria": hechos() + 5, "maxBusquedas": 500},
        ).status_code
        == 200
    )
    for _ in range(3):
        programar()
        ultima = tareas_meta()[-1]
        with transaccion() as c:
            c.execute(
                "UPDATE tareas SET ultimo_error='Gemini: la clave GEMINI_API_KEY no es válida (400)' "
                "WHERE id=%s",
                (ultima["id"],),
            )
        terminar(ultima["id"], "fallida")
    total = len(tareas_meta())
    programar()
    assert len(tareas_meta()) == total
    assert (
        "En pausa: las últimas 3 búsquedas fallaron"
        in (auth.get("/api/descubrimiento/meta").json()["estado"])
    )


def test_meta_acepta_sola_lo_verificado_y_deja_lo_demas(auth, ia, monkeypatch, limpiar_meta):
    monkeypatch.setattr(ia, "tavily_api_key", SecretStr("tvly-prueba"))
    monkeypatch.setattr(ia, "buscador_descubrimiento", "auto")
    sufijo = uuid4().hex[:8]
    buena, otra = f"empaques-{sufijo}.co", f"plasticos-{sufijo}.co"
    paginas = {
        f"https://{buena}/": f"Empaques {sufijo} S.A.S. Palmira, Colombia. Escríbanos a ventas@{buena}",
        f"https://{otra}/": f"Plásticos {sufijo} S.A.S. Palmira, Colombia. juan.perez@{otra}",
    }
    monkeypatch.setattr(
        "app.agentes.buscador.candidatos",
        lambda criterios: (
            [{"url": u, "titulo": "Empresa", "texto": t} for u, t in paginas.items()],
            2,
        ),
    )
    monkeypatch.setattr("app.agentes.tareas.lector_web.leer", lambda url: paginas[url])

    def empresa(nombre: str, dominio: str, correo: str) -> dict:
        valores = {
            "razonSocial": nombre,
            "municipio": "Palmira",
            "correoCorporativo": correo,
            "pais": "Colombia",
        }
        return {
            "razonSocial": nombre,
            "sitioWeb": f"https://{dominio}/",
            "sector": None,
            "municipio": "Palmira",
            "correoCorporativo": correo,
            "datos": [
                {"campo": k, "valor": v, "url": f"https://{dominio}/", "cita": v}
                for k, v in valores.items()
            ],
        }

    proveedor = Capturador(
        {
            "empresas": [
                empresa(f"Empaques {sufijo} S.A.S.", buena, f"ventas@{buena}"),
                empresa(f"Plásticos {sufijo} S.A.S.", otra, f"juan.perez@{otra}"),
            ]
        }
    )
    assert (
        auth.put("/api/descubrimiento/meta", json={**BASE, "metaDiaria": hechos() + 5}).status_code
        == 200
    )
    dia = datetime.now(BOGOTA).date().isoformat()
    with transaccion() as c:
        b = insertar(
            c,
            "busquedas_descubrimiento",
            {
                "consulta": "Busca empresas en Colombia: Fabricantes de empaques",
                "municipios": ["Palmira"],
            },
        )
        clave = str(uuid4())
        encolar(
            c, "descubrir_empresas", {"busquedaId": str(b["id"]), "meta": True, "fecha": dia}, clave
        )
        tarea = str(uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))["id"])
    antes = hechos()
    correr(
        "descubrir_empresas",
        {"busquedaId": str(b["id"]), "meta": True, "fecha": dia},
        tarea,
        proveedor,
    )
    with transaccion() as c:
        estados = {
            r["razon_social"]: r["estado"]
            for r in todos(
                c, "SELECT * FROM resultados_descubrimiento WHERE busqueda_id=%s", (b["id"],)
            )
        }
        assert estados == {
            f"Empaques {sufijo} S.A.S.": "aceptado",
            f"Plásticos {sufijo} S.A.S.": "pendiente",
        }
        ct = uno(c, "SELECT * FROM contactos WHERE correo=%s", (f"ventas@{buena}",))
        assert ct["origen"] == "descubrimiento"
        lineas = [
            e["mensaje"]
            for e in todos(c, "SELECT mensaje FROM eventos_trabajador WHERE tarea_id=%s", (tarea,))
        ]
    assert hechos() == antes + 1
    assert any(x.startswith("Búsqueda en la web (Tavily): 2 consultas, 2 páginas") for x in lineas)
    assert any(x.startswith(f"Aceptada sola (meta diaria): Empaques {sufijo}") for x in lineas)
    assert any("1 aceptada sola" in x for x in lineas)
    assert any(
        x == "El modelo devolvió 2 empresas; se comprueba cada dato en su sitio oficial."
        for x in lineas
    )
    # La lista de búsquedas marca las que lanzó la meta.
    lista = {x["id"]: x for x in auth.get("/api/descubrimiento/busquedas").json()}
    assert lista[str(b["id"])]["meta"] is True
    # Las líneas de la terminal no llevan correos.
    assert not any("@" in x for x in lineas)

    # Con la meta cumplida, lo nuevo queda pendiente de revisión.
    assert (
        auth.put("/api/descubrimiento/meta", json={**BASE, "metaDiaria": hechos()}).status_code
        == 200
    )
    otra_vez = uuid4().hex[:8]
    dominio3 = f"bebidas-{otra_vez}.co"
    paginas.clear()
    paginas[f"https://{dominio3}/"] = (
        f"Bebidas {otra_vez} S.A.S. Palmira, Colombia. info@{dominio3}"
    )
    proveedor = Capturador(
        {"empresas": [empresa(f"Bebidas {otra_vez} S.A.S.", dominio3, f"info@{dominio3}")]}
    )
    with transaccion() as c:
        b2 = insertar(
            c, "busquedas_descubrimiento", {"consulta": "Busca empresas en Colombia: Bebidas"}
        )
        clave = str(uuid4())
        encolar(
            c,
            "descubrir_empresas",
            {"busquedaId": str(b2["id"]), "meta": True, "fecha": dia},
            clave,
        )
        tarea2 = str(uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))["id"])
    correr(
        "descubrir_empresas",
        {"busquedaId": str(b2["id"]), "meta": True, "fecha": dia},
        tarea2,
        proveedor,
    )
    with transaccion() as c:
        r = uno(c, "SELECT estado FROM resultados_descubrimiento WHERE busqueda_id=%s", (b2["id"],))
    assert r["estado"] == "pendiente"


def test_terminal_muestra_trabajador_cola_y_bitacora(auth, ia, monkeypatch):
    from app.tareas.trabajador import procesar

    with transaccion() as c:
        c.execute("DELETE FROM latidos_trabajador")
    r = auth.get("/api/terminal").json()
    assert r["trabajador"] == {"activo": False}
    assert set(r["cola"]) == {"enProceso", "enCola", "esperando", "fallidas24h"}
    ultimo = r["eventos"][-1]["id"] if r["eventos"] else 0

    with transaccion() as c:
        bitacora.latido(c, "trabajador-prueba")
    r = auth.get(f"/api/terminal?desde={ultimo}").json()
    assert r["trabajador"]["activo"] is True and "vistoEn" in r["trabajador"]

    # Una tarea que termina bien deja «empieza» y «listo en N s».
    with transaccion() as c:
        clave = str(uuid4())
        encolar(c, "buscar_duplicados", {}, clave)
        t = uno(
            c,
            "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
            "intentos=intentos+1 WHERE clave_unica=%s RETURNING *",
            (clave,),
        )
    procesar(t)
    # Una búsqueda con Gemini saturado deja el aviso con el próximo intento.
    monkeypatch.setattr(ia, "buscador_descubrimiento", "proveedor")

    class Saturado:
        def ejecutar(self, modelo, prompt, entrada, schema, max_tokens):
            raise FalloProveedor(SATURADO.format(status=503), {})

    monkeypatch.setattr("app.agentes.gemini.GeminiReal", Saturado)
    with transaccion() as c:
        b = insertar(
            c,
            "busquedas_descubrimiento",
            {"consulta": "Busca empresas en Colombia: Hoteles", "municipios": ["Palmira"]},
        )
        clave = str(uuid4())
        encolar(c, "descubrir_empresas", {"busquedaId": str(b["id"])}, clave)
        t = uno(
            c,
            "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
            "intentos=intentos+1 WHERE clave_unica=%s RETURNING *",
            (clave,),
        )
    procesar(t)
    r = auth.get(f"/api/terminal?desde={ultimo}").json()
    lineas = [(e["nivel"], e["mensaje"]) for e in r["eventos"]]
    assert ("info", "Buscar empresas duplicadas: empieza.") in lineas
    assert any(
        n == "exito" and m.startswith("Buscar empresas duplicadas: listo en ") for n, m in lineas
    )
    assert any(
        n == "aviso"
        and m.startswith("Búsqueda de empresas: «Hoteles» en Palmira: Gemini está saturado")
        and "Intento 1 de 10; nuevo intento a las " in m
        for n, m in lineas
    )
    ids = [e["id"] for e in r["eventos"]]
    assert ids == sorted(ids) and all(i > ultimo for i in ids)

    # «Ahora mismo» (sin las tareas que otras pruebas dejaron en cola; se deshace al final).
    class Deshacer(Exception):
        pass

    with pytest.raises(Deshacer):
        with transaccion() as c:
            c.execute(
                "UPDATE tareas SET estado='cancelada',terminada_en=now() "
                "WHERE estado='pendiente' AND id<>%s",
                (t["id"],),
            )
            ahora_mismo = serializar(bitacora.terminal(c, None))["ahoraMismo"]
            raise Deshacer
    assert ahora_mismo == [
        {
            "tareaId": str(t["id"]),
            "descripcion": "Búsqueda de empresas: «Hoteles» en Palmira",
            "estado": "esperando",
            "desde": ahora_mismo[0]["desde"],
            "intento": 1,
            "maxIntentos": 10,
        }
    ]
    assert auth.get(f"/api/terminal?desde={ids[-1]}").json()["eventos"] == []
    with transaccion() as c:
        c.execute("UPDATE tareas SET estado='cancelada',terminada_en=now() WHERE id=%s", (t["id"],))


def test_migracion_0007_baja_y_sube(auth):
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run(
        [*alembic, "downgrade", "0006"], check=True, env={**os.environ}, capture_output=True
    )
    try:
        with transaccion() as c:
            assert not uno(c, "SELECT to_regclass('nexus.eventos_trabajador') AS t")["t"]
            assert not uno(c, "SELECT 1 FROM configuracion WHERE clave='descubrimiento'", (), False)
    finally:
        subprocess.run(
            [*alembic, "upgrade", "head"], check=True, env={**os.environ}, capture_output=True
        )
    with transaccion() as c:
        assert uno(c, "SELECT to_regclass('nexus.latidos_trabajador') AS t")["t"]
        valor = uno(c, "SELECT valor FROM configuracion WHERE clave='descubrimiento'")["valor"]
    assert valor["activa"] is False and valor["metaDiaria"] == 20
