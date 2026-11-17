import base64
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any

from app.api import datos
from app.config import config
from app.db import Conn, Row, encolar, insertar, todos, uno


def texto_payload(p: Row) -> str:
    if p.get("mimeType") in (
        "text/plain",
        "message/delivery-status",
        "text/rfc822-headers",
    ) and p.get("body", {}).get("data"):
        raw = p["body"]["data"]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode(
            "utf-8", errors="replace"
        )
    return "\n".join(texto_payload(x) for x in p.get("parts", []))


def limpiar(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if line.startswith(">") or re.match(r"^(On .+wrote:|El .+escribió:|De:|From:)", line):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def registrar(c: Conn, gm: Row) -> bool:
    headers = {h["name"].lower(): h["value"] for h in gm.get("payload", {}).get("headers", [])}
    sender = parseaddr(headers.get("from", ""))[1].lower()
    if sender == config().gmail_cuenta_envio.lower() or "SENT" in gm.get("labelIds", []):
        return False
    original = uno(
        c,
        "SELECT m.*,i.campana_id FROM mensajes m LEFT JOIN inscripciones i ON i.id=m.inscripcion_id WHERE m.gmail_thread_id=%s AND m.estado='enviado' ORDER BY m.enviado_en DESC LIMIT 1",
        (gm["threadId"],),
        False,
    )
    if not original:

        def encontrar_clave(part: Row) -> str | None:
            for header in part.get("headers", []):
                if header["name"].lower() == "x-nexus-idempotencia":
                    return str(header["value"])
            for child in part.get("parts", []):
                key = encontrar_clave(child)
                if key:
                    return key
            return None

        clave = encontrar_clave(gm.get("payload", {}))
        if clave:
            original = uno(
                c,
                "SELECT m.*,i.campana_id FROM mensajes m LEFT JOIN inscripciones i ON i.id=m.inscripcion_id WHERE m.clave_idempotencia=%s AND m.estado='enviado'",
                (clave,),
                False,
            )
        if not original:
            return False
    cid = original["contacto_id"]
    texto = limpiar(texto_payload(gm.get("payload", {})))
    is_bounce = any(
        x in sender for x in ("mailer-daemon", "postmaster")
    ) or "delivery-status" in headers.get("content-type", "")
    auto = (
        headers.get("auto-submitted", "no") != "no"
        or any(k in headers for k in ("x-autoreply", "x-autorespond"))
        or headers.get("precedence") == "auto_reply"
        or any(
            x in headers.get("subject", "").lower()
            for x in ("fuera de oficina", "out of office", "automatic reply")
        )
    )
    if is_bounce or auto:
        tipo = (
            "rebote_duro"
            if is_bounce and re.search(r"\b5\.\d\.\d", texto_payload(gm.get("payload", {})))
            else "rebote_suave"
            if is_bounce
            else "fuera_oficina"
        )
        inserted = c.execute(
            "INSERT INTO eventos_correo(tipo,mensaje_id,contacto_id,id_externo) VALUES(%s,%s,%s,%s) ON CONFLICT(id_externo) DO NOTHING RETURNING id",
            (tipo, original["id"], cid, "gmail:" + gm["id"]),
        ).fetchone()
        if not inserted:
            return False
        if tipo == "fuera_oficina":
            from app.dominio.reglas import siguiente_ventana

            for ins in todos(
                c,
                "SELECT i.*,a.contenido_congelado FROM inscripciones i JOIN campanas ca ON ca.id=i.campana_id JOIN aprobaciones_campana a ON a.id=ca.aprobacion_vigente_id WHERE i.contacto_id=%s AND i.estado='activa'",
                (cid,),
            ):
                limits = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
                regreso = datos.ahora()
                espera = 3
                iso = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", texto)
                if iso:
                    try:
                        parsed = datetime.fromisoformat(iso[1]).replace(
                            tzinfo=regreso.tzinfo, hour=8
                        )
                        if regreso < parsed and (parsed - regreso).days < 366:
                            regreso = parsed
                            espera = 0
                    except ValueError:
                        pass
                nxt = siguiente_ventana(
                    regreso,
                    ins["contenido_congelado"]["secuencia"]["ventanaEnvio"],
                    limits["ventana"],
                    espera,
                )
                c.execute("UPDATE inscripciones SET proximo_envio=%s WHERE id=%s", (nxt, ins["id"]))
                c.execute(
                    "UPDATE mensajes SET estado='cancelado' WHERE inscripcion_id=%s AND estado='programado'",
                    (ins["id"],),
                )
        elif tipo == "rebote_suave":
            c.execute("SELECT detener_envios(ARRAY[%s]::uuid[],%s,false)", (cid, "rebote"))
        return True
    if sender != original["destinatario"].lower():
        return False
    if uno(c, "SELECT id FROM respuestas WHERE gmail_message_id=%s", (gm["id"],), False):
        return True
    r = insertar(
        c,
        "respuestas",
        {
            "contacto_id": cid,
            "campana_id": original["campana_id"],
            "inscripcion_id": original["inscripcion_id"],
            "mensaje_origen_id": original["id"],
            "gmail_message_id": gm["id"],
            "gmail_thread_id": gm["threadId"],
            "remitente": sender,
            "recibida_en": datetime.fromtimestamp(
                int(gm.get("internalDate", 0)) / 1000, timezone.utc
            )
            if gm.get("internalDate")
            else datos.ahora(),
            "asunto": headers.get("subject"),
            "texto": texto,
        },
    )
    norm = "".join(
        x for x in unicodedata.normalize("NFD", texto.lower()) if not unicodedata.combining(x)
    )
    if norm.strip() == "baja":
        datos.suprimir(c, {"correo": sender, "motivo": "Baja solicitada"}, "respuesta")
        c.execute(
            "UPDATE respuestas SET clasificacion='baja',confianza=1,estado='atendida',atendida_en=now() WHERE id=%s",
            (r["id"],),
        )
    else:
        encolar(
            c,
            "clasificar_respuesta",
            {"respuestaId": str(r["id"])},
            "clasificar:" + str(r["id"]),
            3,
        )
    return True


def sincronizar(c: Conn, api: Any) -> None:
    from psycopg.types.json import Jsonb

    r = uno(c, "SELECT cursor_sincronizacion FROM integraciones WHERE id='gmail'")
    cursor = (r["cursor_sincronizacion"] or {}).get("historyId")
    # Capture boundary before enumeration so arrivals during scan are retained next time.
    next_cursor = api.users().getProfile(userId="me").execute()["historyId"]
    ids: set[str] = set()
    token = None
    if cursor:
        from googleapiclient.errors import HttpError

        try:
            while True:
                result = (
                    api.users()
                    .history()
                    .list(
                        userId="me",
                        startHistoryId=cursor,
                        historyTypes=["messageAdded"],
                        pageToken=token,
                    )
                    .execute()
                )
                for h in result.get("history", []):
                    ids.update(x["message"]["id"] for x in h.get("messagesAdded", []))
                token = result.get("nextPageToken")
                if not token:
                    break
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            cursor = None
    if not cursor:
        while True:
            result = (
                api.users()
                .messages()
                .list(userId="me", q="newer_than:7d -in:sent", pageToken=token, maxResults=500)
                .execute()
            )
            ids.update(x["id"] for x in result.get("messages", []))
            token = result.get("nextPageToken")
            if not token:
                break
    for ident in ids:
        registrar(c, api.users().messages().get(userId="me", id=ident, format="full").execute())
    from app.motor.alertas import revisar

    revisar(c)
    c.execute(
        "UPDATE integraciones SET cursor_sincronizacion=%s,ultima_sincronizacion=now() WHERE id='gmail'",
        (Jsonb({"historyId": next_cursor}),),
    )
