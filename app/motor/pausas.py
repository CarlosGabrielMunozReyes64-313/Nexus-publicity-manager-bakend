"""Pausas aisladas de campañas: una campaña con problemas no bloquea a las demás."""

from typing import Any

from app.db import Conn, auditar, uno
from app.dominio.reglas import BOGOTA, VentanaIncompatible, siguiente_ventana

MOTIVO_VENTANA = (
    "El horario de la secuencia aprobada no coincide con el horario global de envío. "
    "Ajuste Configuración → Límites y horarios o la secuencia y vuelva a reanudar la campaña."
)


def pausar_campana(c: Conn, campana_id: str, accion: str = "pausa_automatica_horario") -> bool:
    """Pausa una campaña activa, sus inscripciones activas y cancela sus envíos programados.

    Devuelve True si la campaña estaba activa. El motivo visible se deriva al leer la
    campaña (horario incompatible o Gmail desconectado), sin columnas nuevas.
    """
    r = c.execute(
        "UPDATE campanas SET estado='pausada' WHERE id=%s AND estado='activa' RETURNING id",
        (campana_id,),
    ).fetchone()
    c.execute(
        "UPDATE inscripciones SET estado='pausada',proximo_envio=NULL WHERE campana_id=%s AND estado='activa'",
        (campana_id,),
    )
    c.execute(
        "UPDATE mensajes m SET estado='cancelado' FROM inscripciones i WHERE m.inscripcion_id=i.id AND i.campana_id=%s AND m.estado='programado'",
        (campana_id,),
    )
    if r:
        auditar(c, accion, "campanas", str(campana_id))
    return bool(r)


def proximo_al_reanudar(
    c: Conn, ins: dict[str, Any], secuencia: dict[str, Any], global_: dict[str, Any], ahora: Any
) -> Any:
    """Conserva la espera del paso pendiente al reanudar una inscripción.

    Antes, reanudar ponía todos los seguimientos como vencidos y se enviaban de inmediato.
    """
    if ins["paso_actual"] <= 1:
        return ahora
    previo = uno(
        c,
        "SELECT max(enviado_en) AS fecha FROM mensajes WHERE inscripcion_id=%s AND estado='enviado'",
        (ins["id"],),
    )["fecha"]
    paso = next((p for p in secuencia["pasos"] if p["orden"] == ins["paso_actual"]), None)
    if not previo or not paso:
        return ahora
    try:
        objetivo = siguiente_ventana(
            previo.astimezone(BOGOTA), secuencia["ventanaEnvio"], global_, paso["esperaDiasHabiles"]
        )
    except VentanaIncompatible:
        return ahora
    return max(ahora, objetivo)
