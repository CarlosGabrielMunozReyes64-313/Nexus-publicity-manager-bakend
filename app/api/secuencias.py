"""Secuencias de correo: borrador, versiones, revisión, aprobación, pruebas y archivo.

Modelo (sin tablas nuevas):
- Cada secuencia pertenece a una sola campaña y la campaña tiene una propuesta interna de
  tipo «campana». Sus `propuesta_versiones` son el historial; la más reciente es el borrador.
- La tabla `secuencias` (con `pasos`) guarda la última versión aprobada (o la inicial).
  Solo cambia al aprobar: `campanas.aprobar` copia la versión y la congela.
- Guardar un borrador crea una versión y no toca la secuencia, la aprobación ni los envíos.
"""

import re
from email.message import EmailMessage
from email.utils import formataddr
from typing import Any
from uuid import uuid4

from fastapi import HTTPException
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.api import datos, lecturas, rutas_correo
from app.config import config
from app.db import Conn, Row, actualizar, auditar, insertar, obtener, todos, uno
from app.dominio.reglas import (
    BOGOTA,
    VentanaIncompatible,
    siguiente_ventana,
    validar_secuencia,
    ventanas_compatibles,
)
from app.esquemas.entradas import Secuencia
from app.esquemas.serializar import serializar
from app.motor.html import con_plantilla, preparar_pasos
from app.motor.plantillas import render, variables_no_admitidas

PRUEBAS_POR_DIA = 20
SIN_CAMPANA = "La secuencia no tiene campaña asociada"
ARCHIVADA = rutas_correo.SECUENCIA_ARCHIVADA
SIN_CAMBIOS = "No hay cambios para aprobar: la versión aprobada ya está vigente"
ESTADOS_CAMPANA = ("aprobada", "activa", "pausada", "finalizada")
SOLO_SEGMENTO_IA = (
    "La IA usa cifras agregadas de un segmento: con una lista como audiencia, edite los "
    "correos a mano o elija un segmento"
)
VENTANA_SUGERIDA = {"dias": [2, 3, 4], "desde": "08:00", "hasta": "10:30"}


# ---------------------------------------------------------------------- utilidades


def huella(contenido: Any) -> str | None:
    """Contenido comparable: secuencia normalizada sin ids de pasos ni notas."""
    s = rutas_correo.normalizar(contenido)
    if s is None:
        return None
    s.pop("id", None)
    for p in s["pasos"]:
        p.pop("id", None)
        # Solo referencia de origen: el contenido copiado es lo que cuenta.
        p.pop("plantillaId", None)
    return repr(s)


def correo(msg: str) -> str:
    """«Paso 2: falta {{baja}}» → «Correo 2: falta {{baja}}» (así se llaman en el editor)."""
    return re.sub(r"^Paso (\d+):", r"Correo \1:", msg)


def leer_forma(valor: Any) -> dict[str, Any]:
    """Valida solo la forma (que se lea como Secuencia) y prepara el HTML. Sin reglas."""
    if not isinstance(valor, dict):
        raise HTTPException(400, "La secuencia no tiene el formato esperado")
    try:
        s = Secuencia.model_validate(valor).model_dump(by_alias=True, exclude_none=True)
    except ValidationError as exc:
        campo = ".".join(str(x) for x in exc.errors()[0]["loc"])
        raise HTTPException(400, f"La secuencia no tiene el formato esperado ({campo})") from exc
    return preparar_pasos(s)


def campana_de(c: Conn, seq_id: str, lock: bool = False) -> Row | None:
    r = uno(
        c,
        "SELECT * FROM campanas WHERE secuencia_id=%s ORDER BY creado_en, id LIMIT 1"
        + (" FOR UPDATE" if lock else ""),
        (seq_id,),
        False,
    )
    return r or None


def propuesta_de(c: Conn, cam_id: Any, lock: bool = False) -> Row | None:
    r = uno(
        c,
        "SELECT * FROM propuestas WHERE tipo='campana' AND objetivo_campana_id=%s "
        "ORDER BY creado_en DESC LIMIT 1" + (" FOR UPDATE" if lock else ""),
        (cam_id,),
        False,
    )
    return r or None


def contenido_vivo(c: Conn, seq: Row) -> dict[str, Any]:
    return dict(serializar(lecturas.secuencia(c, seq)))


def asegurar(c: Conn, ident: str, lock: bool = False) -> tuple[Row, Row, Row]:
    """Secuencia, campaña y propuesta interna (la crea con la secuencia actual si falta)."""
    seq = obtener(c, "secuencias", ident, lock)
    cam = campana_de(c, ident, lock)
    if not cam:
        raise HTTPException(409, SIN_CAMPANA)
    p = propuesta_de(c, cam["id"], lock)
    if not p:
        from app.api.campanas import nueva_propuesta

        nueva_propuesta(
            c,
            "estratega_redactor",
            "campana",
            "Secuencia de la campaña " + cam["nombre"],
            contenido_vivo(c, seq),
            {"objetivo_campana_id": cam["id"]},
            autor="admin",
        )
        p = propuesta_de(c, cam["id"], lock)
        assert p
    return seq, cam, p


def ultima_version(c: Conn, p: Row) -> Row:
    return uno(
        c,
        "SELECT * FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], p["version_actual"]),
    )


def borrador_de(c: Conn, seq: Row, p: Row) -> dict[str, Any]:
    v = ultima_version(c, p)
    contenido = v["contenido"]
    s = rutas_correo.normalizar(contenido)
    if s is None:
        # Versión antigua sin forma de secuencia: el borrador parte de la secuencia guardada.
        s = rutas_correo.normalizar(contenido_vivo(c, seq)) or {}
    notas = contenido.get("notasAgente") if isinstance(contenido, dict) else None
    out = {**s, "id": str(seq["id"])}
    if notas:
        out["notasAgente"] = notas
    return {"version": v["version"], "autor": v["autor"], "fecha": v["creada_en"], "contenido": out}


def ultima_aprobacion(c: Conn, cam_id: Any) -> Row | None:
    r = uno(
        c,
        """SELECT a.*, v.version AS version_propuesta FROM aprobaciones_campana a
           JOIN propuesta_versiones v ON v.id=a.propuesta_version_id
           WHERE a.campana_id=%s ORDER BY a.aprobada_en DESC LIMIT 1""",
        (cam_id,),
        False,
    )
    return r or None


def hay_cambios(borrador: Any, aprobacion: Row | None, cam: Row) -> bool:
    if not aprobacion or not cam.get("aprobacion_vigente_id"):
        return True
    return huella(borrador) != huella(aprobacion["contenido_congelado"]["secuencia"])


# ---------------------------------------------------------------------- lectura


def _extras(c: Conn, filas: list[Row]) -> list[Row]:
    ids = [str(r["id"]) for r in filas]
    if not ids:
        return []
    cams = {
        str(x["secuencia_id"]): x
        for x in todos(
            c,
            """SELECT DISTINCT ON (secuencia_id) * FROM campanas
               WHERE secuencia_id=ANY(%s::uuid[]) ORDER BY secuencia_id, creado_en, id""",
            (ids,),
        )
    }
    cam_ids = [str(x["id"]) for x in cams.values()]
    props: dict[str, Row] = {}
    aprob: dict[str, Row] = {}
    if cam_ids:
        for x in todos(
            c,
            """SELECT DISTINCT ON (p.objetivo_campana_id) p.objetivo_campana_id AS cam,
                      v.version, v.contenido, v.creada_en
               FROM propuestas p JOIN propuesta_versiones v
                 ON v.propuesta_id=p.id AND v.version=p.version_actual
               WHERE p.tipo='campana' AND p.objetivo_campana_id=ANY(%s::uuid[])
               ORDER BY p.objetivo_campana_id, p.creado_en DESC""",
            (cam_ids,),
        ):
            props[str(x["cam"])] = x
        for x in todos(
            c,
            """SELECT DISTINCT ON (a.campana_id) a.campana_id AS cam, a.contenido_congelado,
                      v.version AS version_propuesta
               FROM aprobaciones_campana a JOIN propuesta_versiones v ON v.id=a.propuesta_version_id
               WHERE a.campana_id=ANY(%s::uuid[]) ORDER BY a.campana_id, a.aprobada_en DESC""",
            (cam_ids,),
        ):
            aprob[str(x["cam"])] = x
    out = []
    for r in lecturas.secuencias(c, filas):
        cam = cams.get(str(r["id"]))
        extra: dict[str, Any] = {"archivada": r.get("archivada_en") is not None}
        actualizada = r["actualizado_en"]
        if cam:
            prop = props.get(str(cam["id"]))
            ap = aprob.get(str(cam["id"]))
            extra.update(
                campana_id=cam["id"],
                campana_estado=cam["estado"],
                segmento_id=cam["segmento_id"],
                lista_id=cam.get("lista_id"),
                incorporar_nuevos=bool(cam.get("incorporar_nuevos")),
                motivo_no_programada=cam.get("motivo_no_programada"),
            )
            if prop:
                extra["version_borrador"] = prop["version"]
                actualizada = max(actualizada, prop["creada_en"])
            if ap:
                extra["version_aprobada"] = ap["version_propuesta"]
            if extra["archivada"]:
                estado = "archivada"
            elif not ap:
                estado = "borrador"
            elif hay_cambios(prop["contenido"] if prop else r, ap, cam):
                estado = "cambios_sin_aprobar"
            elif cam["estado"] in ESTADOS_CAMPANA:
                estado = cam["estado"]
            else:
                estado = "cambios_sin_aprobar"
            extra["estado_editor"] = estado
        else:
            extra["estado_editor"] = "archivada" if extra["archivada"] else "borrador"
        extra["actualizada_en"] = actualizada
        out.append({**r, **extra})
    return out


def lista(c: Conn, params: Any) -> list[Row]:
    archivadas = str(params.get("archivadas", "")).lower() == "true"
    filas = todos(
        c,
        "SELECT * FROM secuencias"
        + ("" if archivadas else " WHERE archivada_en IS NULL")
        + " ORDER BY actualizado_en DESC, id",
    )
    return _extras(c, filas)


def detalle(c: Conn, ident: str) -> Row:
    return _extras(c, [obtener(c, "secuencias", ident)])[0]


def borrador(c: Conn, ident: str) -> dict[str, Any]:
    seq, _, p = asegurar(c, ident)
    return borrador_de(c, seq, p)


def elegibles(c: Conn, ident: str) -> list[Row]:
    """Hasta 50 contactos elegibles del segmento, para la vista previa y la prueba."""
    _, _, p = asegurar(c, ident)
    return rutas_correo.elegibles_de(c, p)


def versiones(c: Conn, ident: str) -> list[Row]:
    _, _, p = asegurar(c, ident)
    return [
        {
            "version": v["version"],
            "autor": v["autor"],
            "fecha": v["creada_en"],
            "decision": v["decision"],
            "comentario": v["comentario"],
            "contenido": v["contenido"],
        }
        for v in todos(
            c,
            "SELECT * FROM propuesta_versiones WHERE propuesta_id=%s ORDER BY version",
            (p["id"],),
        )
    ]


# ---------------------------------------------------------------------- borrador


def guardar_borrador(c: Conn, ident: str, valor: Any) -> dict[str, Any]:
    """Crea una versión (autor admin). No revoca, no pausa y no cambia lo aprobado."""
    from app.api.campanas import versionar

    seq, _, p = asegurar(c, ident, True)
    if seq.get("archivada_en"):
        raise HTTPException(409, ARCHIVADA)
    s = leer_forma(valor)
    actual = ultima_version(c, p)
    if huella(actual["contenido"]) == huella(s) and actual["decision"] is None:
        return {"ok": True, "version": actual["version"]}
    notas = rutas_correo.notas_editadas(actual["contenido"], s)
    versionar(c, p, {**s, "notasAgente": notas} if notas else s, autor="admin")
    auditar(c, "guardar_borrador", "secuencias", ident)
    return {"ok": True, "version": p["version_actual"] + 1}


def restaurar(c: Conn, ident: str, numero: int) -> dict[str, Any]:
    from app.api.campanas import versionar

    _, _, p = asegurar(c, ident, True)
    if numero == p["version_actual"]:
        raise HTTPException(400, "Esa ya es la versión del borrador")
    v = uno(
        c,
        "SELECT contenido FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
        (p["id"], numero),
        False,
    )
    if not v:
        raise HTTPException(404, "Versión no encontrada")
    versionar(c, p, v["contenido"], autor="admin")
    auditar(c, "restaurar_version", "secuencias", ident)
    return {"ok": True, "version": p["version_actual"] + 1}


# ---------------------------------------------------------------------- vista previa


def _contenido_para(c: Conn, ident: str, body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and body.get("secuencia") is not None:
        return leer_forma(body["secuencia"])
    seq, _, p = asegurar(c, ident)
    return dict(borrador_de(c, seq, p)["contenido"])


def _render(c: Conn, s: dict[str, Any], contacto_id: Any, baja: str, enlace: str) -> dict[str, Any]:
    if not contacto_id:
        raise HTTPException(400, "Elija un contacto")
    ct = serializar(lecturas.contacto(c, obtener(c, "contactos", str(contacto_id))))
    e = serializar(lecturas.empresa(c, obtener(c, "empresas", ct["empresaId"])))
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    pasos = [{**x, "id": x.get("id", f"paso-{i}")} for i, x in enumerate(s["pasos"], 1)]
    r = render({**s, "pasos": pasos}, ct, e, marca, baja, enlace)
    return {"pasos": r["pasos"], "respaldo": r["respaldo"]}


def previsualizar(c: Conn, ident: str, body: Any) -> dict[str, Any]:
    """Renderiza los cambios sin guardar (o el borrador) con el render del motor. No guarda."""
    s = _contenido_para(c, ident, body)
    return _render(
        c, s, (body or {}).get("contactoId"), "[Enlace de baja al enviar]", config().diagnostico_url
    )


# ---------------------------------------------------------------------- revisión


def revision(c: Conn, ident: str) -> dict[str, Any]:
    """Lista de verificación del borrador. aprobar() usa exactamente esta lógica."""
    seq, cam, p = asegurar(c, ident)
    s = borrador_de(c, seq, p)["contenido"]
    from app.motor import audiencia

    seg = rutas_correo.audiencia_resumen(c, cam)
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    bloqueos: list[str] = []
    errores = [correo(e) for e in validar_secuencia(s, marca["prohibidas"])]

    advertencias = list(rutas_correo.advertencias_de(s))
    errores.extend(variables_no_admitidas(s))
    from app.motor.html import es_diseno, problemas_diseno

    for i, paso in enumerate(s.get("pasos", []), 1):
        html_paso = paso.get("cuerpoHtml") if paso.get("formato") == "html" else None
        errores.extend(f"Correo {i}: {x}" for x in problemas_diseno(html_paso))
        if es_diseno(html_paso):
            advertencias.append(
                f"El correo {i} lleva diseño con imágenes: en un primer contacto Gmail suele "
                "clasificarlo en Promociones; para el primer correo se recomienda texto simple"
                if i == 1
                else f"El correo {i} lleva imágenes: muchos programas las ocultan hasta que el "
                "destinatario las permite; el texto debe entenderse sin ellas"
            )
    ventana_valida = False
    if seg["bloqueo"]:
        bloqueos.append(seg["bloqueo"])
    try:
        siguiente_ventana(datos.ahora(), s["ventanaEnvio"], limites["ventana"])
        ventana_valida = True
    except VentanaIncompatible:
        bloqueos.append(rutas_correo.VENTANA_GLOBAL)
    except (KeyError, ValueError):
        pass  # la ventana inválida ya figura en errores
    if seq.get("archivada_en"):
        bloqueos.append(ARCHIVADA)
    ap = ultima_aprobacion(c, cam["id"])
    cambios = hay_cambios(s, ap, cam)
    if not cambios:
        bloqueos.append(SIN_CAMBIOS)
    candidatos = audiencia.candidatos(c, cam)
    ids = audiencia.filtrar_elegibles(c, candidatos)
    if not ids:
        advertencias.append(
            "La audiencia no tiene destinatarios elegibles: al aprobar no se programará "
            "ningún envío hasta que haya contactos con base legal aprobada y correo válido"
        )
    if ids:
        usadas = {
            m
            for x in s["pasos"]
            for m in re.findall(
                r"\{\{(\w+)\}\}",
                x["cuerpo"] + (x.get("asunto") or "") + (x.get("cuerpoHtml") or ""),
            )
        }
        verificados = rutas_correo.datos_verificados(c, ids)
        for var in rutas_correo.VARIABLES_DATOS:
            faltan = len(ids) - verificados[var]
            if var in usadas and faltan > 0:
                advertencias.append(
                    f"{{{{{var}}}}}: {faltan} de {len(ids)} contactos usarán el texto de respaldo"
                )
    gmail = uno(c, "SELECT estado FROM integraciones WHERE id='gmail'", (), False)
    gmail_ok = bool(gmail) and gmail["estado"] == "conectada"
    if not gmail_ok:
        advertencias.append(
            "Gmail no está conectado: podrá aprobar, pero los envíos no se programarán "
            "hasta conectarlo"
        )
    return {
        "puedeAprobar": not bloqueos and not errores,
        "bloqueos": bloqueos,
        "errores": errores,
        "advertencias": advertencias,
        "elegibles": len(ids),
        "candidatos": len(candidatos),
        "noElegibles": audiencia.motivos(c, [x for x in candidatos if x not in set(ids)]),
        "audiencia": {
            "tipo": seg["tipo"],
            "id": seg["id"],
            "nombre": seg["nombre"],
            "incorporarNuevos": bool(cam.get("incorporar_nuevos")),
        },
        "gmailConectado": gmail_ok,
        "duracionDiasHabiles": sum(x["esperaDiasHabiles"] for x in s["pasos"]),
        "hayCambiosSinAprobar": cambios,
        "segmento": {
            "id": seg["id"],
            "nombre": seg["nombre"],
            "estado": seg["estado"],
            "version": seg["version"],
        },
        "ventanaValida": ventana_valida,
        "topeDiario": cam["tope_diario"],
        "campanaEstado": cam["estado"],
    }


# ---------------------------------------------------------------------- aprobación


def aprobar_secuencia(c: Conn, ident: str, admin: str, body: Any = None) -> dict[str, Any]:
    """Aprueba la versión y, salvo «programar: false», programa los envíos en la misma
    transacción: activa la campaña e inscribe a cada destinatario elegible con su primer
    paso. Si no puede programar (Gmail, audiencia vacía, horario), la aprobación queda
    guardada y la respuesta explica qué falta."""
    from app.api.campanas import aprobar, versionar
    from app.motor import audiencia

    programar_envios = not (isinstance(body, dict) and body.get("programar") is False)
    # Serializa dos aprobaciones simultáneas (doble clic): la segunda ve «sin cambios».
    seq, cam, p = asegurar(c, ident, True)
    rev = revision(c, ident)
    if not rev["puedeAprobar"]:
        raise HTTPException(400, ". ".join(rev["bloqueos"] + rev["errores"]))
    v = ultima_version(c, p)
    if v["decision"] is not None:
        # La versión ya se decidió (p. ej., aprobada y luego revocada): se aprueba una copia.
        versionar(c, p, v["contenido"], autor="admin")
    p = obtener(c, "propuestas", str(p["id"]), True)
    aprobar(c, p, admin)
    auditar(c, "aprobar_secuencia", "secuencias", ident)
    out: dict[str, Any] = {"ok": True, "version": p["version_actual"]}
    if programar_envios:
        out["programacion"] = audiencia.activar(c, str(cam["id"]), datos.ahora())
    return out


def programar_envios(c: Conn, ident: str) -> dict[str, Any]:
    """POST /secuencias/:id/programar: programa (o vuelve a intentar programar) la
    versión aprobada vigente. Idempotente: nunca duplica inscripciones ni mensajes."""
    from app.motor import audiencia

    cam = campana_de(c, ident, True)
    if not cam:
        raise HTTPException(409, SIN_CAMPANA)
    if not cam.get("aprobacion_vigente_id"):
        raise HTTPException(409, "Apruebe la secuencia antes de programar envíos")
    r = audiencia.activar(c, str(cam["id"]), datos.ahora())
    return {"ok": True, "programacion": r}


def cambiar_audiencia(c: Conn, ident: str, body: Any) -> dict[str, Any]:
    """PUT /secuencias/:id/audiencia {segmentoId | listaId, incorporarNuevos}.

    Cambiar el segmento o la lista revoca la aprobación vigente (la audiencia es parte de
    lo aprobado). Cambiar solo «incorporarNuevos» también, porque decide quién recibe.
    """
    body = body if isinstance(body, dict) else {}
    seq, cam, _ = asegurar(c, ident, True)
    if seq.get("archivada_en"):
        raise HTTPException(409, ARCHIVADA)
    seg_id = body.get("segmentoId") or None
    lista_id = body.get("listaId") or None
    if bool(seg_id) == bool(lista_id):
        raise HTTPException(400, "Elija un segmento o una lista como audiencia")
    if seg_id:
        obtener(c, "segmentos", str(seg_id))
    else:
        lista = obtener(c, "listas", str(lista_id))
        if lista["archivada_en"]:
            raise HTTPException(409, "La lista de envío está archivada")
    nuevos = bool(body.get("incorporarNuevos", cam.get("incorporar_nuevos")))
    cambios = (
        str(cam.get("segmento_id") or "") != str(seg_id or "")
        or str(cam.get("lista_id") or "") != str(lista_id or "")
        or bool(cam.get("incorporar_nuevos")) != nuevos
    )
    if not cambios:
        return {"ok": True, "revocada": False}
    revocada = bool(cam.get("aprobacion_vigente_id"))
    if revocada:
        c.execute(
            "SELECT revocar_aprobaciones(ARRAY[%s]::uuid[],%s)",
            (cam["id"], "La audiencia de la campaña cambió"),
        )
    actualizar(
        c,
        "campanas",
        str(cam["id"]),
        {"segmento_id": seg_id, "lista_id": lista_id, "incorporar_nuevos": nuevos},
    )
    from app.api.campanas import revisar_pendientes

    revisar_pendientes(c)
    auditar(c, "cambiar_audiencia", "secuencias", ident)
    return {"ok": True, "revocada": revocada}


def reanudar(c: Conn, ident: str, admin: str) -> dict[str, Any]:
    from app.api.campanas import modificar

    cam = campana_de(c, ident, True)
    if not cam:
        raise HTTPException(409, SIN_CAMPANA)
    modificar(c, ["campanas", str(cam["id"]), "reanudar"], "POST", {}, admin)
    return {"ok": True}


# ---------------------------------------------------------------------- prueba


def proveedor_gmail(c: Conn) -> Any:
    """Punto único para el envío de pruebas (las pruebas automáticas lo reemplazan)."""
    from app.integraciones.gmail import GmailReal

    return GmailReal(c)


def prueba(c: Conn, ident: str, body: Any, admin: str) -> dict[str, Any]:
    """Envía la secuencia renderizada solo al correo del administrador, como «[Prueba]»."""
    body = body if isinstance(body, dict) else {}
    gmail = uno(c, "SELECT estado FROM integraciones WHERE id='gmail'", (), False)
    if not gmail or gmail["estado"] != "conectada":
        raise HTTPException(400, "Conecte Gmail en Configuración para enviar pruebas")
    destino = uno(c, "SELECT correo FROM administradores WHERE id=%s", (admin,), False)
    if not destino or not destino.get("correo"):
        raise HTTPException(400, "No se encontró el correo del administrador con sesión iniciada")
    s = _contenido_para(c, ident, body)
    r = _render(c, s, body.get("contactoId"), "#", config().diagnostico_url)
    pasos = r["pasos"]
    if body.get("orden") is not None:
        pasos = [x for x in pasos if x["orden"] == body["orden"]]
        if not pasos:
            raise HTTPException(404, "Correo no encontrado en la secuencia")
    hoy = datos.ahora().astimezone(BOGOTA).date()
    usadas = uno(
        c,
        "SELECT count(*) AS n FROM registro_auditoria WHERE accion='envio_prueba' "
        "AND (fecha AT TIME ZONE 'America/Bogota')::date=%s",
        (hoy,),
    )["n"]
    if usadas + len(pasos) > PRUEBAS_POR_DIA:
        raise HTTPException(
            429,
            f"Límite de {PRUEBAS_POR_DIA} correos de prueba por día "
            f"(quedan {max(0, PRUEBAS_POR_DIA - usadas)})",
        )
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    proveedor = proveedor_gmail(c)
    primero = next((x.get("asunto") for x in r["pasos"] if x.get("asunto")), "")
    enviados = 0
    for x in pasos:
        m = EmailMessage()
        m["From"] = formataddr((marca["nombre"], config().gmail_cuenta_envio))
        m["To"] = destino["correo"]
        m["Subject"] = "[Prueba] " + (x.get("asunto") or ("Re: " + primero))
        m["X-Nexus-Prueba"] = "si"
        m.set_content(x["cuerpo"], charset="utf-8")
        if x.get("formato") == "html" and x.get("cuerpoHtml"):
            m.add_alternative(con_plantilla(x["cuerpoHtml"]), subtype="html", charset="utf-8")
        try:
            proveedor.enviar(m, None)
        except Exception as exc:
            # Sin detalles del proveedor: un mensaje claro y la cantidad ya enviada.
            raise HTTPException(
                400,
                "Gmail no aceptó el correo de prueba; revise la conexión en Configuración"
                + (f" (se enviaron {enviados} antes del error)" if enviados else ""),
            ) from exc
        enviados += 1
        auditar(c, "envio_prueba", "secuencias", ident)
    return {"ok": True, "destinatario": destino["correo"], "enviados": len(pasos)}


# ---------------------------------------------------------------------- crear y archivar


def _insertar_secuencia(c: Conn, s: dict[str, Any], con_pasos: bool) -> Row:
    v = s["ventanaEnvio"]
    seq = insertar(
        c,
        "secuencias",
        {
            "nombre": s["nombre"],
            "objetivo": s["objetivo"],
            "llamada_accion": s["llamadaAccion"],
            "metricas_exito": s["metricasExito"],
            "ventana_dias": v["dias"],
            "ventana_desde": v["desde"],
            "ventana_hasta": v["hasta"],
        },
    )
    if con_pasos:
        for i, p in enumerate(s["pasos"], 1):
            insertar(
                c,
                "pasos",
                {
                    "secuencia_id": seq["id"],
                    "orden": i,
                    "asunto": p.get("asunto"),
                    "cuerpo": p["cuerpo"],
                    "espera_dias_habiles": p["esperaDiasHabiles"],
                    "mismo_hilo": p["mismoHilo"],
                    "variables": Jsonb(p["variables"]),
                    "formato": p.get("formato", "texto"),
                    "cuerpo_html": p.get("cuerpoHtml") if p.get("formato") == "html" else None,
                },
            )
    return seq


def _crear(
    c: Conn, s: dict[str, Any], segmento_id: Any, nombre_campana: str, lista_id: Any = None
) -> str:
    from app.api.campanas import nueva_propuesta

    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    # La tabla de pasos solo admite contenido válido; un borrador con errores vive en la
    # versión hasta que se corrija y apruebe.
    seq = _insertar_secuencia(c, s, not validar_secuencia(s, marca["prohibidas"]))
    cam = insertar(
        c,
        "campanas",
        {
            "nombre": nombre_campana,
            "segmento_id": segmento_id,
            "lista_id": lista_id,
            "secuencia_id": seq["id"],
            "estado": "borrador",
        },
    )
    nueva_propuesta(
        c,
        "estratega_redactor",
        "campana",
        "Secuencia creada por el administrador",
        {**s, "id": str(seq["id"])},
        {"objetivo_campana_id": cam["id"]},
        autor="admin",
    )
    return str(seq["id"])


def nueva(c: Conn, body: Any) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    nombre = " ".join(str(body.get("nombre") or "").split())
    objetivo = " ".join(str(body.get("objetivo") or "").split())
    llamada = " ".join(str(body.get("llamadaAccion") or "").split())
    if not 3 <= len(nombre) <= 120:
        raise HTTPException(400, "El nombre requiere entre 3 y 120 caracteres")
    if not 5 <= len(objetivo) <= 200:
        raise HTTPException(400, "El objetivo requiere entre 5 y 200 caracteres")
    if not 3 <= len(llamada) <= 200:
        raise HTTPException(400, "La llamada a la acción requiere entre 3 y 200 caracteres")
    lista = None
    seg = None
    if body.get("listaId"):
        lista = obtener(c, "listas", str(body["listaId"]))
        if lista["archivada_en"]:
            raise HTTPException(409, "La lista de envío está archivada")
    elif not body.get("segmentoId"):
        raise HTTPException(400, "Elija un segmento o una lista")
    else:
        seg = obtener(c, "segmentos", str(body["segmentoId"]))
        if seg["estado"] != "aprobado":
            raise HTTPException(409, rutas_correo.SEGMENTO_SIN_APROBAR)
    limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    ventana = (
        VENTANA_SUGERIDA
        if ventanas_compatibles(VENTANA_SUGERIDA, limites["ventana"])
        else limites["ventana"]
    )
    s = {
        "nombre": nombre,
        "objetivo": objetivo,
        "llamadaAccion": llamada,
        "metricasExito": ["Respuestas positivas", "Diagnósticos completados"],
        "ventanaEnvio": {k: ventana[k] for k in ("dias", "desde", "hasta")},
        "pasos": [
            {
                "orden": 1,
                "asunto": "Una pregunta para {{empresa}}",
                "cuerpo": "Buenos días, {{nombre}}:\n\nEscriba aquí su mensaje. "
                + llamada
                + ": {{enlace}}\n\n{{firma}}\n{{baja}}",
                "esperaDiasHabiles": 0,
                "mismoHilo": False,
                "variables": [
                    {"nombre": "nombre", "campoFuente": "nombre", "respaldo": "equipo"},
                    {"nombre": "empresa", "campoFuente": "razonSocial", "respaldo": "su empresa"},
                ],
                "formato": "texto",
            }
        ],
    }
    if body.get("plantillaId"):
        # Primer correo a partir de una plantilla (copia: editable antes de aprobar).
        from app.api.plantillas import para_paso

        t = para_paso(c, str(body["plantillaId"]))
        primero: dict[str, Any] = s["pasos"][0]  # type: ignore[index]
        primero.update(
            asunto=t["asunto"] or primero.get("asunto"),
            cuerpo=t["cuerpo"],
            formato=t["formato"],
            cuerpoHtml=t["cuerpoHtml"],
            plantillaId=t["plantillaId"],
        )
        s = preparar_pasos(s)
    ident = _crear(c, s, seg["id"] if seg else None, nombre, lista["id"] if lista else None)
    if body.get("incorporarNuevos"):
        c.execute("UPDATE campanas SET incorporar_nuevos=true WHERE secuencia_id=%s", (ident,))
    auditar(c, "crear_secuencia", "secuencias", ident)
    return {"ok": True, "id": ident}


def duplicar(c: Conn, ident: str) -> dict[str, Any]:
    seq, cam, p = asegurar(c, ident)
    s = borrador_de(c, seq, p)["contenido"]
    s = {k: v for k, v in s.items() if k not in ("id", "notasAgente")}
    s["nombre"] = ("Copia de " + s["nombre"])[:200]
    s["pasos"] = [{k: v for k, v in x.items() if k != "id"} for x in s["pasos"]]
    nuevo = _crear(
        c, s, cam["segmento_id"], ("Copia de " + cam["nombre"])[:200], cam.get("lista_id")
    )
    auditar(c, "duplicar_secuencia", "secuencias", nuevo)
    return {"ok": True, "id": nuevo}


def archivar(c: Conn, ident: str) -> dict[str, Any]:
    obtener(c, "secuencias", ident, True)
    cam = campana_de(c, ident, True)
    if cam and cam["estado"] == "activa":
        raise HTTPException(409, "Pause la campaña antes de archivar la secuencia")
    c.execute("UPDATE secuencias SET archivada_en=now() WHERE id=%s", (ident,))
    auditar(c, "archivar_secuencia", "secuencias", ident)
    return {"ok": True}


# ---------------------------------------------------------------------- otra versión


def otra_version_ia(c: Conn, ident: str, body: Any, admin: str) -> dict[str, Any]:
    """Rechaza el borrador actual con el comentario y pide al agente una versión nueva."""
    from app.agentes.solicitudes import proveedor_configurado
    from app.db import encolar

    comentario = str((body or {}).get("comentario") or "").strip()
    if len(comentario) < 3:
        raise HTTPException(400, "El comentario requiere al menos 3 caracteres")
    proveedor_configurado()
    _, cam, p = asegurar(c, ident, True)
    if not cam.get("segmento_id"):
        raise HTTPException(409, SOLO_SEGMENTO_IA)
    if uno(
        c,
        "SELECT count(*) AS n FROM tareas WHERE tipo='revisar_campana' "
        "AND estado IN ('pendiente','en_proceso') AND datos->>'propuestaId'=%s",
        (str(p["id"]),),
    )["n"]:
        raise HTTPException(409, "La IA ya está escribiendo una versión nueva de esta secuencia")
    v = ultima_version(c, p)
    if v["decision"] is None:
        actualizar(
            c,
            "propuesta_versiones",
            str(v["id"]),
            {
                "decision": "rechazada",
                "comentario": comentario,
                "decidida_por": admin,
                "decidida_en": datos.ahora(),
            },
        )
        actualizar(c, "propuestas", str(p["id"]), {"estado": "rechazada"})
    datos_tarea = {
        "propuestaId": str(p["id"]),
        "secuenciaId": ident,
        "segmentoId": str(cam["segmento_id"]),
        "comentario": comentario,
    }
    encolar(c, "revisar_campana", datos_tarea, f"revisar_campana:{p['id']}:{uuid4()}", 6)
    auditar(c, "pedir_otra_version", "secuencias", ident)
    return {"ok": True}


# ---------------------------------------------------------------------- solicitudes a la IA

TIPOS_IA = ("proponer_campana", "revisar_campana", "reescribir_correo")


def pedir_ia(c: Conn, body: Any) -> dict[str, Any]:
    """POST /secuencias/ia: valida en el momento y encola proponer_campana."""
    rutas_correo.encolar_ruta(c, rutas_correo.validar_solicitud(body))
    return {"ok": True}


def solicitudes(c: Conn) -> list[Row]:
    """Solicitudes a la IA de los últimos 30 días (crear, otra versión y mejorar correo)."""
    return [
        {k: v for k, v in s.items() if k != "propuesta_id"}
        for s in rutas_correo.solicitudes(c, 30, TIPOS_IA)
    ]


def reintentar_solicitud(c: Conn, tarea_id: str) -> dict[str, Any]:
    return rutas_correo.reintentar(c, tarea_id, TIPOS_IA)


# ---------------------------------------------------------------------- «Mejorar con IA»

INSTRUCCION_MIN, INSTRUCCION_MAX = 3, 300


def mejorar(c: Conn, ident: str, orden_texto: str, body: Any) -> dict[str, Any]:
    """POST /secuencias/:id/correos/:orden/mejorar: encola reescribir_correo. No guarda nada.

    La IA recibe el texto del correo (con sus variables), el resto de la secuencia y cifras
    agregadas del segmento; nunca datos personales. La sugerencia queda en la tarea y el
    administrador decide si la usa en su borrador.
    """
    from app.agentes.solicitudes import proveedor_configurado
    from app.db import encolar

    try:
        orden = int(orden_texto)
    except ValueError as exc:
        raise HTTPException(400, "Correo no válido") from exc
    cuerpo = body if isinstance(body, dict) else {}
    instruccion = " ".join(str(cuerpo.get("instruccion") or "").split())
    if not INSTRUCCION_MIN <= len(instruccion) <= INSTRUCCION_MAX:
        raise HTTPException(
            400,
            f"La instrucción requiere entre {INSTRUCCION_MIN} y {INSTRUCCION_MAX} caracteres",
        )
    seq, cam, _ = asegurar(c, ident)
    if seq.get("archivada_en"):
        raise HTTPException(409, ARCHIVADA)
    if not cam.get("segmento_id"):
        raise HTTPException(409, SOLO_SEGMENTO_IA)
    s = _contenido_para(c, ident, cuerpo)
    paso = next((p for p in s["pasos"] if p["orden"] == orden), None)
    if paso is None:
        raise HTTPException(404, "Correo no encontrado en la secuencia")
    if uno(
        c,
        "SELECT count(*) AS n FROM tareas WHERE tipo='reescribir_correo' "
        "AND estado IN ('pendiente','en_proceso') AND datos->>'secuenciaId'=%s "
        "AND (datos->>'orden')::int=%s",
        (ident, orden),
    )["n"]:
        raise HTTPException(409, "La IA ya está mejorando este correo")
    proveedor_configurado()
    lleva_asunto = orden == 1 or not paso.get("mismoHilo")
    original: dict[str, Any] = {"cuerpo": paso["cuerpo"], "llevaAsunto": lleva_asunto}
    if lleva_asunto and paso.get("asunto"):
        original["asunto"] = paso["asunto"]
    contexto = {k: v for k, v in s.items() if k not in ("id", "notasAgente")}
    contexto["pasos"] = [
        {k: p.get(k) for k in ("orden", "asunto", "cuerpo", "mismoHilo") if p.get(k) is not None}
        for p in s["pasos"]
    ]
    encolar(
        c,
        "reescribir_correo",
        {
            "secuenciaId": ident,
            "segmentoId": str(cam["segmento_id"]),
            "orden": orden,
            "instruccion": instruccion,
            "original": original,
            "contexto": contexto,
        },
        f"reescribir_correo:{ident}:{uuid4()}",
        5,
    )
    auditar(c, "pedir_mejora_correo", "secuencias", ident)
    return {"ok": True}


def sugerencias(c: Conn, ident: str) -> list[dict[str, Any]]:
    """GET /secuencias/:id/sugerencias: las de los últimos 30 días que no se descartaron."""
    obtener(c, "secuencias", ident)
    filas = todos(
        c,
        "SELECT id,estado,datos,ultimo_error,creada_en FROM tareas "
        "WHERE tipo='reescribir_correo' AND datos->>'secuenciaId'=%s "
        "AND NOT (datos ? 'descartada') AND creada_en > now() - interval '30 days' "
        "ORDER BY creada_en DESC",
        (ident,),
    )
    out = []
    for f in filas:
        d = f["datos"] or {}
        estado = rutas_correo.ESTADOS_SOLICITUD.get(f["estado"], "en_cola")
        original = {k: v for k, v in (d.get("original") or {}).items() if k in ("asunto", "cuerpo")}
        fila: dict[str, Any] = {
            "id": f["id"],
            "orden": d.get("orden"),
            "instruccion": d.get("instruccion") or "",
            "estado": estado,
            "creada_en": f["creada_en"],
            "original": original or None,
        }
        if estado == "lista":
            fila.update({k: v for k, v in (d.get("resultado") or {}).items() if v})
        if estado == "fallida":
            fila["error"] = (
                "Solicitud cancelada"
                if f["estado"] == "cancelada"
                else f["ultimo_error"] or "La solicitud no pudo completarse"
            )
        out.append(fila)
    return out


def descartar_sugerencia(c: Conn, ident: str, tarea_id: str) -> dict[str, Any]:
    """DELETE /secuencias/:id/sugerencias/:tareaId: la oculta (y la cancela si no empezó)."""
    t = uno(
        c,
        "SELECT * FROM tareas WHERE id::text=%s AND tipo='reescribir_correo' "
        "AND datos->>'secuenciaId'=%s FOR UPDATE",
        (tarea_id, ident),
        False,
    )
    if not t:
        raise HTTPException(404, "Sugerencia no encontrada")
    c.execute(
        "UPDATE tareas SET datos=datos||'{\"descartada\":true}'::jsonb, "
        "estado=CASE WHEN estado='pendiente' THEN 'cancelada' ELSE estado END, "
        "terminada_en=CASE WHEN estado='pendiente' THEN now() ELSE terminada_en END "
        "WHERE id=%s",
        (t["id"],),
    )
    auditar(c, "descartar_sugerencia", "tareas", str(t["id"]))
    return {"ok": True}
