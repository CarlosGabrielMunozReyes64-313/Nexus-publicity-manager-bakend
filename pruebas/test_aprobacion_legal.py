"""Aprobación automática de la base legal (política de cumplimiento, migración 0006)."""

# ruff: noqa: F811
import os
import subprocess
import sys
from datetime import datetime, timezone
from uuid import uuid4

import psycopg
import pytest
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.config import config
from app.db import insertar, todos, transaccion, uno
from app.integraciones import hunter


def admin_id(c):
    return str(
        uno(c, "SELECT id FROM administradores WHERE correo=%s", (config().admin_correo,))["id"]
    )


def empresa_con_correo_publicado(c, correo: str) -> dict:
    """Como lo deja Descubrimiento: correo corporativo con página y cita verificada."""
    e = insertar(
        c, "empresas", {"razon_social": "Legal " + uuid4().hex[:6] + " SAS", "municipio": "Palmira"}
    )
    insertar(
        c,
        "evidencias",
        {
            "empresa_id": e["id"],
            "campo": "correoCorporativo",
            "valor": correo,
            "fuente": "agente",
            "url": "https://empresa.example/contacto",
            "cita": f"Escríbanos a {correo}",
            "estado": "verificado",
            "cita_verificada_en": datetime.now(timezone.utc),
        },
    )
    return e


def contacto(c, empresa_id, correo: str, base: dict | None = None) -> str:
    ct = insertar(
        c,
        "contactos",
        {
            "empresa_id": empresa_id,
            "nombre": "Contacto corporativo",
            "correo": correo,
            "origen": "descubrimiento",
        },
    )
    insertar(
        c, "bases_legales", {"contacto_id": ct["id"], **(base or {"tipo": "corporativo_generico"})}
    )
    return str(ct["id"])


def verificar(ident: str) -> None:
    with transaccion("proceso:verificador") as c:
        hunter.verificar(c, ident, hunter.HunterFalso({"status": "valid"}))


def base(ident: str) -> dict:
    with transaccion() as c:
        return uno(c, "SELECT * FROM bases_legales WHERE contacto_id=%s AND vigente", (ident,))


def motivo(ident: str):
    with transaccion() as c:
        return uno(c, "SELECT motivo_no_elegible(%s) AS m", (ident,))["m"]


def test_aprobacion_legal_automatica(auth):
    correo = f"hablemos@{uuid4().hex[:8]}.example"
    with transaccion("admin") as c:
        admin = admin_id(c)
        e = empresa_con_correo_publicado(c, correo)
        apagada = contacto(c, e["id"], correo)
    try:
        # Apagada (por defecto): verificar el correo no aprueba nada.
        assert auth.get("/api/configuracion/cumplimiento").json() == {
            "aprobacionLegalAutomatica": False
        }
        verificar(apagada)
        assert base(apagada)["aprobada_en"] is None and motivo(apagada) == "Base legal sin aprobar"
        r = auth.put("/api/configuracion/cumplimiento", json={"aprobacionLegalAutomatica": "sí"})
        assert r.status_code == 400

        with transaccion("admin") as c:
            sin_prueba_empresa = insertar(
                c,
                "empresas",
                {"razon_social": "Sin prueba " + uuid4().hex[:6], "municipio": "Palmira"},
            )
            sin_prueba = contacto(c, sin_prueba_empresa["id"], f"ventas@{uuid4().hex[:8]}.example")
            otra = empresa_con_correo_publicado(c, f"hola@{uuid4().hex[:8]}.example")
            manual = contacto(
                c,
                otra["id"],
                f"maria@{uuid4().hex[:8]}.example",
                {
                    "tipo": "autorizacion_registrada",
                    "texto": "Firmó el formulario impreso",
                    "prueba": "Archivo 2026-11",
                },
            )

        # Encenderla aplica la política a los pendientes con prueba real.
        r = auth.put("/api/configuracion/cumplimiento", json={"aprobacionLegalAutomatica": True})
        assert r.status_code == 200 and r.json()["ok"] is True and r.json()["aprobadas"] >= 2
        b = base(apagada)
        assert b["aprobada_en"] is not None and str(b["aprobada_por"]) == admin
        assert b["tipo"] == "corporativo_generico" and "automáticamente" in b["texto"]
        assert b["prueba"].startswith("https://empresa.example/contacto — «Escríbanos a ")
        assert motivo(apagada) is None
        with transaccion() as c:
            assert (
                uno(c, "SELECT etapa FROM contactos WHERE id=%s", (apagada,))["etapa"]
                == "contactable"
            )
            assert (
                uno(
                    c,
                    "SELECT count(*) AS n FROM registro_auditoria WHERE accion='aprobacion_legal_automatica' AND entidad_id=%s",
                    (apagada,),
                )["n"]
                == 1
            )
        # Sin página publicada no hay prueba: queda pendiente para el administrador.
        assert base(sin_prueba)["aprobada_en"] is None
        # Lo que el administrador ya registró se aprueba tal cual.
        b = base(manual)
        assert b["aprobada_en"] is not None and b["texto"] == "Firmó el formulario impreso"

        # Un contacto nuevo se aprueba solo al verificar su correo.
        nuevo_correo = f"servicio@{uuid4().hex[:8]}.example"
        with transaccion("admin") as c:
            nuevo = contacto(c, empresa_con_correo_publicado(c, nuevo_correo)["id"], nuevo_correo)
        verificar(nuevo)
        assert base(nuevo)["aprobada_en"] is not None and motivo(nuevo) is None

        # Apagada otra vez: los nuevos vuelven a esperar al administrador.
        assert auth.put(
            "/api/configuracion/cumplimiento", json={"aprobacionLegalAutomatica": False}
        ).json() == {
            "ok": True,
            "aprobadas": 0,
        }
        otro_correo = f"hablemos@{uuid4().hex[:8]}.example"
        with transaccion("admin") as c:
            otro = contacto(c, empresa_con_correo_publicado(c, otro_correo)["id"], otro_correo)
        verificar(otro)
        assert base(otro)["aprobada_en"] is None
    finally:
        with transaccion("admin") as c:
            c.execute(
                "UPDATE configuracion SET valor='{\"aprobacionLegalAutomatica\": false}' WHERE clave='cumplimiento'"
            )


def test_migracion_0006_clave_cumplimiento_y_bajada(auth):
    env = {**os.environ}
    alembic = [sys.executable, "-m", "alembic"]
    subprocess.run([*alembic, "downgrade", "0005"], check=True, env=env, capture_output=True)
    try:
        with transaccion() as c:
            assert not uno(c, "SELECT 1 FROM configuracion WHERE clave='cumplimiento'", (), False)
        with pytest.raises(psycopg.errors.CheckViolation):
            with transaccion() as c:
                c.execute("INSERT INTO configuracion(clave,valor) VALUES('otra','{}')")
    finally:
        subprocess.run([*alembic, "upgrade", "head"], check=True, env=env, capture_output=True)
    with transaccion() as c:
        assert todos(c, "SELECT valor FROM configuracion WHERE clave='cumplimiento'") == [
            {"valor": {"aprobacionLegalAutomatica": False}}
        ]
    with pytest.raises(psycopg.errors.CheckViolation):
        with transaccion() as c:
            c.execute("INSERT INTO configuracion(clave,valor) VALUES('otra','{}')")
