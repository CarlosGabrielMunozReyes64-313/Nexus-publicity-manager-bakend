"""Errores de Google explicados en la Terminal y en Integraciones; Gmail sin reintentos inútiles."""
# ruff: noqa: F811  (los fixtures de pytest se importan y se reciben como argumento)

import json
from uuid import uuid4

import httplib2
import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError
from test_base import client  # noqa: F401
from test_datos import auth  # noqa: F401

from app.db import encolar, todos, transaccion, uno
from app.integraciones.errores_google import explicar
from app.seguridad.auth import cifrar


def http_error(status: int, reason: str = "", message: str = "", status_txt: str = "") -> HttpError:
    cuerpo = {
        "error": {
            "code": status,
            "message": message,
            "status": status_txt,
            "errors": [{"reason": reason}] if reason else [],
        }
    }
    return HttpError(
        httplib2.Response({"status": status}),
        json.dumps(cuerpo).encode(),
        uri="https://gmail.googleapis.com/gmail/v1/users/me/profile",
    )


@pytest.mark.parametrize(
    ("exc", "esperado"),
    [
        (
            http_error(403, "accessNotConfigured", "Gmail API has not been used in project 123"),
            "Gmail: la API no está habilitada en su proyecto de Google Cloud",
        ),
        (
            http_error(403, message="x", status_txt="PERMISSION_DENIED"),
            "Gmail: Google negó el permiso (403)",
        ),
        (http_error(401, "authError"), "Gmail: Google rechazó la autorización (401)"),
        (
            http_error(429, "rateLimitExceeded"),
            "Gmail: se alcanzó el límite de uso de Google (429)",
        ),
        (http_error(503), "Gmail no está disponible temporalmente (503)"),
        (
            http_error(400, "failedPrecondition"),
            "Gmail: Google respondió con el error 400 (failedPrecondition)",
        ),
        (RefreshError("invalid_grant"), "Gmail: Google rechazó la autorización guardada"),
    ],
)
def test_explicar_errores_de_google(exc, esperado):
    texto = explicar(exc, "Gmail")
    assert texto and texto.startswith(esperado)
    # Nunca repite el texto de Google (puede traer identificadores del proyecto).
    assert "123" not in texto and "invalid_grant" not in texto


def test_explicar_ignora_otros_errores():
    assert explicar(KeyError("x"), "Gmail") is None


class GmailRoto:
    def __init__(self, exc: Exception):
        self.exc = exc

    def users(self):
        return self

    def getProfile(self, userId):  # noqa: N802 (nombre de la API de Google)
        return self

    def execute(self):
        raise self.exc


@pytest.fixture
def gmail_conectada():
    with transaccion() as c:
        antes = uno(
            c, "SELECT estado,credenciales_cifradas,detalle FROM integraciones WHERE id='gmail'"
        )
        c.execute(
            "UPDATE integraciones SET estado='conectada',credenciales_cifradas=%s WHERE id='gmail'",
            (cifrar("token-falso"),),
        )
    yield
    with transaccion() as c:
        c.execute(
            "UPDATE integraciones SET estado=%s,credenciales_cifradas=%s,detalle=%s WHERE id='gmail'",
            (antes["estado"], antes["credenciales_cifradas"], antes["detalle"]),
        )


def revisar_gmail():
    from app.tareas.trabajador import procesar

    with transaccion() as c:
        clave = "gmail-prueba:" + str(uuid4())
        encolar(c, "sincronizar_gmail", {}, clave, 2)
        t = uno(
            c,
            "UPDATE tareas SET estado='en_proceso',tomada_por='prueba',tomada_en=now(),"
            "intentos=intentos+1 WHERE clave_unica=%s RETURNING *",
            (clave,),
        )
    procesar(t)
    with transaccion() as c:
        return uno(c, "SELECT * FROM tareas WHERE id=%s", (t["id"],))


def test_gmail_con_api_deshabilitada_lo_dice_sin_reintentos_ni_ruido(
    auth, monkeypatch, gmail_conectada
):
    exc = http_error(
        403, "accessNotConfigured", "Gmail API has not been used in project 9", "PERMISSION_DENIED"
    )
    monkeypatch.setattr("app.integraciones.conexiones.gmail", lambda c: GmailRoto(exc))
    with transaccion() as c:
        ultimo = uno(c, "SELECT coalesce(max(id),0) AS n FROM eventos_trabajador")["n"]
        detalle_antes = uno(c, "SELECT detalle FROM integraciones WHERE id='gmail'")["detalle"]
    t = revisar_gmail()
    # Una sola vez: la próxima revisión (en 2 minutos) es el reintento.
    assert t["estado"] == "fallida" and t["intentos"] == t["max_intentos"] == 1
    assert t["ultimo_error"].startswith(
        "Gmail: la API no está habilitada en su proyecto de Google Cloud"
    )
    with transaccion() as c:
        # Con un solo fallo no se pausan los envíos.
        assert (
            uno(c, "SELECT detalle FROM integraciones WHERE id='gmail'")["detalle"] == detalle_antes
        )
    revisar_gmail()
    revisar_gmail()
    with transaccion() as c:
        lineas = todos(
            c, "SELECT nivel,mensaje FROM eventos_trabajador WHERE id>%s ORDER BY id", (ultimo,)
        )
        detalle = uno(c, "SELECT detalle FROM integraciones WHERE id='gmail'")["detalle"]
    # El mismo fallo se anota una vez cada 10 minutos, no en cada vuelta.
    fallos = [x for x in lineas if x["mensaje"].startswith("Revisar respuestas en Gmail: falló.")]
    assert len(fallos) == 1 and fallos[0]["nivel"] == "error"
    assert "la API no está habilitada" in fallos[0]["mensaje"] and "9" not in fallos[0]["mensaje"]
    # Tres fallos seguidos: envíos en pausa, con el motivo a la vista en Integraciones.
    assert detalle.startswith("Envíos en pausa: Gmail: la API no está habilitada")


def test_error_desconocido_dice_su_clase(auth, monkeypatch, gmail_conectada):
    class Raro:
        def users(self):
            raise KeyError("historyId")

    monkeypatch.setattr("app.integraciones.conexiones.gmail", lambda c: Raro())
    t = revisar_gmail()
    assert t["ultimo_error"] == "Error no clasificado (KeyError); requiere diagnóstico adicional"


def test_probar_conexion_de_gmail_explica_el_error(auth, monkeypatch, gmail_conectada):
    from app.integraciones.conexiones import cambiar

    monkeypatch.setattr(
        "app.integraciones.conexiones.gmail",
        lambda c: GmailRoto(http_error(403, "accessNotConfigured", "is disabled")),
    )
    with transaccion("admin") as c:
        r = cambiar(c, "gmail", "conectar")
    assert r["estado"] == "error"
    assert r["detalle"].startswith("Gmail: la API no está habilitada")
