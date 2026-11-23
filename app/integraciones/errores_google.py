"""Explica en palabras del administrador los errores de las API de Google (Gmail, GA4, Search
Console). Solo usa el código y el motivo («reason») que devuelve Google: nunca el contenido de
correos, claves ni identificadores de la cuenta."""

import json
import re

from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError


def _motivos(exc: HttpError) -> tuple[set[str], str]:
    try:
        error = json.loads(exc.content.decode("utf-8", "replace")).get("error", {})
    except (ValueError, AttributeError):
        return set(), ""
    if not isinstance(error, dict):
        return set(), ""
    motivos = {str(error.get("status", ""))}
    for clave in ("errors", "details"):
        for d in error.get(clave) or []:
            if isinstance(d, dict):
                motivos.add(str(d.get("reason", "")))
    # El texto solo se usa para clasificar; nunca se muestra.
    return {m for m in motivos if m}, str(error.get("message", ""))


def explicar(exc: Exception, servicio: str = "Google") -> str | None:
    """Mensaje fijo para un error de Google, o None si no es uno."""
    if isinstance(exc, RefreshError):
        return (
            f"{servicio}: Google rechazó la autorización guardada (vencida o revocada). Vuelva "
            "a conectarlo en Configuración → Integraciones"
        )
    if not isinstance(exc, HttpError):
        return None
    status = int(exc.resp.status)
    motivos, texto = _motivos(exc)
    if status == 403 and (
        motivos & {"accessNotConfigured", "SERVICE_DISABLED"}
        or "has not been used in project" in texto
        or "is disabled" in texto
    ):
        return (
            f"{servicio}: la API no está habilitada en su proyecto de Google Cloud. Habilítela "
            "en APIs y servicios → Biblioteca (mismo proyecto del inicio de sesión) y espere "
            "unos minutos"
        )
    if status == 401:
        return (
            f"{servicio}: Google rechazó la autorización (401). Vuelva a conectarlo en "
            "Configuración → Integraciones"
        )
    if status == 403 and motivos & {
        "insufficientPermissions",
        "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
        "PERMISSION_DENIED",
        "forbidden",
    }:
        return (
            f"{servicio}: Google negó el permiso (403). Revise la autorización o el acceso de "
            "la cuenta y vuelva a conectarlo en Configuración → Integraciones"
        )
    if status == 429 or motivos & {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "RESOURCE_EXHAUSTED",
    }:
        return f"{servicio}: se alcanzó el límite de uso de Google ({status})"
    if status >= 500:
        return f"{servicio} no está disponible temporalmente ({status})"
    motivo = next((m for m in sorted(motivos) if re.fullmatch(r"[A-Za-z_]{3,40}", m)), "")
    return f"{servicio}: Google respondió con el error {status}" + (
        f" ({motivo})" if motivo else ""
    )
