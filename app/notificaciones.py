"""Centro de notificaciones: avisos que deben poder consultarse después.

Cada aviso tiene un destinatario y una `clave` de incidente. Volver a notificar la misma
clave al mismo destinatario no crea otro aviso: suma `repeticiones` y actualiza el texto.
Si el incidente había cesado (sin repetirse durante HUECO_INCIDENTE), cuenta como uno nuevo
y vuelve a aparecer sin leer. La lectura es por destinatario y queda guardada en la base.

Los toasts del frontend confirman acciones inmediatas; esta bandeja es para lo que ocurre
en segundo plano o para otra persona.
"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request

from app.db import Conn, todos, transaccion, uno
from app.esquemas.serializar import serializar
from app.seguridad.auth import cuenta_actual
from app.seguridad.permisos import pantalla_permitida

router = APIRouter(prefix="/api")

TIPOS = (
    "solicitud_acceso",
    "acceso_aprobado",
    "pausa_envios",
    "fallo_integracion",
    "tarea_fallida",
)
HUECO_INCIDENTE = "6 hours"
LIMITE_BANDEJA = 30

_UPSERT = f"""
    ON CONFLICT (destinatario_id, clave) DO UPDATE SET
      titulo = EXCLUDED.titulo,
      descripcion = EXCLUDED.descripcion,
      enlace = EXCLUDED.enlace,
      repeticiones = CASE WHEN notificaciones.ultima_vez < now() - interval '{HUECO_INCIDENTE}'
                          THEN 1 ELSE notificaciones.repeticiones + 1 END,
      creada_en = CASE WHEN notificaciones.ultima_vez < now() - interval '{HUECO_INCIDENTE}'
                       THEN now() ELSE notificaciones.creada_en END,
      leida_en = CASE WHEN notificaciones.ultima_vez < now() - interval '{HUECO_INCIDENTE}'
                      THEN NULL ELSE notificaciones.leida_en END,
      ultima_vez = now()
"""


def _texto(valor: str, maximo: int) -> str:
    valor = " ".join(str(valor).split())
    return valor if len(valor) <= maximo else valor[: maximo - 1] + "…"


def notificar(
    c: Conn,
    destinatario_id: str,
    tipo: str,
    titulo: str,
    descripcion: str,
    enlace: str | None,
    clave: str,
) -> None:
    """Crea o agrupa el aviso de un destinatario, en la transacción del evento."""
    assert tipo in TIPOS
    c.execute(
        "INSERT INTO notificaciones(destinatario_id,tipo,titulo,descripcion,enlace,clave) "
        "VALUES(%s,%s,%s,%s,%s,%s)" + _UPSERT,
        (
            destinatario_id,
            tipo,
            _texto(titulo, 200),
            _texto(descripcion, 1000),
            enlace,
            _texto(clave, 300),
        ),
    )


def notificar_administradores(
    c: Conn, tipo: str, titulo: str, descripcion: str, enlace: str | None, clave: str
) -> int:
    """Un aviso para cada administrador activo (quienes pueden resolverlo)."""
    assert tipo in TIPOS
    cur = c.execute(
        "INSERT INTO notificaciones(destinatario_id,tipo,titulo,descripcion,enlace,clave) "
        "SELECT id,%s,%s,%s,%s,%s FROM administradores WHERE activo AND rol='administrador'"
        + _UPSERT,
        (tipo, _texto(titulo, 200), _texto(descripcion, 1000), enlace, _texto(clave, 300)),
    )
    return cur.rowcount


def _publica(fila: dict[str, Any], rol: str) -> dict[str, Any]:
    enlace = fila.pop("enlace", None)
    fila["leida"] = fila["leida_en"] is not None
    if pantalla_permitida(rol, enlace):
        fila["enlace"] = enlace
    publica: dict[str, Any] = serializar(fila)
    return publica


def _ident(texto: str) -> str:
    try:
        return str(UUID(texto))
    except ValueError:
        raise HTTPException(404, "Aviso no encontrado") from None


@router.get("/notificaciones")
def bandeja(request: Request) -> dict[str, Any]:
    cuenta = cuenta_actual(request)
    with transaccion(cuenta.actor, cuenta.id) as c:
        filas = todos(
            c,
            "SELECT id,tipo,titulo,descripcion,enlace,repeticiones,creada_en,ultima_vez,leida_en "
            "FROM notificaciones WHERE destinatario_id=%s ORDER BY ultima_vez DESC LIMIT %s",
            (cuenta.id, LIMITE_BANDEJA),
        )
        sin_leer = uno(
            c,
            "SELECT count(*) AS n FROM notificaciones WHERE destinatario_id=%s AND leida_en IS NULL",
            (cuenta.id,),
        )["n"]
    return {"sinLeer": sin_leer, "items": [_publica(f, cuenta.rol) for f in filas]}


@router.post("/notificaciones/leer-todas")
def leer_todas(request: Request) -> dict[str, Any]:
    cuenta = cuenta_actual(request)
    with transaccion(cuenta.actor, cuenta.id) as c:
        n = c.execute(
            "UPDATE notificaciones SET leida_en=now() WHERE destinatario_id=%s AND leida_en IS NULL",
            (cuenta.id,),
        ).rowcount
    return {"ok": True, "marcadas": n}


@router.post("/notificaciones/{ident}/leer")
def leer(ident: str, request: Request) -> dict[str, Any]:
    cuenta = cuenta_actual(request)
    with transaccion(cuenta.actor, cuenta.id) as c:
        # Solo el destinatario: un aviso ajeno responde igual que uno inexistente.
        r = uno(
            c,
            "UPDATE notificaciones SET leida_en=coalesce(leida_en,now()) "
            "WHERE id=%s AND destinatario_id=%s RETURNING id",
            (_ident(ident), cuenta.id),
            False,
        )
    if not r:
        raise HTTPException(404, "Aviso no encontrado")
    return {"ok": True}


# Tareas del trabajador: a qué pantalla lleva su aviso y si es una integración.
ENLACES_TAREA = {
    "descubrir_empresas": "/descubrimiento",
    "enriquecer_empresa": "/empresas",
    "clasificar_respuesta": "/respuestas",
    "proponer_segmentos": "/segmentos",
    "proponer_campana": "/secuencias",
    "revisar_campana": "/secuencias",
    "reescribir_correo": "/secuencias",
    "informe_semanal": "/informes",
}
INTEGRACIONES_TAREA = {
    "sincronizar_gmail": "Gmail",
    "enviar_mensaje": "Gmail",
    "programar_envios": "Gmail",
    "sincronizar_ga4": "Google Analytics",
    "sincronizar_search_console": "Search Console",
}


def avisar_fallo_tarea(c: Conn, tipo: str, descripcion: str, motivo: str) -> None:
    """Aviso de una tarea que falló sin más reintentos.

    La clave agrupa por tipo de tarea y motivo: los reintentos y las repeticiones del mismo
    incidente (p. ej., la revisión de Gmail que falla cada minuto) suman repeticiones en un
    solo aviso en lugar de crear uno nuevo cada vez.
    """
    import hashlib

    huella = hashlib.sha256(motivo.encode()).hexdigest()[:16]
    servicio = INTEGRACIONES_TAREA.get(tipo)
    if servicio:
        notificar_administradores(
            c,
            "fallo_integracion",
            f"Falla la integración con {servicio}",
            f"{descripcion}: {motivo}. Revise la integración en Configuración.",
            "/configuracion",
            f"integracion:{tipo}:{huella}",
        )
        return
    notificar_administradores(
        c,
        "tarea_fallida",
        f"Requiere intervención: {descripcion}",
        f"La tarea no se completó tras sus reintentos. Motivo: {motivo}.",
        ENLACES_TAREA.get(tipo, "/terminal"),
        f"tarea:{tipo}:{huella}",
    )
