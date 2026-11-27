import random
import secrets
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast
from uuid import uuid4

from fastapi import HTTPException
from psycopg import errors

from app.api import datos, lecturas
from app.config import config
from app.db import (
    Conn,
    Row,
    actualizar,
    auditar,
    encolar,
    insertar,
    obtener,
    todos,
    transaccion,
    uno,
)
from app.dominio.reglas import BOGOTA, VentanaIncompatible, siguiente_ventana
from app.esquemas.serializar import serializar
from app.integraciones.gmail import Gmail, GmailReal, message_id, mime
from app.motor.pausas import pausar_campana
from app.motor.plantillas import render
from app.motor.rastreador import registrar
from app.publico.rutas import firma_baja


class Reloj(Protocol):
    def ahora(self) -> datetime: ...


class RelojReal:
    def ahora(self) -> datetime:
        return datetime.now(timezone.utc)


class RelojFalso:
    def __init__(self, instante: datetime) -> None:
        self.instante = instante

    def ahora(self) -> datetime:
        return self.instante


def limites(c: Conn) -> Row:
    return cast(Row, uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"])


def respondio(c: Conn, gmail: Gmail, hilo: str | None) -> bool:
    if not hilo:
        return False
    found = False
    for gm in gmail.hilo(hilo):
        found = registrar(c, gm) or found
    return found


def topes(c: Conn, ins: Row, cam: Row, ct: Row, t: datetime, lim: Row) -> bool:
    day = t.astimezone(BOGOTA).date()
    r = uno(
        c,
        """SELECT count(*) FILTER(WHERE paso_orden=1) AS nuevos,
        count(*) FILTER(WHERE paso_orden=1 AND i.campana_id=%s) AS campana,
        count(*) FILTER(WHERE split_part(destinatario::text,'@',2)=%s) AS dominio
        FROM mensajes m LEFT JOIN inscripciones i ON i.id=m.inscripcion_id
        WHERE m.tipo='secuencia' AND m.estado IN ('programado','enviando','enviado')
        AND (coalesce(m.enviado_en,m.programado_para) AT TIME ZONE 'America/Bogota')::date=%s""",
        (cam["id"], ct["correo"].split("@")[1], day),
    )
    return (
        ins["paso_actual"] == 1
        and (r["nuevos"] >= lim["topeDiarioNuevos"] or r["campana"] >= cam["tope_diario"])
    ) or bool(r["dominio"] >= lim["topePorDominio"])


def programar(reloj: Reloj | None = None, proveedor: Gmail | None = None) -> None:
    now = (reloj or RelojReal()).ahora()
    with transaccion("proceso:motor_secuencias", accion="programar") as c:
        c.execute("SELECT pg_advisory_xact_lock(70717)")
        if obtener(c, "integraciones", "gmail")["estado"] != "conectada":
            return
        gmail = proveedor or GmailReal(c)
        lim = limites(c)
        from app.motor.audiencia import incorporar_nuevos

        with c.transaction():
            incorporar_nuevos(c, now)
        for ins in todos(
            c,
            "SELECT i.* FROM inscripciones i JOIN campanas ca ON ca.id=i.campana_id WHERE i.estado='activa' AND ca.estado='activa' AND i.proximo_envio<=%s ORDER BY i.proximo_envio FOR UPDATE OF i SKIP LOCKED",
            (now,),
        ):
            # Cada inscripción en su propio punto de guardado: una campaña incompatible
            # se aísla y se pausa sin revertir lo programado para las demás.
            try:
                with c.transaction():
                    programar_inscripcion(c, ins, now, gmail, lim)
            except VentanaIncompatible:
                pausar_campana(c, str(ins["campana_id"]))


def programar_inscripcion(c: Conn, ins: Row, now: datetime, gmail: Gmail, lim: Row) -> None:
    actual = obtener(c, "inscripciones", str(ins["id"]), True)
    cam = obtener(c, "campanas", str(ins["campana_id"]), True)
    if actual["estado"] != "activa" or cam["estado"] != "activa":
        return
    ins = actual
    ct = obtener(c, "contactos", str(ins["contacto_id"]), True)
    if uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (ct["id"],))["motivo"]:
        c.execute("SELECT detener_envios(ARRAY[%s]::uuid[],%s,false)", (ct["id"], "manual"))
        return
    a = obtener(c, "aprobaciones_campana", str(cam["aprobacion_vigente_id"]))
    frozen = a["contenido_congelado"]
    seq = frozen["secuencia"]
    target = siguiente_ventana(now, seq["ventanaEnvio"], lim["ventana"])
    if topes(c, ins, cam, ct, now, lim):
        target = siguiente_ventana(
            (now.astimezone(BOGOTA) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            ),
            seq["ventanaEnvio"],
            lim["ventana"],
        )
    if target > now:
        actualizar(c, "inscripciones", str(ins["id"]), {"proximo_envio": target})
        return
    if uno(
        c,
        "SELECT id FROM mensajes WHERE inscripcion_id=%s AND paso_orden=%s AND estado<>'cancelado'",
        (ins["id"], ins["paso_actual"]),
        False,
    ):
        return
    step = next((p for p in seq["pasos"] if p["orden"] == ins["paso_actual"]), None)
    if not step:
        actualizar(
            c,
            "inscripciones",
            str(ins["id"]),
            {
                "estado": "finalizada",
                "motivo_salida": "completada",
                "finalizada_en": now,
                "proximo_envio": None,
            },
        )
        return
    previous = uno(
        c,
        "SELECT * FROM mensajes WHERE inscripcion_id=%s AND estado='enviado' ORDER BY paso_orden DESC LIMIT 1",
        (ins["id"],),
        False,
    )
    hilo = previous.get("gmail_thread_id") if step["mismoHilo"] else None
    if respondio(c, gmail, hilo):
        return
    key = f"{ins['id']}:{ins['paso_actual']}:{a['id']}"
    canceled = uno(
        c,
        "SELECT id FROM mensajes WHERE clave_idempotencia=%s AND estado='cancelado'",
        (key,),
        False,
    )
    mid = str(canceled["id"]) if canceled else str(uuid4())
    ref = secrets.token_urlsafe(24)
    baja = config().url_baja(firma_baja().dumps(mid))
    e = serializar(lecturas.empresa(c, obtener(c, "empresas", str(ct["empresa_id"]))))
    rendered = render(
        seq,
        serializar(lecturas.contacto(c, ct)),
        e,
        frozen["marca"],
        baja,
        config().app_url + "/r/" + ref,
    )
    rendered_step = next(p for p in rendered["pasos"] if p["orden"] == step["orden"])
    last = uno(
        c,
        "SELECT max(coalesce(enviado_en,programado_para)) AS fecha FROM mensajes WHERE estado IN ('programado','enviando','enviado')",
    )["fecha"]
    target = max(now, last or now) + timedelta(seconds=random.randint(*lim["separacionSegundos"]))
    target = siguiente_ventana(target, seq["ventanaEnvio"], lim["ventana"])
    # Do not reserve tomorrow's quota from today's batch.
    if target.astimezone(BOGOTA).date() != now.astimezone(BOGOTA).date():
        actualizar(c, "inscripciones", str(ins["id"]), {"proximo_envio": target})
        return
    asunto = rendered_step.get("asunto")
    if hilo:
        asunto = "Re: " + (previous.get("asunto") or "").removeprefix("Re: ")
    mensaje_values = {
        "id": mid,
        "tipo": "secuencia",
        "contacto_id": ct["id"],
        "inscripcion_id": ins["id"],
        "aprobacion_id": a["id"],
        "paso_orden": step["orden"],
        "clave_idempotencia": key,
        "destinatario": ct["correo"],
        "asunto": asunto,
        "cuerpo": rendered_step["cuerpo"],
        # Texto con formato: el HTML personalizado se guarda para enviarlo como alternativa.
        "cuerpo_html": rendered_step.get("cuerpoHtml") if step.get("formato") == "html" else None,
        "programado_para": target,
        "gmail_thread_id": hilo,
    }
    mensaje_values["estado"] = "programado"
    if canceled:
        mensaje_values.pop("id")
        actualizar(c, "mensajes", mid, mensaje_values)
    else:
        insertar(c, "mensajes", mensaje_values)
    insertar(
        c,
        "enlaces_seguimiento",
        {
            "referencia": ref,
            "mensaje_id": mid,
            "url_destino": config().diagnostico_url,
            "utm_campaign": "cam-" + str(cam["id"])[:8],
            "utm_content": "paso-" + str(step["orden"]),
        },
    )
    encolar(c, "enviar_mensaje", {"mensajeId": mid}, "enviar:" + mid, 2)
    c.execute(
        "UPDATE tareas SET ejecutar_desde=%s,estado='pendiente',terminada_en=NULL,intentos=0,tomada_por=NULL,tomada_en=NULL WHERE clave_unica=%s AND estado<>'en_proceso'",
        (target, "enviar:" + mid),
    )


def enviar(ident: str, reloj: Reloj | None = None, proveedor: Gmail | None = None) -> None:
    from app import db

    assert db.pool is not None
    now = (reloj or RelojReal()).ahora()
    # Session advisory lock spans the provider call and its two transactions.
    with db.pool.connection() as c:
        c.execute("SELECT pg_advisory_lock(70719)")
        c.execute("SELECT pg_advisory_lock(hashtextextended(%s,1))", (ident,))
        now = (reloj or RelojReal()).ahora()
        c.commit()
        try:
            with c.transaction():
                c.execute("SELECT set_config('nexus.actor','proceso:motor_secuencias',true)")
                m = obtener(c, "mensajes", ident, True)
                if m["estado"] in ("enviado", "cancelado", "fallido") or m["programado_para"] > now:
                    return
                gmail = proveedor or GmailReal(c)
                recovered = (
                    gmail.buscar(m["clave_idempotencia"]) if m["estado"] == "enviando" else None
                )
                if m["estado"] == "enviando" and recovered is None:
                    # Gmail pudo aceptar el correo antes de que el proceso se cayera y su
                    # búsqueda tarda en reflejarlo: una ausencia no prueba que no salió.
                    # Nunca se reenvía a ciegas; queda para conciliación (automática con
                    # «conciliar_envios» o manual desde la secuencia).
                    marcar_conciliacion(c, m)
                    return
                if not recovered:
                    lim = limites(c)
                    last_sent = uno(
                        c, "SELECT max(enviado_en) AS fecha FROM mensajes WHERE estado='enviado'"
                    )["fecha"]
                    if last_sent:
                        next_allowed = last_sent + timedelta(
                            seconds=random.randint(*lim["separacionSegundos"])
                        )
                        if next_allowed > now:
                            actualizar(c, "mensajes", ident, {"programado_para": next_allowed})
                            return
                    if m["tipo"] == "secuencia":
                        approval = obtener(c, "aprobaciones_campana", str(m["aprobacion_id"]))
                        frozen_seq = approval["contenido_congelado"]["secuencia"]
                        try:
                            valid_at = siguiente_ventana(
                                now, frozen_seq["ventanaEnvio"], lim["ventana"]
                            )
                        except VentanaIncompatible:
                            # Aísla la campaña: cancela lo programado y no envía este mensaje.
                            pausar_campana(c, str(approval["campana_id"]))
                            return
                        counts = uno(
                            c,
                            """SELECT count(*) FILTER(WHERE paso_orden=1) AS nuevos,
                            count(*) FILTER(WHERE paso_orden=1 AND i.campana_id=%s) AS campana,
                            count(*) FILTER(WHERE split_part(destinatario::text,'@',2)=%s) AS dominio
                            FROM mensajes m JOIN inscripciones i ON i.id=m.inscripcion_id WHERE m.tipo='secuencia' AND m.estado='enviado'
                            AND (enviado_en AT TIME ZONE 'America/Bogota')::date=%s""",
                            (
                                approval["campana_id"],
                                m["destinatario"].split("@")[1],
                                now.astimezone(BOGOTA).date(),
                            ),
                        )
                        cam = obtener(c, "campanas", str(approval["campana_id"]))
                        if (
                            m["paso_orden"] == 1
                            and (
                                counts["nuevos"] >= lim["topeDiarioNuevos"]
                                or counts["campana"] >= cam["tope_diario"]
                            )
                        ) or counts["dominio"] >= lim["topePorDominio"]:
                            try:
                                valid_at = siguiente_ventana(
                                    (now.astimezone(BOGOTA) + timedelta(days=1)).replace(
                                        hour=0, minute=0, second=0, microsecond=0
                                    ),
                                    frozen_seq["ventanaEnvio"],
                                    lim["ventana"],
                                )
                            except VentanaIncompatible:
                                pausar_campana(c, str(approval["campana_id"]))
                                return
                        if valid_at > now:
                            actualizar(c, "mensajes", ident, {"programado_para": valid_at})
                            return
                    if m["tipo"] == "secuencia" and respondio(c, gmail, m["gmail_thread_id"]):
                        return
                    try:
                        with c.transaction():
                            actualizar(
                                c,
                                "mensajes",
                                ident,
                                {"estado": "enviando", "intentos": m["intentos"] + 1},
                            )
                    except errors.CheckViolation:
                        actualizar(c, "mensajes", ident, {"estado": "cancelado"})
                        return
                marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
                prev = None
                if m["gmail_thread_id"]:
                    if m["tipo"] == "respuesta_manual":
                        gm = gmail.hilo(m["gmail_thread_id"])
                        target = obtener(c, "respuestas", str(m["en_respuesta_a"]))[
                            "gmail_message_id"
                        ]
                        prev = next(
                            (
                                h["value"]
                                for x in gm
                                if x["id"] == target
                                for h in x.get("payload", {}).get("headers", [])
                                if h["name"].lower() == "message-id"
                            ),
                            None,
                        )
                    else:
                        prevrow = uno(
                            c,
                            "SELECT clave_idempotencia FROM mensajes WHERE inscripcion_id=%s AND estado='enviado' ORDER BY paso_orden DESC LIMIT 1",
                            (m["inscripcion_id"],),
                            False,
                        )
                        if prevrow:
                            prev = message_id(prevrow["clave_idempotencia"])
                em = mime(m, marca, config().url_baja(firma_baja().dumps(ident)), prev)
            if not recovered and not puede_entregarse(c, m):
                return
            try:
                result = recovered or gmail.enviar(em, m["gmail_thread_id"])
            except Exception as exc:
                from googleapiclient.errors import HttpError

                if isinstance(exc, HttpError):
                    with c.transaction():
                        c.execute(
                            "SELECT set_config('nexus.actor','proceso:motor_secuencias',true)"
                        )
                        if exc.resp.status == 429:
                            try:
                                with c.transaction():
                                    actualizar(
                                        c,
                                        "mensajes",
                                        ident,
                                        {
                                            "estado": "programado",
                                            "ultimo_error": "Proveedor limitó la frecuencia",
                                        },
                                    )
                            except errors.CheckViolation:
                                actualizar(c, "mensajes", ident, {"estado": "cancelado"})
                        elif exc.resp.status == 401:
                            actualizar(
                                c,
                                "mensajes",
                                ident,
                                {
                                    "estado": "cancelado",
                                    "ultimo_error": "Renueve la autorización de Gmail",
                                },
                            )
                        elif exc.resp.status in (400, 403, 404):
                            from app.integraciones.errores_google import explicar

                            actualizar(
                                c,
                                "mensajes",
                                ident,
                                {
                                    "estado": "fallido",
                                    "ultimo_error": (
                                        explicar(exc, "Gmail")
                                        or "Gmail rechazó el mensaje; revise destinatario y permisos"
                                    )[:300],
                                },
                            )
                            detail = str(exc).lower()
                            if exc.resp.status == 400 and (
                                "recipient" in detail or "invalid to" in detail
                            ):
                                actualizar(
                                    c,
                                    "contactos",
                                    str(m["contacto_id"]),
                                    {"estado_correo": "invalido"},
                                )
                raise

            with c.transaction():
                c.execute("SELECT set_config('nexus.actor','proceso:motor_secuencias',true)")
                registrar_enviado(c, m, result, now)
        finally:
            c.rollback()
            c.execute("SELECT pg_advisory_unlock(hashtextextended(%s,1))", (ident,))
            c.execute("SELECT pg_advisory_unlock(70719)")
            c.commit()


def registrar_enviado(c: Conn, m: Row, result: dict[str, str], now: datetime) -> None:
    """Gmail aceptó el mensaje: se guarda como enviado y la inscripción pasa al paso
    siguiente según su espera en días hábiles, ventana y horario."""
    ident = str(m["id"])
    actualizar(
        c,
        "mensajes",
        ident,
        {
            "estado": "enviado",
            "enviado_en": now,
            "gmail_message_id": result["id"],
            "gmail_thread_id": result["threadId"],
            "ultimo_error": None,
            "conciliacion": "enviado" if m.get("conciliacion") == "pendiente" else None,
        },
    )
    if m["tipo"] == "secuencia":
        ins = obtener(c, "inscripciones", str(m["inscripcion_id"]), True)
        frozen = obtener(c, "aprobaciones_campana", str(m["aprobacion_id"]))["contenido_congelado"]
        seq = frozen["secuencia"]
        nextstep = next((p for p in seq["pasos"] if p["orden"] == m["paso_orden"] + 1), None)
        if ins["estado"] == "activa" and ins["paso_actual"] == m["paso_orden"]:
            if nextstep:
                try:
                    nxt = siguiente_ventana(
                        now,
                        seq["ventanaEnvio"],
                        limites(c)["ventana"],
                        nextstep["esperaDiasHabiles"],
                    )
                    actualizar(
                        c,
                        "inscripciones",
                        str(ins["id"]),
                        {"paso_actual": nextstep["orden"], "proximo_envio": nxt},
                    )
                except VentanaIncompatible:
                    # El correo ya salió: se registra como enviado y solo se pausa
                    # la campaña, conservando el paso siguiente para reanudar.
                    actualizar(
                        c,
                        "inscripciones",
                        str(ins["id"]),
                        {
                            "paso_actual": nextstep["orden"],
                            "proximo_envio": None,
                            "estado": "pausada",
                        },
                    )
                    pausar_campana(c, str(ins["campana_id"]))
            else:
                actualizar(
                    c,
                    "inscripciones",
                    str(ins["id"]),
                    {
                        "estado": "finalizada",
                        "motivo_salida": "completada",
                        "finalizada_en": now,
                        "proximo_envio": None,
                    },
                )
        elif ins["paso_actual"] == m["paso_orden"] and ins["estado"] == "pausada" and nextstep:
            # Pausada mientras salía: conserva el avance para reanudar desde el paso siguiente.
            actualizar(c, "inscripciones", str(ins["id"]), {"paso_actual": nextstep["orden"]})
        datos.avanzar(c, m["contacto_id"], "contactado", "primer_envio")
    auditar(c, "correo_enviado", "mensajes", ident)


CONCILIAR = "Resultado incierto: Gmail pudo aceptarlo; requiere conciliación (no se reenvía solo)"


def marcar_conciliacion(c: Conn, m: Row) -> None:
    ident = str(m["id"])
    if m.get("conciliacion") == "pendiente":
        return
    actualizar(c, "mensajes", ident, {"ultimo_error": CONCILIAR, "conciliacion": "pendiente"})
    auditar(c, "envio_incierto", "mensajes", ident)
    from app.notificaciones import notificar_responsables

    notificar_responsables(
        c,
        "tarea_fallida",
        "Envío con resultado incierto",
        "Un correo quedó sin confirmación de Gmail. NEXUS no lo reenviará solo: lo buscará "
        "de nuevo en Gmail y, si no aparece, decida en la secuencia si salió o no.",
        "/secuencias",
        f"envio_incierto:{ident}",
    )


def puede_entregarse(c: Conn, m: Row) -> bool:
    """Última comprobación inmediatamente antes de entregar el mensaje a Gmail.

    Lee la supresión y la elegibilidad con una consulta nueva (ya confirmado «enviando»):
    una baja registrada en ese instante cancela el envío en lugar de dejarlo salir.
    """
    with c.transaction():
        c.execute("SELECT set_config('nexus.actor','proceso:motor_secuencias',true)")
        if m["tipo"] == "secuencia":
            motivo = uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (m["contacto_id"],))[
                "motivo"
            ]
        else:
            motivo = (
                "Contacto suprimido o excluido"
                if uno(c, "SELECT contacto_bloqueado(%s) AS b", (m["contacto_id"],))["b"]
                else None
            )
        if motivo:
            c.execute(
                "UPDATE mensajes SET estado='cancelado',ultimo_error=%s WHERE id=%s",
                (("Cancelado antes de entregar a Gmail: " + motivo)[:300], m["id"]),
            )
            return False
    return True


def conciliar_pendientes(proveedor: Gmail | None = None, reloj: Reloj | None = None) -> int:
    """Busca otra vez en Gmail los envíos inciertos (tarea «conciliar_envios»).

    Si Gmail tiene el mensaje (por Message-ID o encabezado de idempotencia) se registra
    como enviado. Si no, sigue pendiente para la decisión manual; nunca se reenvía solo.
    """
    now = (reloj or RelojReal()).ahora()
    resueltos = 0
    with transaccion("proceso:motor_secuencias", accion="conciliar_envios") as c:
        if obtener(c, "integraciones", "gmail")["estado"] != "conectada":
            return 0
        gmail = proveedor or GmailReal(c)
        for m in todos(
            c,
            "SELECT * FROM mensajes WHERE estado='enviando' AND conciliacion='pendiente' "
            "ORDER BY creado_en LIMIT 50 FOR UPDATE SKIP LOCKED",
        ):
            encontrado = gmail.buscar(m["clave_idempotencia"])
            if encontrado:
                with c.transaction():
                    registrar_enviado(c, m, encontrado, now)
                resueltos += 1
    return resueltos


def conciliar_manual(c: Conn, ident: str, resultado: str) -> dict[str, object]:
    """Decisión del administrador sobre un envío incierto.

    - «enviado»: lo encontró en Enviados de Gmail; se registra sin reenviar.
    - «no_enviado»: confirmó que no salió; se cancela y la inscripción vuelve a programarse
      con la misma clave de idempotencia (mismo Message-ID).
    """
    m = obtener(c, "mensajes", ident, True)
    if m["estado"] != "enviando" or m.get("conciliacion") != "pendiente":
        raise HTTPException(409, "Este mensaje no está pendiente de conciliación")
    if resultado == "enviado":
        registrar_enviado(
            c,
            m,
            {"id": "conciliado-manual:" + ident, "threadId": m["gmail_thread_id"] or ""},
            datos.ahora(),
        )
        actualizar(c, "mensajes", ident, {"conciliacion": "enviado"})
    elif resultado == "no_enviado":
        c.execute(
            "UPDATE mensajes SET estado='cancelado',conciliacion='no_enviado',"
            "ultimo_error='Conciliado: no salió; se programará de nuevo' WHERE id=%s",
            (ident,),
        )
        if m["tipo"] == "secuencia":
            c.execute(
                "UPDATE inscripciones SET proximo_envio=%s WHERE id=%s AND estado='activa'",
                (datos.ahora(), m["inscripcion_id"]),
            )
    else:
        raise HTTPException(400, "Indique si el correo salió (enviado) o no (no_enviado)")
    auditar(c, "conciliar_envio_" + resultado, "mensajes", ident)
    return {"ok": True}
