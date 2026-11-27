"""Estado de envíos de una secuencia: qué se sabe, por qué y qué acción corresponde.

GET /secuencias/:id/envios. Distingue, por secuencia y por destinatario:
aprobada → programada → entregándose → aceptada por Gmail, además de pausada, fallida,
cancelada y «por conciliar». «Aceptado por Gmail» solo aparece con la respuesta del
proveedor guardada (gmail_message_id), nunca por haber programado o intentado.
"""

from datetime import timedelta
from typing import Any

from app.api import datos
from app.db import Conn, Row, todos, uno
from app.dominio.reglas import BOGOTA
from app.motor import audiencia

LATIDO_MAX = timedelta(minutes=2)
SALIDAS = {
    "completada": "Secuencia completada",
    "respondio": "Respondió: secuencia detenida",
    "baja": "Se dio de baja",
    "rebote": "Rebote: correo inválido",
    "reunion": "Agendó reunión",
    "cliente": "Empresa cliente",
    "manual": "Detenida manualmente",
}


def estado_destinatario(ins: Row) -> tuple[str, str]:
    """(clave, texto) visibles para un destinatario."""
    m_estado = ins.get("m_estado")
    if ins["estado"] == "finalizada":
        if ins["motivo_salida"] == "completada" and m_estado == "enviado":
            return "completada", "Aceptado por Gmail · secuencia completada"
        return "finalizada", SALIDAS.get(ins["motivo_salida"] or "", "Finalizada")
    if m_estado == "enviando":
        if ins.get("m_conciliacion") == "pendiente":
            return "conciliar", "Por conciliar: Gmail pudo aceptarlo"
        return "entregando", "Entregándose a Gmail"
    if m_estado == "fallido":
        return "fallido", "Fallido: " + (ins.get("m_error") or "Gmail rechazó el mensaje")
    if ins["estado"] == "pausada":
        if ins["motivo_salida"] == "respondio":
            return "pausada", "Pausado: respondió"
        return "pausada", "Pausado"
    if m_estado == "programado":
        return "programado", f"Programado (correo {ins['m_paso']})"
    if m_estado == "enviado" and ins["proximo_envio"]:
        return "aceptado", f"Aceptado por Gmail (correo {ins['m_paso']}); siguiente programado"
    if m_estado == "enviado":
        return "aceptado", f"Aceptado por Gmail (correo {ins['m_paso']})"
    if ins["estado"] == "activa":
        return "programado", f"Programado (correo {ins['paso_actual']})"
    return "pendiente", "Sin programar"


def envios(c: Conn, cam: Row | None) -> dict[str, Any]:
    ahora = datos.ahora()
    if not cam:
        return {"aprobada": False, "diagnostico": [], "destinatarios": []}
    aud = audiencia.describir(c, cam)
    cand = audiencia.candidatos(c, cam)
    eleg = audiencia.filtrar_elegibles(c, cand)
    excluidos = audiencia.motivos(c, [x for x in cand if x not in set(eleg)])
    gmail = uno(
        c, "SELECT estado,detalle,cuenta FROM integraciones WHERE id='gmail'", (), False
    ) or {"estado": "desconectada"}
    latido = uno(c, "SELECT max(visto_en) AS visto FROM latidos_trabajador", (), False).get("visto")
    lim = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    hoy = ahora.astimezone(BOGOTA).date()
    usados = uno(
        c,
        """SELECT count(*) FILTER (WHERE m.paso_orden=1) AS nuevos,
                  count(*) FILTER (WHERE m.paso_orden=1 AND i.campana_id=%s) AS campana
           FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id
           WHERE m.tipo='secuencia' AND m.estado IN ('programado','enviando','enviado')
             AND (coalesce(m.enviado_en,m.programado_para) AT TIME ZONE 'America/Bogota')::date=%s""",
        (cam["id"], hoy),
    )
    por_estado = {
        r["estado"]: r["n"]
        for r in todos(
            c,
            """SELECT m.estado, count(*) AS n FROM mensajes m JOIN inscripciones i
               ON i.id=m.inscripcion_id WHERE i.campana_id=%s AND m.tipo='secuencia'
               GROUP BY m.estado""",
            (cam["id"],),
        )
    }
    por_paso = todos(
        c,
        """SELECT m.paso_orden AS paso,
                  count(*) FILTER (WHERE m.estado='programado') AS programados,
                  count(*) FILTER (WHERE m.estado='enviando') AS entregando,
                  count(*) FILTER (WHERE m.estado='enviado') AS aceptados,
                  count(*) FILTER (WHERE m.estado='fallido') AS fallidos,
                  count(*) FILTER (WHERE m.estado='cancelado') AS cancelados
           FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id
           WHERE i.campana_id=%s AND m.tipo='secuencia' GROUP BY m.paso_orden ORDER BY 1""",
        (cam["id"],),
    )
    inscripciones = {
        r["estado"]: r["n"]
        for r in todos(
            c,
            "SELECT estado, count(*) AS n FROM inscripciones WHERE campana_id=%s GROUP BY estado",
            (cam["id"],),
        )
    }
    filas = todos(
        c,
        """SELECT i.id, i.contacto_id, i.estado, i.paso_actual, i.proximo_envio, i.motivo_salida,
                  ct.nombre, ct.correo::text AS correo,
                  m.id AS m_id, m.estado AS m_estado, m.paso_orden AS m_paso,
                  m.programado_para AS m_programado, m.enviado_en AS m_enviado,
                  m.ultimo_error AS m_error, m.conciliacion AS m_conciliacion
           FROM inscripciones i JOIN contactos ct ON ct.id=i.contacto_id
           LEFT JOIN LATERAL (SELECT * FROM mensajes x WHERE x.inscripcion_id=i.id
                              AND x.estado<>'cancelado' ORDER BY x.paso_orden DESC LIMIT 1) m ON true
           WHERE i.campana_id=%s ORDER BY ct.nombre, i.id LIMIT 500""",
        (cam["id"],),
    )
    destinatarios = []
    for f in filas:
        clave, texto = estado_destinatario(f)
        destinatarios.append(
            {
                "inscripcionId": f["id"],
                "contactoId": f["contacto_id"],
                "nombre": f["nombre"],
                "correo": f["correo"],
                "estado": clave,
                "texto": texto,
                "pasoActual": f["paso_actual"],
                "proximoEnvio": f["m_programado"]
                if f["m_estado"] == "programado"
                else f["proximo_envio"],
                "enviadoEn": f["m_enviado"],
                "mensajeId": f["m_id"],
                "error": f["m_error"] if f["m_estado"] in ("fallido", "enviando") else None,
            }
        )
    proximo = uno(
        c,
        """SELECT min(t) AS t FROM (
             SELECT min(m.programado_para) AS t FROM mensajes m JOIN inscripciones i
               ON i.id=m.inscripcion_id WHERE i.campana_id=%s AND m.estado='programado'
             UNION ALL SELECT min(proximo_envio) FROM inscripciones
               WHERE campana_id=%s AND estado='activa') x""",
        (cam["id"], cam["id"]),
    )["t"]
    conciliar = [d for d in destinatarios if d["estado"] == "conciliar"]
    trabajador_vivo = bool(latido and ahora - latido <= LATIDO_MAX)
    aprobada = bool(cam.get("aprobacion_vigente_id"))
    diag: list[dict[str, str]] = []

    def aviso(nivel: str, texto: str, accion: str = "") -> None:
        diag.append({"nivel": nivel, "texto": texto, "accion": accion})

    if not aprobada:
        aviso("info", "Aún no hay una versión aprobada: no se programa ningún envío.", "aprobar")
    elif cam["estado"] in ("aprobada", "pausada") and cam.get("motivo_no_programada"):
        aviso("error", cam["motivo_no_programada"], "programar")
    elif cam["estado"] == "aprobada":
        aviso("aviso", "Aprobada pero sin programar.", "programar")
    if gmail["estado"] != "conectada":
        aviso(
            "error",
            "Gmail no está conectado: nada puede salir. Conéctelo en Configuración → "
            "Integraciones y luego pulse «Programar envíos».",
            "configuracion",
        )
    elif cam["estado"] == "pausada" and (gmail.get("detalle") or "").startswith(
        ("Envíos en pausa", "Gmail requiere")
    ):
        aviso(
            "error",
            f"Campaña pausada automáticamente: {gmail['detalle']}. Resuélvalo y pulse "
            "«Programar envíos».",
            "programar",
        )
    elif cam["estado"] == "pausada":
        aviso("aviso", "La campaña está en pausa: no sale nada hasta reanudarla.", "programar")
    if not trabajador_vivo:
        aviso(
            "error",
            "El trabajador no da señales"
            + (f" desde {latido.astimezone(BOGOTA):%d/%m %H:%M}" if latido else "")
            + ": la API responde, pero nadie ejecuta los envíos programados. Arranque el "
            "proceso «trabajador» (python -m app.tareas.trabajador; en Railway, honcho start "
            "o un servicio aparte).",
            "trabajador",
        )
    if not eleg:
        detalle = ", ".join(f"{k}: {v}" for k, v in sorted(excluidos.items()))
        aviso(
            "aviso",
            "Cero destinatarios elegibles en la audiencia"
            + (f" ({detalle})" if detalle else "")
            + ". Revise base legal, verificación del correo y bajas.",
            "contactos",
        )
    if usados["nuevos"] >= lim["topeDiarioNuevos"] or usados["campana"] >= cam["tope_diario"]:
        aviso(
            "aviso",
            "Cuota diaria de primeros correos agotada "
            f"({usados['nuevos']}/{lim['topeDiarioNuevos']} global, "
            f"{usados['campana']}/{cam['tope_diario']} de esta campaña): el resto sale en la "
            "siguiente ventana hábil.",
        )
    if conciliar:
        aviso(
            "aviso",
            f"{len(conciliar)} envío(s) sin confirmación de Gmail. No se reenvían solos: "
            "búsquelos en Enviados y marque si salieron.",
            "conciliar",
        )
    if por_estado.get("fallido"):
        ultimo = next((d["error"] for d in destinatarios if d["estado"] == "fallido"), "")
        aviso("error", f"{por_estado['fallido']} correo(s) fallido(s). {ultimo or ''}".strip())
    if cam["estado"] == "activa" and proximo and proximo > ahora and gmail["estado"] == "conectada":
        aviso(
            "info",
            f"Próximo envío previsto: {proximo.astimezone(BOGOTA):%d/%m %H:%M} (hora de "
            "Colombia), según ventana, cuotas y separación.",
        )
    return {
        "aprobada": aprobada,
        "campanaId": cam["id"],
        "campanaEstado": cam["estado"],
        "programadaEn": cam.get("programada_en"),
        "motivoNoProgramada": cam.get("motivo_no_programada"),
        "audiencia": {
            "tipo": aud["tipo"],
            "id": aud["id"],
            "nombre": aud["nombre"],
            "incorporarNuevos": aud["incorporarNuevos"],
            "candidatos": len(cand),
            "elegibles": len(eleg),
            "noElegibles": excluidos,
        },
        "gmail": {"estado": gmail["estado"], "detalle": gmail.get("detalle")},
        "trabajador": {"vivo": trabajador_vivo, "ultimoLatido": latido},
        "cuota": {
            "nuevosHoy": usados["nuevos"],
            "topeGlobal": lim["topeDiarioNuevos"],
            "campanaHoy": usados["campana"],
            "topeCampana": cam["tope_diario"],
        },
        "inscripciones": inscripciones,
        "mensajes": por_estado,
        "porPaso": por_paso,
        "proximoEnvio": proximo,
        "diagnostico": diag,
        "destinatarios": destinatarios,
    }
