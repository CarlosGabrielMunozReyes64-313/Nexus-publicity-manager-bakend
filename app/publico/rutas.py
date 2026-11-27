import hashlib
import hmac
import time
from collections import defaultdict, deque
from html import escape
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from itsdangerous import BadSignature, URLSafeSerializer
from psycopg.types.json import Jsonb

from app.api import datos
from app.config import config
from app.db import Conn, Row, actualizar, auditar, encolar, insertar, obtener, transaccion, uno
from app.dominio.reglas import normalizar_nombre

router = APIRouter()
peticiones: dict[str, deque[float]] = defaultdict(deque)


def limitar(request: Request) -> None:
    now = time.monotonic()
    ip = request.client.host if request.client else "unknown"
    for key in list(peticiones):
        if not peticiones[key] or peticiones[key][-1] < now - 60:
            del peticiones[key]
    bucket = peticiones[ip]
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= 60:
        raise HTTPException(429, "Espere un minuto antes de volver a intentar")
    bucket.append(now)


def firma_baja() -> URLSafeSerializer:
    return URLSafeSerializer(config().sesion_secreto.get_secret_value(), salt="baja-v1")


def pagina(titulo: str, cuerpo: str, status: int = 200) -> HTMLResponse:
    """Página pública mínima, sin datos personales ni recursos externos."""
    return HTMLResponse(
        '<!doctype html><html lang="es-CO"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="robots" content="noindex">'
        f"<title>{escape(titulo)}</title>"
        "<style>body{font-family:system-ui,sans-serif;max-width:36rem;margin:3rem auto;"
        "padding:0 1rem;color:#1d2b24;line-height:1.5}button{font:inherit;padding:.6rem 1.2rem;"
        "border-radius:.5rem;border:1px solid #024029;background:#024029;color:#fff;cursor:pointer}"
        "</style></head><body>"
        f"<h1>{escape(titulo)}</h1>{cuerpo}</body></html>",
        status_code=status,
        headers={"Cache-Control": "no-store"},
    )


def contacto_alternativo() -> str:
    cuenta = config().gmail_cuenta_envio or config().admin_correo
    return (
        "<p>Si desea dejar de recibir nuestros correos, responda a cualquiera de ellos con la "
        f'palabra <strong>BAJA</strong> o escriba a <a href="mailto:{escape(cuenta)}">'
        f"{escape(cuenta)}</a>. Atenderemos su solicitud sin costo.</p>"
    )


def enlace_invalido() -> HTMLResponse:
    return pagina(
        "No pudimos procesar este enlace",
        "<p>El enlace de baja no es válido o ya no corresponde a un mensaje registrado. "
        "No se realizó ningún cambio.</p>" + contacto_alternativo(),
        400,
    )


def mensaje_de_token(c: Conn, token: str) -> Row | None:
    try:
        ident = str(firma_baja().loads(token))
        UUID(ident)
    except (BadSignature, ValueError):
        return None
    return uno(c, "SELECT id,contacto_id FROM mensajes WHERE id=%s", (ident,), False) or None


@router.get("/baja/{token}")
def baja_get(token: str, request: Request) -> HTMLResponse:
    limitar(request)
    with transaccion("proceso:bajas") as c:
        m = mensaje_de_token(c, token)
    if not m:
        return enlace_invalido()
    return pagina(
        "Dejar de recibir correos",
        "<p>Confirme que no desea recibir más correos de NEXUS Innovación.</p>"
        '<form method="post"><button>Confirmar baja</button></form>',
    )


@router.post("/baja/{token}")
def baja_post(token: str, request: Request) -> HTMLResponse:
    limitar(request)
    with transaccion("proceso:bajas", accion="baja") as c:
        m = mensaje_de_token(c, token)
        if not m:
            return enlace_invalido()
        nuevo = c.execute(
            "INSERT INTO eventos_correo(tipo,mensaje_id,contacto_id,id_externo) VALUES('baja',%s,%s,%s) ON CONFLICT(id_externo) DO NOTHING RETURNING id",
            (m["id"], m["contacto_id"], "baja:" + str(m["id"])),
        ).fetchone()
        # Confirmamos solo si la baja quedó registrada ahora o ya estaba aplicada.
        aplicada = uno(
            c,
            """SELECT EXISTS (
                SELECT 1 FROM contactos ct JOIN supresiones s
                  ON s.correo=ct.correo OR s.dominio=split_part(ct.correo::text,'@',2)::citext
                 WHERE ct.id=%s) OR EXISTS (
                SELECT 1 FROM contactos WHERE id=%s AND (anonimizado_en IS NOT NULL OR excluido))
                AS ok""",
            (m["contacto_id"], m["contacto_id"]),
        )["ok"]
    if not aplicada:
        return pagina(
            "No pudimos confirmar la baja",
            "<p>Registramos su solicitud, pero no fue posible confirmarla automáticamente.</p>"
            + contacto_alternativo(),
            409,
        )
    return pagina(
        "Baja confirmada" if nuevo else "Su baja ya estaba registrada",
        "<p>No recibirá nuevas comunicaciones de NEXUS Innovación en este correo.</p>",
    )


@router.api_route("/img/{ident}", methods=["GET", "HEAD"])
def imagen(ident: str) -> Response:
    """Imagen subida a NEXUS para los correos. Pública por diseño: la piden Gmail y los
    demás programas de correo sin sesión. No registra aperturas."""
    try:
        UUID(ident)
    except ValueError as exc:
        raise HTTPException(404, "Imagen no encontrada") from exc
    with transaccion("publico:imagen") as c:
        r = uno(c, "SELECT tipo, datos FROM imagenes WHERE id=%s", (ident,), False)
    if not r:
        raise HTTPException(404, "Imagen no encontrada")
    return Response(
        bytes(r["datos"]),
        media_type=r["tipo"],
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'",
        },
    )


@router.api_route("/r/{referencia}", methods=["GET", "HEAD"])
def clic(referencia: str, request: Request) -> RedirectResponse:
    limitar(request)
    target = config().diagnostico_url
    with transaccion("proceso:clics", accion="clic", evento="clic_cta") as c:
        link = uno(c, "SELECT * FROM enlaces_seguimiento WHERE referencia=%s", (referencia,), False)
        if link:
            m = obtener(c, "mensajes", str(link["mensaje_id"]))
            ua = request.headers.get("user-agent", "").lower()
            automatico = (
                request.method == "HEAD"
                or not m["enviado_en"]
                or (datos.ahora() - m["enviado_en"]).total_seconds() < 60
                or any(x in ua for x in ("bot", "scanner", "safelinks", "proofpoint", "barracuda"))
            )
            insertar(
                c,
                "eventos_correo",
                {
                    "tipo": "clic",
                    "mensaje_id": m["id"],
                    "contacto_id": m["contacto_id"],
                    "referencia": referencia,
                    "automatico": automatico,
                },
            )
            if not automatico:
                datos.avanzar(c, m["contacto_id"], "interesado", "clic_cta")
            u = urlsplit(link["url_destino"])
            if u.scheme in ("https", "http"):
                params = dict(parse_qsl(u.query))
                params.update(
                    {
                        k: str(link[k])
                        for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content")
                        if link[k]
                    }
                )
                params["ref"] = referencia
                target = urlunsplit((u.scheme, u.netloc, u.path, urlencode(params), u.fragment))
    return RedirectResponse(target, status_code=302, headers={"Cache-Control": "no-store"})


@router.post("/api/webhooks/formulario")
async def formulario(request: Request) -> dict[str, bool]:
    limitar(request)
    raw = await request.body()
    if len(raw) > 2_000_000:
        raise HTTPException(413, "Solicitud demasiado grande")
    fecha = request.headers.get("x-nexus-fecha", "")
    secret = config().formulario_webhook_secreto.get_secret_value()
    try:
        valid = abs(time.time() - int(fecha)) <= 300
    except ValueError:
        valid = False
    expected = hmac.new(secret.encode(), fecha.encode() + b"." + raw, hashlib.sha256).hexdigest()
    if (
        not secret
        or not valid
        or not hmac.compare_digest(expected, request.headers.get("x-nexus-firma", ""))
    ):
        raise HTTPException(401, "Firma no válida")
    try:
        b = await request.json()
        procesar_formulario(b)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(400, "Formulario incompleto o no válido") from exc
    return {"ok": True}


def procesar_formulario(b: dict[str, Any]) -> None:
    correo = "".join(b["correo"].lower().split())
    autorizacion = b["autorizacion"]
    utm = b.get("utm") or {}
    with transaccion("proceso:webhook_formulario", accion="formulario", evento="formulario") as c:
        # Desconectar «Formulario» detiene la recepción aunque el secreto siga configurado.
        # FOR SHARE impide que una desconexión concurrente se cruce con este guardado.
        estado = uno(
            c, "SELECT estado FROM integraciones WHERE id='formulario' FOR SHARE", (), False
        ).get("estado")
        if estado != "conectada":
            raise HTTPException(
                503, "El receptor del formulario está desconectado; reintente más tarde"
            )
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (b["id"],))
        if uno(c, "SELECT id FROM envios_formulario WHERE id_externo=%s", (b["id"],), False):
            return
        f = insertar(
            c,
            "envios_formulario",
            {
                "id_externo": b["id"],
                "formulario": b["formulario"],
                "nombre": b["nombre"],
                "correo": correo,
                "telefono": b.get("telefono"),
                "cargo": b.get("cargo"),
                "empresa_nombre": b["empresa"],
                "municipio": b.get("municipio"),
                "autorizacion_aceptada": autorizacion["aceptada"],
                "texto_autorizacion": autorizacion.get("texto"),
                "version_politica": autorizacion.get("versionPolitica"),
                "datos": Jsonb(b.get("respuestas", {})),
                "referencia_opaca": b.get("ref"),
                "pagina": b.get("pagina"),
                "gclid": b.get("gclid"),
                "fbclid": b.get("fbclid"),
                **{
                    "utm_" + k: utm.get(k)
                    for k in ("source", "medium", "campaign", "content", "term")
                },
            },
        )
        # Lock exact email identity against concurrent submissions.
        c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (correo,))
        ct = uno(c, "SELECT * FROM contactos WHERE correo=%s", (correo,), False)
        e = {}
        if ct:
            e = obtener(c, "empresas", str(ct["empresa_id"]))
        if not e and b.get("nit"):
            e = uno(
                c,
                "SELECT * FROM empresas WHERE nit=%s AND fusionada_en_id IS NULL",
                (b["nit"],),
                False,
            )
        dom = correo.split("@")[1]
        gratuitos = {
            "gmail.com",
            "hotmail.com",
            "outlook.com",
            "yahoo.com",
            "live.com",
            "icloud.com",
        }
        if not e and dom not in gratuitos:
            e = uno(
                c,
                "SELECT * FROM empresas WHERE dominio=%s AND fusionada_en_id IS NULL",
                (dom,),
                False,
            )
        if not e:
            from app.db import todos

            e = next(
                (
                    x
                    for x in todos(
                        c,
                        "SELECT * FROM empresas WHERE municipio=%s AND fusionada_en_id IS NULL",
                        (b.get("municipio", ""),),
                    )
                    if normalizar_nombre(x["razon_social"]) == normalizar_nombre(b["empresa"])
                ),
                {},
            )
        if not e:
            e = insertar(
                c,
                "empresas",
                {
                    "razon_social": b["empresa"],
                    "nit": b.get("nit"),
                    "municipio": b.get("municipio", ""),
                    "dominio": None if dom in gratuitos else dom,
                },
            )
        origen = "registro"
        if b.get("ref") or utm.get("source") == "nexus-pm":
            origen = "correo"
        elif (
            b.get("gclid")
            or b.get("fbclid")
            or utm.get("medium", "").lower() in ("cpc", "ppc", "paid", "paid_social", "display")
        ):
            origen = "pagado"
        elif utm.get("medium", "").lower() in ("social", "organic_social"):
            origen = "organico_redes"
        elif any(
            x in (urlsplit(b.get("referrer") or "").hostname or "")
            for x in ("google.", "bing.", "duckduckgo.")
        ):
            origen = "organico_busqueda"
        if not ct:
            ct = insertar(
                c,
                "contactos",
                {
                    "empresa_id": e["id"],
                    "nombre": b["nombre"],
                    "correo": correo,
                    "telefono": b.get("telefono"),
                    "cargo": b.get("cargo"),
                    "origen": origen,
                },
            )
        for field in ("nombre", "correo", "telefono", "cargo"):
            if b.get(field):
                insertar(
                    c,
                    "evidencias",
                    {
                        "contacto_id": ct["id"],
                        "campo": field,
                        "valor": b[field],
                        "fuente": "formulario",
                        "estado": "verificado",
                    },
                )
        if autorizacion["aceptada"]:
            c.execute(
                "UPDATE bases_legales SET vigente=false,revocada_en=now(),motivo_revocacion='Nuevo formulario' WHERE contacto_id=%s AND vigente",
                (ct["id"],),
            )
            insertar(
                c,
                "bases_legales",
                {
                    "contacto_id": ct["id"],
                    "tipo": "autorizacion_formulario",
                    "texto": autorizacion["texto"],
                    "prueba": f"Formulario {b['formulario']} {b['id']} del {b['enviadoEn']}",
                    "envio_formulario_id": f["id"],
                    "obtenida_en": b["enviadoEn"],
                },
            )
        actualizar(
            c,
            "envios_formulario",
            str(f["id"]),
            {"contacto_id": ct["id"], "procesado_en": datos.ahora()},
        )
        link = uno(
            c,
            "SELECT m.inscripcion_id FROM enlaces_seguimiento l JOIN mensajes m ON m.id=l.mensaje_id WHERE l.referencia=%s AND m.contacto_id=%s",
            (b.get("ref"), ct["id"]),
            False,
        )
        if link.get("inscripcion_id"):
            actualizar(
                c,
                "inscripciones",
                str(link["inscripcion_id"]),
                {
                    "estado": "finalizada",
                    "motivo_salida": "completada",
                    "finalizada_en": datos.ahora(),
                    "proximo_envio": None,
                },
            )
            c.execute(
                "UPDATE mensajes SET estado='cancelado' WHERE inscripcion_id=%s AND estado='programado'",
                (link["inscripcion_id"],),
            )
            auditar(c, "diagnostico_completado", "envios_formulario", str(f["id"]))
        insertar(
            c,
            "interacciones",
            {
                "contacto_id": ct["id"],
                "tipo": "formulario",
                "resumen": "Formulario " + b["formulario"],
                "origen": origen,
            },
        )
        datos.avanzar(c, ct["id"], "lead", "formulario")
        encolar(c, "verificar_correo", {"contactoId": str(ct["id"])})
        encolar(c, "enriquecer_empresa", {"empresaId": str(e["id"])})
