import json
import re
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


DESCONECTADA = {
    "gmail": "Desconectada por el administrador: autorización revocada en Google y campañas activas en pausa.",
    "formulario": "Desconectado: el receptor rechaza los envíos del sitio hasta volver a conectarlo.",
    "verificador": "Desconectado: los correos nuevos solo se comprueban por sintaxis y DNS.",
    "claude": "Desconectado: el informe semanal automático no se programa con Claude.",
    "ga4": "Desconectado: no se importan visitas de Google Analytics.",
    "search_console": "Desconectado: no se importan consultas de Search Console.",
}


def probar_proveedor_ia() -> tuple[str, str]:
    """Llamada mínima al proveedor de agentes activo. Devuelve (secreto, detalle)."""
    cfg = config()
    if cfg.agente_proveedor == "gemini":
        secret = cfg.gemini_api_key.get_secret_value()
        if not secret:
            raise ValueError("Configure GEMINI_API_KEY en el servidor")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", cfg.gemini_modelo):
            raise ValueError("GEMINI_MODELO no válido")
        httpx.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{cfg.gemini_modelo}:generateContent",
            headers={"x-goog-api-key": secret},
            json={
                "contents": [{"role": "user", "parts": [{"text": "Responde OK."}]}],
                "generationConfig": {"maxOutputTokens": 8},
            },
            timeout=30,
        ).raise_for_status()
        return secret, "Conexión comprobada con Gemini (proveedor activo de agentes)"
    from anthropic import Anthropic

    secret = cfg.anthropic_api_key.get_secret_value()
    if not secret:
        raise ValueError("Configure ANTHROPIC_API_KEY en el servidor")
    Anthropic(api_key=secret).messages.create(
        model=cfg.claude_modelo_ligero,
        max_tokens=8,
        messages=[{"role": "user", "content": "Responde OK."}],
    )
    return secret, "Conexión comprobada con Claude (proveedor activo de agentes)"


def cambiar(c: Conn, ident: str, accion: str) -> dict[str, str]:
    """Conecta o desconecta una integración y devuelve el estado real resultante."""
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
        detalle = DESCONECTADA.get(ident, "Desconectada por el administrador")
        actualizar(
            c,
            "integraciones",
            ident,
            {"estado": "desconectada", "credenciales_cifradas": None, "detalle": detalle},
        )
        return {"estado": "desconectada", "detalle": detalle}
    detalle = "Conexión comprobada"
    try:
        secret = ""
        if ident == "gmail":
            # Gmail se conecta con OAuth; aquí solo se comprueba una conexión existente.
            gmail(c).users().getProfile(userId="me").execute()
            actualizar(c, "integraciones", ident, {"detalle": detalle})
            return {"estado": str(r["estado"]), "detalle": detalle}
        if ident == "verificador":
            secret = cfg.hunter_api_key.get_secret_value()
            if not secret:
                raise ValueError("Configure HUNTER_API_KEY en el servidor")
            httpx.get(
                "https://api.hunter.io/v2/account", params={"api_key": secret}, timeout=15
            ).raise_for_status()
        elif ident == "formulario":
            if not cfg.formulario_webhook_secreto.get_secret_value():
                raise ValueError("Configure FORMULARIO_WEBHOOK_SECRETO en el servidor")
            detalle = "Conectado: el receptor acepta envíos firmados del sitio"
        elif ident == "claude":
            secret, detalle = probar_proveedor_ia()
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
                "detalle": detalle,
                "credenciales_cifradas": cifrar(secret) if secret else None,
            },
        )
        return {"estado": "conectada", "detalle": detalle}
    except HTTPException:
        raise
    except Exception as exc:
        # Los ValueError son mensajes propios (nunca contienen secretos).
        detalle = (
            str(exc)
            if isinstance(exc, ValueError)
            else "No se pudo comprobar la conexión. Revise credenciales, permisos y disponibilidad del proveedor."
        )
        actualizar(c, "integraciones", ident, {"estado": "error", "detalle": detalle})
        return {"estado": "error", "detalle": detalle}
