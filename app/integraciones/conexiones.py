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


def pausar_todo(c: Conn, detalle: str) -> int:
    """Pausa todas las campañas activas. Devuelve cuántas estaban activas."""
    activas = todos(c, "SELECT id FROM campanas WHERE estado='activa'")
    for r in activas:
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
    return len(activas)


DESCONECTADA = {
    "gmail": "Desconectada por el administrador: autorización revocada en Google y campañas activas en pausa.",
    "formulario": "Desconectado: el receptor rechaza los envíos del sitio hasta volver a conectarlo.",
    "verificador": "Desconectado: los correos nuevos solo se comprueban por sintaxis y DNS.",
    "claude": "Desconectado: el informe semanal automático no se programa con Claude.",
    "ga4": "Desconectado: no se importan visitas de Google Analytics.",
    "search_console": "Desconectado: no se importan consultas de Search Console.",
}


def probar_proveedor_ia() -> tuple[str, str]:
    """Llamada mínima al proveedor de agentes activo y al buscador de Descubrimiento.

    Devuelve (secreto, detalle). Con Tavily configurado se prueba Tavily (1 crédito) en
    lugar de la búsqueda integrada del proveedor.
    """
    from app.agentes import buscador
    from app.agentes.errores import FalloProveedor

    cfg = config()
    try:
        tavily = buscador.usar_tavily()
    except FalloProveedor as exc:
        raise ValueError(str(exc)) from None
    if cfg.agente_proveedor == "gemini":
        secret = cfg.gemini_api_key.get_secret_value()
        if not secret:
            raise ValueError("Configure GEMINI_API_KEY en el servidor")
        if not re.fullmatch(r"[a-zA-Z0-9._-]+", cfg.gemini_modelo):
            raise ValueError("GEMINI_MODELO no válido")
        from app.agentes.gemini import probar_conexion

        detalle = probar_conexion(secret, cfg.gemini_modelo, buscar=not tavily)
    else:
        secret = cfg.anthropic_api_key.get_secret_value()
        if not secret:
            raise ValueError("Configure ANTHROPIC_API_KEY en el servidor")
        detalle = probar_claude(secret, buscar=not tavily)
    if not tavily:
        return secret, detalle
    try:
        buscador.probar(cfg.tavily_api_key.get_secret_value())
    except FalloProveedor as exc:
        return secret, f"{detalle}, pero la prueba de Tavily (Descubrimiento) falló: {exc}"
    return secret, f"{detalle}; Descubrimiento usará Tavily (búsqueda comprobada)"


def probar_claude(secret: str, cliente: Any = None, buscar: bool = True) -> str:
    """Clave y modelo ligero; aparte, la búsqueda web que usa Descubrimiento."""
    import anthropic

    from app.agentes.ejecutor import clasificar_claude, revisar_busqueda_claude
    from app.agentes.errores import FalloProveedor

    cfg = config()
    client = cliente or anthropic.Anthropic(api_key=secret, timeout=60, max_retries=1)
    try:
        client.messages.create(
            model=cfg.claude_modelo_ligero,
            max_tokens=8,
            messages=[{"role": "user", "content": "Responde OK."}],
        )
    except anthropic.APIStatusError as exc:
        raise ValueError(clasificar_claude(exc)[0]) from None
    except anthropic.APIConnectionError:
        raise ValueError("Claude no disponible (red); revise clave, cuota y modelo") from None
    if not buscar:
        return "Conexión comprobada con Claude"
    try:
        found = client.messages.create(
            model=cfg.claude_modelo_mayor,
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": "Busca en la web la fecha de hoy en Bogotá y responde solo la fecha.",
                }
            ],
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": 1}],
        )
        revisar_busqueda_claude([b.model_dump() for b in found.content], {})
    except anthropic.APIStatusError as exc:
        return (
            "Claude responde, pero la prueba de búsqueda web (Descubrimiento) falló: "
            + clasificar_claude(exc)[0]
        )
    except anthropic.APIConnectionError:
        return "Claude responde, pero la prueba de búsqueda web no obtuvo respuesta (red)"
    except FalloProveedor as exc:
        return f"Claude responde, pero la prueba de búsqueda web (Descubrimiento) falló: {exc}"
    return "Conexión comprobada con Claude, incluida la búsqueda web que usa Descubrimiento"


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
        if ident == "verificador":
            from app.api.datos import verificar_pendientes

            # Los correos guardados sin verificador quedaron «desconocido»: se verifican ahora.
            n = verificar_pendientes(c)
            detalle = (
                f"Conexión comprobada. Se verificarán {n} correos pendientes." if n else detalle
            )
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
        from app.integraciones.errores_google import explicar

        servicio = {"gmail": "Gmail", "ga4": "Google Analytics", "search_console": "Search Console"}
        # Los ValueError son mensajes propios (nunca contienen secretos).
        detalle = (
            str(exc)
            if isinstance(exc, ValueError)
            else explicar(exc, servicio.get(ident, "Google"))
            or "No se pudo comprobar la conexión. Revise credenciales, permisos y disponibilidad del proveedor."
        )
        actualizar(c, "integraciones", ident, {"estado": "error", "detalle": detalle})
        return {"estado": "error", "detalle": detalle}
