import json
from typing import Any

import httpx
from fastapi import HTTPException
from google.oauth2 import service_account
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.config import config
from app.db import Conn, actualizar, obtener, todos
from app.seguridad.auth import cifrar, descifrar


def gmail(c: Conn) -> Any:
    r = obtener(c, "integraciones", "gmail")
    if r["estado"] != "conectada" or not r["credenciales_cifradas"]:
        raise HTTPException(400, "Gmail está desconectado")
    cfg = config()
    creds = Credentials(
        None,
        refresh_token=descifrar(r["credenciales_cifradas"]),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret.get_secret_value(),
    )  # type: ignore[no-untyped-call]
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def credenciales_servicio() -> Any:
    raw = config().google_cuenta_servicio_json.get_secret_value()
    scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ]
    if raw.lstrip().startswith("{"):
        return service_account.Credentials.from_service_account_info(json.loads(raw), scopes=scopes)  # type: ignore[no-untyped-call]
    return service_account.Credentials.from_service_account_file(raw, scopes=scopes)  # type: ignore[no-untyped-call]


def pausar_todo(c: Conn, detalle: str) -> None:
    for r in todos(c, "SELECT id FROM campanas WHERE estado='activa'"):
        cid = r["id"]
        c.execute("UPDATE campanas SET estado='pausada' WHERE id=%s", (cid,))
        c.execute(
            "UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE campana_id=%s AND estado='activa'",
            (cid,),
        )
        c.execute(
            "UPDATE mensajes m SET estado='cancelado' FROM inscripciones i WHERE m.inscripcion_id=i.id AND i.campana_id=%s AND m.estado='programado'",
            (cid,),
        )
    c.execute("UPDATE integraciones SET detalle=%s WHERE id='gmail'", (detalle,))


def cambiar(c: Conn, ident: str, accion: str) -> None:
    r = obtener(c, "integraciones", ident, True)
    cfg = config()
    if accion == "desconectar":
        if ident == "gmail":
            if r["credenciales_cifradas"]:
                response = httpx.post(
                    "https://oauth2.googleapis.com/revoke",
                    data={"token": descifrar(r["credenciales_cifradas"])},
                    timeout=15,
                )
                if response.status_code not in (200, 400):
                    raise HTTPException(502, "Google no confirmó la revocación; vuelva a intentar")
            pausar_todo(c, "Gmail desconectado por el administrador")
        actualizar(
            c, "integraciones", ident, {"estado": "desconectada", "credenciales_cifradas": None}
        )
        return
    try:
        secret = ""
        if ident == "gmail":
            gmail(c).users().getProfile(userId="me").execute()
            return
        if ident == "verificador":
            secret = cfg.hunter_api_key.get_secret_value()
            if not secret:
                raise ValueError("Configure HUNTER_API_KEY")
            httpx.get(
                "https://api.hunter.io/v2/account", params={"api_key": secret}, timeout=15
            ).raise_for_status()
        elif ident == "formulario":
            if not cfg.formulario_webhook_secreto.get_secret_value():
                raise ValueError("Configure FORMULARIO_WEBHOOK_SECRETO")
        elif ident == "claude":
            from anthropic import Anthropic

            secret = cfg.anthropic_api_key.get_secret_value()
            if not secret:
                raise ValueError("Configure ANTHROPIC_API_KEY")
            Anthropic(api_key=secret).messages.create(
                model=cfg.claude_modelo_ligero,
                max_tokens=8,
                messages=[{"role": "user", "content": "Responde OK."}],
            )
        elif ident == "ga4":
            secret = cfg.google_cuenta_servicio_json.get_secret_value()
            service = build(
                "analyticsdata",
                "v1beta",
                credentials=credenciales_servicio(),
                cache_discovery=False,
            )
            service.properties().runReport(
                property="properties/" + cfg.ga4_propiedad_id,
                body={
                    "dateRanges": [{"startDate": "yesterday", "endDate": "yesterday"}],
                    "metrics": [{"name": "sessions"}],
                },
            ).execute()
        elif ident == "search_console":
            from datetime import date, timedelta

            secret = cfg.google_cuenta_servicio_json.get_secret_value()
            day = (date.today() - timedelta(days=3)).isoformat()
            build(
                "searchconsole", "v1", credentials=credenciales_servicio(), cache_discovery=False
            ).searchanalytics().query(
                siteUrl=cfg.search_console_sitio,
                body={"startDate": day, "endDate": day, "rowLimit": 1},
            ).execute()
        actualizar(
            c,
            "integraciones",
            ident,
            {
                "estado": "conectada",
                "detalle": "Conexión comprobada",
                "credenciales_cifradas": cifrar(secret) if secret else None,
            },
        )
    except Exception:
        actualizar(
            c,
            "integraciones",
            ident,
            {
                "estado": "error",
                "detalle": "No se pudo comprobar la conexión. Revise credenciales, permisos y disponibilidad del proveedor.",
            },
        )
