"""Solicitudes de acceso: lista, aprobación y rechazo (solo administradores).

La decisión, su auditoría y el aviso al solicitante se guardan en una sola transacción.
Si dos administradores deciden a la vez, el UPDATE condicionado a estado='pendiente' deja
pasar solo al primero; el segundo recibe 409 con el estado definitivo.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import Conn, auditar, todos, transaccion, uno
from app.esquemas.serializar import serializar
from app.seguridad.auth import administrador

router = APIRouter(prefix="/api")

COLUMNAS = """s.id, s.correo::text AS correo, s.nombre, s.estado, s.creada_en,
    s.ultima_identificacion_en, s.identificaciones, s.decidida_en,
    CASE WHEN d.id IS NULL THEN NULL ELSE jsonb_build_object(
      'id', d.id, 'nombre', d.nombre, 'correo', d.correo::text) END AS decidida_por"""
DESDE = "FROM solicitudes_acceso s LEFT JOIN administradores d ON d.id=s.decidida_por"
ETIQUETA = {"aprobada": "aprobada", "rechazada": "rechazada"}


def leer_solicitud(c: Conn, ident: str) -> dict[str, Any]:
    return uno(c, f"SELECT {COLUMNAS} {DESDE} WHERE s.id=%s", (ident,))


def _ident(texto: str) -> str:
    try:
        return str(UUID(texto))
    except ValueError:
        raise HTTPException(404, "Solicitud no encontrada") from None


@router.get("/solicitudes-acceso")
def lista(request: Request) -> list[dict[str, Any]]:
    admin = administrador(request)
    with transaccion("admin", admin) as c:
        filas = todos(
            c,
            f"""SELECT {COLUMNAS} {DESDE}
            ORDER BY (s.estado='pendiente') DESC,
              CASE WHEN s.estado='pendiente' THEN s.creada_en END ASC,
              s.decidida_en DESC NULLS LAST
            LIMIT 500""",
        )
    return [serializar(f) for f in filas]


def decidir(request: Request, ident: str, decision: str) -> Any:
    admin = administrador(request)
    ident = _ident(ident)
    estado = "aprobada" if decision == "aprobar" else "rechazada"
    accion = "aprobar_solicitud_acceso" if decision == "aprobar" else "rechazar_solicitud_acceso"
    with transaccion("admin", admin, accion) as c:
        r = uno(
            c,
            "UPDATE solicitudes_acceso SET estado=%s,decidida_por=%s,decidida_en=now() "
            "WHERE id=%s AND estado='pendiente' RETURNING id,correo::text AS correo,nombre",
            (estado, admin, ident),
            False,
        )
        if not r:
            actual = leer_solicitud(c, ident)  # 404 si no existe
            quien = (actual.get("decidida_por") or {}).get("nombre") or "otro administrador"
            return JSONResponse(
                {
                    "message": f"Esta solicitud ya fue {ETIQUETA[actual['estado']]} por {quien}. "
                    "Se conserva esa decisión.",
                    "solicitud": serializar(actual),
                },
                status_code=409,
            )
        if estado == "aprobada":
            aprobar(c, r)
        auditar(c, accion, "solicitudes_acceso", ident)
        return {"ok": True, "solicitud": serializar(leer_solicitud(c, ident))}


def aprobar(c: Conn, solicitud: dict[str, Any]) -> None:
    """Crea la cuenta con rol «usuario» y deja el aviso de bienvenida para su primer ingreso.

    Si ya existe una cuenta con ese correo (p. ej., una cuenta administrativa desactivada),
    se enlaza sin tocar su rol ni su estado: la solicitud nunca reactiva una cuenta.
    """
    from app.notificaciones import notificar

    nombre = solicitud["nombre"] or solicitud["correo"].split("@", 1)[0]
    nueva = uno(
        c,
        "INSERT INTO administradores(correo,nombre,rol,activo) VALUES(%s,%s,'usuario',true) "
        "ON CONFLICT (correo) DO NOTHING RETURNING id",
        (solicitud["correo"], nombre),
        False,
    )
    cuenta = nueva or uno(
        c, "SELECT id FROM administradores WHERE correo=%s", (solicitud["correo"],)
    )
    c.execute(
        "UPDATE solicitudes_acceso SET cuenta_id=%s WHERE id=%s", (cuenta["id"], solicitud["id"])
    )
    if nueva:
        notificar(
            c,
            str(cuenta["id"]),
            "acceso_aprobado",
            "Tu acceso fue aprobado",
            "Un administrador aprobó tu solicitud. Tienes acceso completo a NEXUS, salvo "
            "aprobar o rechazar solicitudes de acceso.",
            "/",
            f"acceso_aprobado:{solicitud['id']}",
        )


@router.post("/solicitudes-acceso/{ident}/aprobar")
def aprobar_ruta(ident: str, request: Request) -> Any:
    return decidir(request, ident, "aprobar")


@router.post("/solicitudes-acceso/{ident}/rechazar")
def rechazar_ruta(ident: str, request: Request) -> Any:
    return decidir(request, ident, "rechazar")
