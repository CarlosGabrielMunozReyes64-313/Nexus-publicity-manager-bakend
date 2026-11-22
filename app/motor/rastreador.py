import base64
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any

from bs4 import BeautifulSoup

from app.api import datos
from app.config import config
from app.db import Conn, Row, encolar, insertar, todos, uno


def _decodificar(p: Row) -> str:
    raw = p.get("body", {}).get("data")
    if not raw:
        return ""
    data = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    charset = "utf-8"
    for h in p.get("headers", []):
        if h.get("name", "").lower() == "content-type":
            m = re.search(r'charset="?([\w.-]+)', h.get("value", ""), re.I)
            if m:
                charset = m[1]
    try:
        return data.decode(charset, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def _partes(p: Row, tipos: tuple[str, ...]) -> list[str]:
    if p.get("mimeType") in tipos:
        texto = _decodificar(p)
        return [texto] if texto else []
    return [x for parte in p.get("parts", []) for x in _partes(parte, tipos)]


def html_a_texto(html: str) -> str:
    """Convierte HTML a texto conservando saltos de línea y sin la parte citada."""
    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "head", "title"]):
        el.decompose()
    # Respuestas citadas de Gmail, Outlook y Apple Mail.
    for el in soup.select(
        "blockquote, .gmail_quote, .gmail_extra, #appendonsend, #divRplyFwdMsg, "
        "[type=cite], .moz-cite-prefix, .yahoo_quoted"
    ):
        el.decompose()
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for bloque in soup.find_all(["p", "div", "li", "tr", "h1", "h2", "h3", "h4"]):
        bloque.insert_after("\n")
    lineas = (" ".join(x.split()) for x in soup.get_text().splitlines())
    return "\n".join(x for x in lineas if x).strip()


def texto_payload(p: Row) -> str:
    """Texto plano y partes de entrega; si el mensaje solo trae HTML, se convierte."""
    texto = _partes(p, ("text/plain", "message/delivery-status", "text/rfc822-headers"))
    if texto:
        return "\n".join(texto)
    return "\n".join(html_a_texto(h) for h in _partes(p, ("text/html",)))


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
            from app.dominio.reglas import VentanaIncompatible, siguiente_ventana

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
                try:
                    nxt = siguiente_ventana(
                        regreso,
                        ins["contenido_congelado"]["secuencia"]["ventanaEnvio"],
                        limits["ventana"],
                        espera,
                    )
                except VentanaIncompatible:
                    from app.motor.pausas import pausar_campana

                    pausar_campana(c, str(ins["campana_id"]))
                    continue
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


SOLAPAMIENTO = timedelta(hours=2)
PRESUPUESTO_SEGUNDOS = 120


def _desde_recuperacion(c: Conn, ultima: datetime | None) -> datetime | None:
    """Inicio de la recuperación sin cursor: la última sincronización correcta.

    Si nunca se sincronizó, se parte del primer envío registrado. Sin envíos no hay
    respuestas que recuperar.
    """
    primer_envio: datetime | None = uno(
        c, "SELECT min(enviado_en) AS fecha FROM mensajes WHERE estado='enviado'"
    )["fecha"]
    if primer_envio is None:
        return None
    inicio = ultima or primer_envio
    desde: datetime = max(inicio, primer_envio) - SOLAPAMIENTO
    return desde


def _ya_registrados(c: Conn, ids: list[str]) -> set[str]:
    if not ids:
        return set()
    return {
        str(r["id"])
        for r in todos(
            c,
            """SELECT gmail_message_id AS id FROM respuestas WHERE gmail_message_id=ANY(%s)
            UNION SELECT substr(id_externo,7) FROM eventos_correo WHERE id_externo=ANY(%s)""",
            (ids, ["gmail:" + x for x in ids]),
        )
    }


def _obtener(api: Any, ident: str, formato: str = "full") -> Row | None:
    from googleapiclient.errors import HttpError

    try:
        kwargs: dict[str, Any] = {"userId": "me", "id": ident, "format": formato}
        if formato == "metadata":
            kwargs["metadataHeaders"] = ["From", "Content-Type"]
        r: Row = api.users().messages().get(**kwargs).execute()
        return r
    except HttpError as exc:
        # Un mensaje borrado entre el listado y la lectura no debe detener la sincronización.
        if exc.resp.status == 404:
            return None
        raise


def sincronizar(c: Conn, api: Any, reloj: Any = None) -> bool:
    """Registra respuestas, rebotes y avisos. Devuelve True si terminó completa."""
    import time

    from psycopg.types.json import Jsonb

    inicio = time.monotonic()
    r = uno(
        c,
        "SELECT cursor_sincronizacion,ultima_sincronizacion FROM integraciones WHERE id='gmail'",
    )
    cursor = (r["cursor_sincronizacion"] or {}).get("historyId")
    # Capture boundary before enumeration so arrivals during scan are retained next time.
    next_cursor = api.users().getProfile(userId="me").execute()["historyId"]
    ids: dict[str, str | None] = {}
    token = None
    recuperacion = False
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
                    for x in h.get("messagesAdded", []):
                        ids[x["message"]["id"]] = x["message"].get("threadId")
                token = result.get("nextPageToken")
                if not token:
                    break
        except HttpError as exc:
            if exc.resp.status != 404:
                raise
            cursor = None
            token = None
    if not cursor:
        # El cursor caducó (p. ej. tras una desconexión larga): se recupera desde la
        # última sincronización correcta, con solapamiento; los duplicados se descartan
        # por identificador de Gmail.
        desde = _desde_recuperacion(c, r["ultima_sincronizacion"])
        recuperacion = desde is not None
        while desde is not None:
            result = (
                api.users()
                .messages()
                .list(
                    userId="me",
                    q=f"after:{int(desde.timestamp())} -in:sent -in:chats",
                    pageToken=token,
                    maxResults=500,
                )
                .execute()
            )
            for x in result.get("messages", []):
                ids[x["id"]] = x.get("threadId")
            token = result.get("nextPageToken")
            if not token:
                break
    pendientes = [i for i in ids if i not in _ya_registrados(c, list(ids))]
    hilos = {
        str(x["hilo"])
        for x in todos(
            c,
            "SELECT DISTINCT gmail_thread_id AS hilo FROM mensajes WHERE estado='enviado' AND gmail_thread_id IS NOT NULL",
        )
    }
    completa = True
    for ident in pendientes:
        if time.monotonic() - inicio > PRESUPUESTO_SEGUNDOS:
            # Se guarda lo procesado; el resto se recupera en la siguiente vuelta.
            completa = False
            break
        hilo = ids[ident]
        if recuperacion and hilo and hilo not in hilos:
            # Fuera de nuestros hilos solo interesan los avisos de rebote.
            meta = _obtener(api, ident, "metadata")
            if not meta:
                continue
            remitente = next(
                (
                    h["value"].lower()
                    for h in meta.get("payload", {}).get("headers", [])
                    if h["name"].lower() == "from"
                ),
                "",
            )
            if not any(x in remitente for x in ("mailer-daemon", "postmaster")):
                continue
        gm = _obtener(api, ident)
        if gm:
            registrar(c, gm)
    from app.motor.alertas import revisar

    revisar(c)
    if completa:
        c.execute(
            "UPDATE integraciones SET cursor_sincronizacion=%s,ultima_sincronizacion=now() WHERE id='gmail'",
            (Jsonb({"historyId": next_cursor}),),
        )
    return completa
