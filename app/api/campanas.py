from typing import Any

from fastapi import HTTPException
from psycopg.types.json import Jsonb

from app.api import datos, lecturas
from app.config import config
from app.db import Conn, Row, actualizar, encolar, insertar, obtener, todos, uno
from app.dominio.reglas import (
    VentanaIncompatible,
    reglas_validas,
    siguiente_ventana,
    validar_secuencia,
    ventanas_compatibles,
)
from app.esquemas.entradas import Secuencia
from app.esquemas.serializar import serializar
from app.motor.pausas import proximo_al_reanudar
from app.motor.plantillas import render


def nueva_propuesta(
    c: Conn,
    agente: str,
    tipo: str,
    resumen: str,
    contenido: Any,
    objetivo: Row | None = None,
    autor: str | None = None,
) -> Row:
    r = insertar(
        c, "propuestas", {"agente": agente, "tipo": tipo, "resumen": resumen, **(objetivo or {})}
    )
    insertar(
        c,
        "propuesta_versiones",
        {
            "propuesta_id": r["id"],
            "version": 1,
            "contenido": Jsonb(serializar(contenido)),
            "autor": autor or ("agente" if agente else "admin"),
        },
    )
    return r


def versionar(
    c: Conn, p: Row, contenido: Any, resumen: str | None = None, autor: str = "admin"
) -> None:
    version = p["version_actual"] + 1
    insertar(
        c,
        "propuesta_versiones",
        {
            "propuesta_id": p["id"],
            "version": version,
            "contenido": Jsonb(serializar(contenido)),
            "autor": autor,
        },
    )
    actualizar(
        c,
        "propuestas",
        str(p["id"]),
        {"version_actual": version, "estado": "pendiente", "resumen": resumen or p["resumen"]},
    )


def revisar_pendientes(c: Conn) -> None:
    """Campañas que quedaron en revisión sin aprobación (cambió la marca o el segmento).

    La secuencia en vivo ya no se edita fuera de la aprobación, así que nunca se crean
    versiones a partir de ella: si la última versión ya estaba aprobada, se crea una copia
    de esa misma versión para poder aprobarla otra vez. Un borrador sin decidir (más nuevo)
    se deja tal cual: nunca se tapa con contenido viejo.
    """
    for cam in todos(
        c,
        "SELECT * FROM campanas WHERE estado='en_revision' AND aprobacion_vigente_id IS NULL FOR UPDATE",
    ):
        p = uno(
            c,
            "SELECT * FROM propuestas WHERE tipo='campana' AND objetivo_campana_id=%s ORDER BY creado_en DESC LIMIT 1 FOR UPDATE",
            (cam["id"],),
            False,
        )
        if not p:
            seq = serializar(
                lecturas.secuencia(c, obtener(c, "secuencias", str(cam["secuencia_id"])))
            )
            nueva_propuesta(
                c,
                "estratega_redactor",
                "campana",
                "Revisar campaña: " + cam["nombre"],
                seq,
                {"objetivo_campana_id": cam["id"]},
                autor="admin",
            )
            continue
        last = uno(
            c,
            "SELECT contenido,decision FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
            (p["id"], p["version_actual"]),
        )
        if last["decision"] == "aprobada":
            versionar(c, p, last["contenido"], autor="admin")


def guardar_secuencia(c: Conn, ident: str, body: Row, revisar: bool = True) -> None:
    r = obtener(c, "secuencias", ident, True)
    s = Secuencia.model_validate(body).model_dump(by_alias=True, exclude_none=True)
    marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
    errors = validar_secuencia(s, marca["prohibidas"])
    if errors:
        raise HTTPException(400, ". ".join(errors))
    v = s["ventanaEnvio"]
    actualizar(
        c,
        "secuencias",
        ident,
        {
            "nombre": s["nombre"],
            "objetivo": s["objetivo"],
            "llamada_accion": s["llamadaAccion"],
            "metricas_exito": s["metricasExito"],
            "ventana_dias": v["dias"],
            "ventana_desde": v["desde"],
            "ventana_hasta": v["hasta"],
            "version": r["version"] + 1,
        },
    )
    c.execute("DELETE FROM pasos WHERE secuencia_id=%s", (ident,))
    for i, p in enumerate(s["pasos"], 1):
        insertar(
            c,
            "pasos",
            {
                "secuencia_id": ident,
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
    if revisar:
        revisar_pendientes(c)


def aprobar(c: Conn, p: Row, admin: str) -> None:
    if p["estado"] == "aprobada":
        raise HTTPException(400, "Propuesta ya aprobada")
    if p["tipo"] == "correo_inferido":
        raise HTTPException(
            400, "Un correo inferido requiere verificación; no se aprueba para envíos"
        )
    v = uno(
        c,
        "SELECT * FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s FOR UPDATE",
        (p["id"], p["version_actual"]),
    )
    if v["decision"]:
        raise HTTPException(400, "Esta versión ya fue decidida; cree una versión nueva")
    if p["tipo"] == "campana":
        from app.api.rutas_correo import revision

        # La misma lista de verificación que ve el administrador decide si se aprueba.
        rev = revision(c, p, v)
        if not rev["puedeAprobar"]:
            raise HTTPException(400, ". ".join(rev["bloqueos"] + rev["errores"]))
        cam = obtener(c, "campanas", str(p["objetivo_campana_id"]), True)
        seq = obtener(c, "secuencias", str(cam["secuencia_id"]), True)
        if isinstance(v["contenido"], dict) and "pasos" in v["contenido"]:
            propuesta_actual = Secuencia.model_validate(v["contenido"]).model_dump(
                by_alias=True, exclude_none=True
            )
            secuencia_actual = Secuencia.model_validate(
                serializar(lecturas.secuencia(c, seq))
            ).model_dump(by_alias=True, exclude_none=True)
            if propuesta_actual != secuencia_actual:
                guardar_secuencia(c, str(seq["id"]), propuesta_actual, revisar=False)
                seq = obtener(c, "secuencias", str(seq["id"]), True)

        seg = obtener(c, "segmentos", str(cam["segmento_id"]), True)
        marca = uno(c, "SELECT * FROM configuracion WHERE clave='marca' FOR UPDATE")
        limits = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
        s = serializar(lecturas.secuencia(c, seq))
        errors = validar_secuencia(s, marca["valor"]["prohibidas"])
        if seg["estado"] != "aprobado":
            errors.append("El segmento requiere aprobación")
        if errors:
            raise HTTPException(400, ". ".join(errors))
        try:
            siguiente_ventana(datos.ahora(), s["ventanaEnvio"], limits["ventana"])
        except VentanaIncompatible as exc:
            raise HTTPException(
                400,
                "La ventana de envío de la secuencia no coincide con el horario global "
                "(Configuración → Límites y horarios)",
            ) from exc
        congelado = {
            "secuencia": s,
            "reglas": seg["reglas"],
            "versionSegmento": seg["version"],
            "marca": marca["valor"],
            "versionMarca": marca["version"],
        }
        a = insertar(
            c,
            "aprobaciones_campana",
            {
                "campana_id": cam["id"],
                "propuesta_version_id": v["id"],
                "version_secuencia": seq["version"],
                "version_segmento": seg["version"],
                "version_marca": marca["version"],
                "contenido_congelado": Jsonb(congelado),
                "aprobada_por": admin,
            },
        )
        actualizar(
            c, "campanas", str(cam["id"]), {"aprobacion_vigente_id": a["id"], "estado": "aprobada"}
        )
    elif p["tipo"] == "segmento":
        actualizar(
            c,
            "segmentos",
            str(p["objetivo_segmento_id"]),
            {"estado": "aprobado", "aprobado_por": admin, "aprobado_en": datos.ahora()},
        )
    elif p["tipo"] == "enriquecimiento":
        contenido = v["contenido"]
        if not isinstance(contenido, dict) or not isinstance(contenido.get("campos"), dict):
            raise HTTPException(400, "La propuesta requiere campos estructurados con evidencia")
        target = p["objetivo_empresa_id"] or p["objetivo_contacto_id"]
        fields = contenido["campos"]
        fuentes = todos(c, "SELECT * FROM evidencias WHERE propuesta_id=%s", (p["id"],))
        if any(
            not any(e["campo"] == k and e["valor"] == str(val) for e in fuentes)
            for k, val in fields.items()
        ):
            raise HTTPException(400, "Cada campo requiere su evidencia")
        if p["objetivo_empresa_id"]:
            datos.guardar_empresa(c, fields, str(target))
            tipo = "empresa_id"
        else:
            datos.guardar_contacto(c, fields, str(target))
            tipo = "contacto_id"
        for e in fuentes:
            insertar(
                c,
                "evidencias",
                {
                    tipo: target,
                    **{
                        k: e[k]
                        for k in (
                            "campo",
                            "valor",
                            "fuente",
                            "url",
                            "cita",
                            "estado",
                            "cita_verificada_en",
                        )
                    },
                },
            )
    elif p["tipo"] == "mejora":
        x = v["contenido"]
        if (
            not isinstance(x, dict)
            or x.get("objetivo") not in ("campana", "secuencia", "segmento")
            or not isinstance(x.get("cambios"), dict)
        ):
            raise HTTPException(400, "La mejora requiere objetivo, id y cambios estructurados")
        if x["objetivo"] == "secuencia":
            # Aplicar una mejora crea un borrador en la secuencia; lo aprobado no cambia.
            from app.api import secuencias as sec

            borrador = sec.borrador(c, str(x["id"]))["contenido"]
            sec.guardar_borrador(c, str(x["id"]), {**borrador, **x["cambios"]})
        elif x["objetivo"] == "segmento":
            modificar(c, ["segmentos", x["id"]], "PATCH", x["cambios"], admin)
        else:
            allowed = {k: v for k, v in x["cambios"].items() if k in ("nombre", "topeDiario")}
            if not allowed:
                raise HTTPException(400, "La mejora no tiene cambios admitidos")
            actualizar(
                c,
                "campanas",
                x["id"],
                {"tope_diario" if k == "topeDiario" else k: v for k, v in allowed.items()},
            )
            c.execute(
                "SELECT revocar_aprobaciones(ARRAY[%s]::uuid[],%s)",
                (x["id"], "Mejora aprobada: requiere revisar campaña"),
            )
            revisar_pendientes(c)
    actualizar(
        c,
        "propuesta_versiones",
        str(v["id"]),
        {"decision": "aprobada", "decidida_por": admin, "decidida_en": datos.ahora()},
    )
    actualizar(c, "propuestas", str(p["id"]), {"estado": "aprobada"})


def modificar(c: Conn, p: list[str], method: str, body: Any, admin: str) -> Any:
    recurso = p[0]
    ident = p[1] if len(p) > 1 else None
    action = p[2] if len(p) > 2 else None
    if recurso == "segmentos":
        if ident == "previsualizar" and method == "POST":
            muestra = lecturas.datos_segmento(c, reglas_validas(body))
            return {"conteo": len(muestra), "muestra": muestra[:8]}
        if not ident and method == "POST":
            insertar(
                c,
                "segmentos",
                {
                    "nombre": body["nombre"],
                    "reglas": Jsonb(reglas_validas(body["reglas"])),
                    "justificacion": body.get("justificacion", ""),
                    "propuesto_por": "admin",
                },
            )
        elif ident and action == "aprobar" and method == "POST":
            actualizar(
                c,
                "segmentos",
                ident,
                {"estado": "aprobado", "aprobado_por": admin, "aprobado_en": datos.ahora()},
            )
            # La propuesta del agente para este segmento queda aprobada con él.
            for prop in todos(
                c,
                "SELECT * FROM propuestas WHERE tipo='segmento' AND objetivo_segmento_id=%s "
                "AND estado IN ('pendiente','editada') FOR UPDATE",
                (ident,),
            ):
                v = uno(
                    c,
                    "SELECT id,decision FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
                    (prop["id"], prop["version_actual"]),
                )
                if v["decision"] is None:
                    actualizar(
                        c,
                        "propuesta_versiones",
                        str(v["id"]),
                        {
                            "decision": "aprobada",
                            "decidida_por": admin,
                            "decidida_en": datos.ahora(),
                        },
                    )
                actualizar(c, "propuestas", str(prop["id"]), {"estado": "aprobada"})
        elif ident and method == "PATCH" and not action:
            if body == {"estado": "archivado"}:
                actualizar(c, "segmentos", ident, {"estado": "archivado"})
                cams = todos(
                    c,
                    "SELECT id FROM campanas WHERE segmento_id=%s AND aprobacion_vigente_id IS NOT NULL",
                    (ident,),
                )
                for cam in cams:
                    c.execute(
                        "SELECT revocar_aprobaciones(ARRAY[%s]::uuid[],%s)",
                        (cam["id"], "Segmento archivado"),
                    )
            else:
                values = {k: body[k] for k in ("nombre", "justificacion") if k in body}
                if "reglas" in body:
                    values["reglas"] = Jsonb(reglas_validas(body["reglas"]))
                actualizar(c, "segmentos", ident, values)
            revisar_pendientes(c)
        else:
            raise HTTPException(404, "Ruta no encontrada")
    elif recurso == "secuencias" and ident:
        if method == "PUT" and not action:
            guardar_secuencia(c, ident, body)
        elif method == "POST" and action == "previsualizar":
            ct = serializar(lecturas.contacto(c, obtener(c, "contactos", body["contactoId"])))
            e = serializar(lecturas.empresa(c, obtener(c, "empresas", ct["empresaId"])))
            seq = serializar(lecturas.secuencia(c, obtener(c, "secuencias", ident)))
            marca = uno(c, "SELECT valor FROM configuracion WHERE clave='marca'")["valor"]
            return render(seq, ct, e, marca, "[Enlace de baja al enviar]", config().diagnostico_url)
        else:
            raise HTTPException(404, "Ruta no encontrada")
    elif recurso == "propuestas" and ident:
        from app.api import rutas_correo

        prop = obtener(c, "propuestas", ident, True)
        if action == "aprobar" and method == "POST":
            aprobar(c, prop, admin)
        elif action == "previsualizar" and method == "POST":
            return rutas_correo.previsualizar(c, prop, body)
        elif action == "restaurar" and method == "POST":
            return rutas_correo.restaurar(c, prop, body)
        elif action == "rechazar" and method == "POST":
            text = str(body.get("comentario", "")).strip()
            if len(text) < 3:
                raise HTTPException(400, "El comentario requiere al menos 3 caracteres")
            v = uno(
                c,
                "SELECT * FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
                (ident, prop["version_actual"]),
            )
            if v["decision"]:
                raise HTTPException(400, "Esta versión ya fue decidida; cree una versión nueva")
            actualizar(
                c,
                "propuesta_versiones",
                str(v["id"]),
                {
                    "decision": "rechazada",
                    "comentario": text,
                    "decidida_por": admin,
                    "decidida_en": datos.ahora(),
                },
            )
            actualizar(c, "propuestas", ident, {"estado": "rechazada"})
            pedir = body.get("pedirNuevaVersion", True) is not False
            if pedir and prop["agente"] == "estratega_redactor" and prop["tipo"] == "campana":
                encolar(c, "revisar_campana", {"propuestaId": ident})
        elif method == "PUT" and not action:
            if not isinstance(body, dict) or ("resumen" not in body and "contenido" not in body):
                raise HTTPException(400, "Indique el resumen o el contenido")
            resumen = None
            if "resumen" in body:
                resumen = str(body.get("resumen") or "").strip()
                if len(resumen) < 5:
                    raise HTTPException(400, "El resumen requiere al menos 5 caracteres")
            if "contenido" in body:
                contenido = rutas_correo.contenido_editado(c, prop, body["contenido"])
            else:
                contenido = uno(
                    c,
                    "SELECT contenido FROM propuesta_versiones WHERE propuesta_id=%s AND version=%s",
                    (ident, prop["version_actual"]),
                )["contenido"]
            # Un borrador nuevo no cambia lo que se envía: lo aprobado sigue vigente hasta
            # que se apruebe la versión nueva.
            versionar(c, prop, contenido, resumen)
            return {"ok": True, "version": prop["version_actual"] + 1}
        else:
            raise HTTPException(404, "Ruta no encontrada")
    elif recurso == "campanas" and ident and method == "POST":
        cam = obtener(c, "campanas", ident, True)
        if action == "inscribir":
            if cam["estado"] not in ("aprobada", "activa", "pausada"):
                raise HTTPException(400, "La campaña requiere aprobación")
            seg = obtener(c, "segmentos", str(cam["segmento_id"]))
            candidates = set(lecturas.ids_segmento(c, seg["reglas"]))
            if not isinstance(body, list):
                raise HTTPException(400, "Envíe una lista de contactos")
            for cid in body:
                if (
                    cid not in candidates
                    or uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (cid,))["motivo"]
                ):
                    raise HTTPException(400, "Contacto no elegible o fuera del segmento")
                c.execute(
                    "INSERT INTO inscripciones(campana_id,contacto_id) VALUES(%s,%s) ON CONFLICT(campana_id,contacto_id) DO NOTHING",
                    (ident, cid),
                )
        elif action == "reanudar":
            a = uno(
                c,
                "SELECT * FROM aprobaciones_campana WHERE id=%s AND revocada_en IS NULL",
                (cam["aprobacion_vigente_id"],),
                False,
            )
            if not a or cam["estado"] not in ("aprobada", "pausada", "activa"):
                raise HTTPException(400, "La campaña requiere aprobación vigente")
            if obtener(c, "integraciones", "gmail")["estado"] != "conectada":
                raise HTTPException(400, "Gmail está desconectado")
            limits = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
            congelada = a["contenido_congelado"]["secuencia"]
            if not ventanas_compatibles(congelada["ventanaEnvio"], limits["ventana"]):
                raise HTTPException(
                    400,
                    "La ventana de envío aprobada no coincide con el horario global "
                    "(Configuración → Límites y horarios). Ajuste uno de los dos antes de reanudar.",
                )
            actualizar(c, "campanas", ident, {"estado": "activa"})
            seg = obtener(c, "segmentos", str(cam["segmento_id"]))
            ids = set(lecturas.ids_segmento(c, seg["reglas"]))
            ahora = datos.ahora()
            for ins in todos(
                c,
                "SELECT * FROM inscripciones WHERE campana_id=%s AND motivo_salida IS NULL",
                (ident,),
            ):
                cid = str(ins["contacto_id"])
                if (
                    cid in ids
                    and uno(c, "SELECT motivo_no_elegible(%s) AS motivo", (cid,))["motivo"] is None
                ):
                    actualizar(
                        c,
                        "inscripciones",
                        str(ins["id"]),
                        {
                            "estado": "activa",
                            "proximo_envio": proximo_al_reanudar(
                                c, ins, congelada, limits["ventana"], ahora
                            ),
                        },
                    )
        elif action in ("pausar", "detener"):
            stop = action == "detener"
            actualizar(c, "campanas", ident, {"estado": "finalizada" if stop else "pausada"})
            for ins in todos(
                c,
                "SELECT id FROM inscripciones WHERE campana_id=%s AND estado<>'finalizada'",
                (ident,),
            ):
                values = {"estado": "finalizada" if stop else "pausada", "proximo_envio": None}
                if stop:
                    values.update(motivo_salida="manual", finalizada_en=datos.ahora())
                actualizar(c, "inscripciones", str(ins["id"]), values)
            c.execute(
                "UPDATE mensajes m SET estado='cancelado' FROM inscripciones i WHERE m.inscripcion_id=i.id AND i.campana_id=%s AND m.estado='programado'",
                (ident,),
            )
        else:
            raise HTTPException(404, "Ruta no encontrada")
    else:
        raise HTTPException(404, "Ruta no encontrada")
    return {"ok": True}
