"""Eliminar registros con vista previa del impacto.

POST /eliminacion/previa {recurso, ids}  → qué se eliminará, efectos, secuencias activas
                                           afectadas y, si no se puede, por qué (bloqueo).
POST /eliminacion        {recurso, ids}  → elimina (vuelve a calcular el bloqueo).

Recursos: contactos (varios a la vez), segmentos, listas, plantillas, empresas e imágenes.
Secuencias, campañas, respuestas y auditoría no se eliminan: se archivan o se conservan
como historial (lo exige el cumplimiento de bajas y la trazabilidad de envíos).

«Eliminar contacto de la aplicación» no es «quitar de esta lista» (eso solo lo saca de una
lista) ni «dar de baja» (eso suprime el correo para siempre). Eliminar cancela sus envíos
pendientes, borra sus datos personales y conserva cualquier baja existente.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException
from psycopg import errors as pg_errors

from app.db import Conn, Row, auditar, todos, uno

RECURSOS = ("contactos", "segmentos", "listas", "plantillas", "empresas", "imagenes")
MAX_IDS = 500


def _ids(body: Any) -> tuple[str, list[str]]:
    body = body if isinstance(body, dict) else {}
    recurso = body.get("recurso")
    if recurso not in RECURSOS:
        raise HTTPException(400, "No se puede eliminar ese tipo de registro")
    crudos = body.get("ids")
    if not isinstance(crudos, list) or not crudos:
        raise HTTPException(400, "Indique qué registros eliminar")
    if len(crudos) > MAX_IDS:
        raise HTTPException(400, f"Elimine como máximo {MAX_IDS} registros a la vez")
    if recurso != "contactos" and len(crudos) != 1:
        raise HTTPException(400, "Elimine estos registros de uno en uno")
    try:
        ids = list(dict.fromkeys(str(UUID(str(x))) for x in crudos))
    except ValueError as exc:
        raise HTTPException(400, "Identificador no válido") from exc
    return recurso, ids


def _secuencias_activas(c: Conn, where: str, params: tuple[Any, ...]) -> list[Row]:
    return todos(
        c,
        "SELECT ca.id, ca.nombre, ca.estado, ca.secuencia_id FROM campanas ca "
        f"WHERE {where} AND ca.estado='activa' ORDER BY ca.nombre",
        params,
    )


def previa(c: Conn, body: Any) -> dict[str, Any]:
    recurso, ids = _ids(body)
    return {"recurso": recurso, "ids": ids, **_impacto(c, recurso, ids)}


def _impacto(c: Conn, recurso: str, ids: list[str]) -> dict[str, Any]:  # noqa: C901
    efectos: list[str] = []
    bloqueo: str | None = None
    activas: list[Row] = []
    if recurso == "contactos":
        filas = todos(
            c,
            "SELECT id, nombre, correo::text AS correo, anonimizado_en FROM contactos "
            "WHERE id=ANY(%s::uuid[]) ORDER BY nombre",
            (ids,),
        )
        if len(filas) != len(ids):
            raise HTTPException(404, "Algún contacto ya no existe")
        vivos = [f for f in filas if not f["anonimizado_en"]]
        if not vivos:
            bloqueo = "Ya estaba eliminado"
        nombres = [f"{f['nombre']} ({f['correo'] or 'sin correo'})" for f in vivos]
        activas = todos(
            c,
            """SELECT ca.id, ca.nombre, ca.estado, ca.secuencia_id, count(*) AS contactos
               FROM inscripciones i JOIN campanas ca ON ca.id=i.campana_id
               WHERE i.contacto_id=ANY(%s::uuid[]) AND i.estado<>'finalizada'
                 AND ca.estado='activa' GROUP BY ca.id ORDER BY ca.nombre""",
            (ids,),
        )
        n = uno(
            c,
            """SELECT
                 (SELECT count(*) FROM mensajes WHERE contacto_id=ANY(%s::uuid[])
                    AND estado='programado') AS programados,
                 (SELECT count(*) FROM inscripciones WHERE contacto_id=ANY(%s::uuid[])
                    AND estado<>'finalizada') AS inscripciones,
                 (SELECT count(DISTINCT lista_id) FROM lista_miembros
                    WHERE contacto_id=ANY(%s::uuid[]) AND activo) AS listas,
                 (SELECT count(*) FROM contactos ct WHERE ct.id=ANY(%s::uuid[])
                    AND contacto_bloqueado(ct.id) AND ct.anonimizado_en IS NULL) AS con_baja""",
            (ids, ids, ids, ids),
        )
        if n["programados"]:
            efectos.append(f"Se cancelan {n['programados']} correo(s) programado(s).")
        if n["inscripciones"]:
            efectos.append(f"Sale de {n['inscripciones']} secuencia(s) en curso.")
        if n["listas"]:
            efectos.append(f"Sale de {n['listas']} lista(s) de envío.")
        efectos.append(
            "Se borran sus datos personales (nombre, correo, teléfono, evidencias). Los totales "
            "de campañas y el historial quedan anónimos."
        )
        if n["con_baja"]:
            efectos.append(
                f"{n['con_baja']} tiene(n) baja registrada: la baja se conserva y, si se vuelve "
                "a importar, ese correo seguirá sin recibir mensajes."
            )
        efectos.append(
            "No es una baja: si no se había dado de baja, el correo podrá importarse de nuevo."
        )
        titulo = nombres[0] if len(nombres) == 1 else f"{len(nombres)} contactos"
        detalle = nombres[:10] + ([f"… y {len(nombres) - 10} más"] if len(nombres) > 10 else [])
    elif recurso == "segmentos":
        s = uno(c, "SELECT id, nombre FROM segmentos WHERE id=%s", (ids[0],), False)
        if not s:
            raise HTTPException(404, "Segmento no encontrado")
        titulo, detalle = s["nombre"], []
        usan = todos(
            c, "SELECT nombre, estado FROM campanas WHERE segmento_id=%s ORDER BY nombre", (ids[0],)
        )
        activas = _secuencias_activas(c, "ca.segmento_id=%s", (ids[0],))
        listas = todos(
            c, "SELECT nombre FROM listas WHERE segmento_id=%s ORDER BY nombre", (ids[0],)
        )
        if usan:
            bloqueo = (
                "Lo usan como audiencia: "
                + ", ".join(f"«{u['nombre']}» ({u['estado']})" for u in usan)
                + ". Cambie su audiencia o archive el segmento."
            )
        elif listas:
            bloqueo = (
                "Lo siguen las listas vinculadas: "
                + ", ".join(f"«{x['nombre']}»" for x in listas)
                + ". Elimínelas o cree listas estáticas antes."
            )
        efectos.append("Los contactos no se eliminan: solo desaparece la regla de selección.")
    elif recurso == "listas":
        s = uno(c, "SELECT id, nombre FROM listas WHERE id=%s", (ids[0],), False)
        if not s:
            raise HTTPException(404, "Lista no encontrada")
        titulo, detalle = s["nombre"], []
        usan = todos(
            c, "SELECT nombre, estado FROM campanas WHERE lista_id=%s ORDER BY nombre", (ids[0],)
        )
        activas = _secuencias_activas(c, "ca.lista_id=%s", (ids[0],))
        if usan:
            bloqueo = (
                "La usan como audiencia: "
                + ", ".join(f"«{u['nombre']}» ({u['estado']})" for u in usan)
                + ". Cambie su audiencia o archive la lista."
            )
        en_reglas = todos(
            c,
            "SELECT nombre FROM segmentos WHERE reglas::text LIKE %s ORDER BY nombre",
            ("%" + ids[0] + "%",),
        )
        if en_reglas:
            efectos.append(
                "Las reglas «Miembro de la lista» de estos segmentos dejarán de coincidir: "
                + ", ".join(f"«{x['nombre']}»" for x in en_reglas)
                + "."
            )
        efectos.append("Los contactos no se eliminan: solo la lista y su pertenencia.")
    elif recurso == "plantillas":
        s = uno(
            c, "SELECT id, nombre, predeterminada FROM plantillas WHERE id=%s", (ids[0],), False
        )
        if not s:
            raise HTTPException(404, "Plantilla no encontrada")
        titulo, detalle = s["nombre"], []
        if s["predeterminada"]:
            bloqueo = "Las plantillas de la galería de NEXUS no se eliminan."
        efectos.append(
            "Los correos que la usaron conservan su copia; lo aprobado y lo programado no cambia."
        )
    elif recurso == "empresas":
        s = uno(c, "SELECT id, razon_social FROM empresas WHERE id=%s", (ids[0],), False)
        if not s:
            raise HTTPException(404, "Empresa no encontrada")
        titulo, detalle = s["razon_social"], []
        n = uno(
            c,
            """SELECT (SELECT count(*) FROM contactos WHERE empresa_id=%s) AS contactos,
                      (SELECT count(*) FROM oportunidades WHERE empresa_id=%s) AS oportunidades,
                      (SELECT count(*) FROM empresas WHERE fusionada_en_id=%s) AS fusionadas""",
            (ids[0], ids[0], ids[0]),
        )
        if n["contactos"]:
            bloqueo = (
                f"Tiene {n['contactos']} contacto(s) (incluidos los eliminados, que guardan "
                "historial). Muévalos o fusione la empresa con otra en lugar de eliminarla."
            )
        elif n["oportunidades"] or n["fusionadas"]:
            bloqueo = "Tiene oportunidades o empresas fusionadas: fusiónela en lugar de eliminarla."
        efectos.append("Se borran también sus señales, puntajes y posibles duplicados.")
    else:  # imagenes
        s = uno(c, "SELECT id, nombre, ejemplo FROM imagenes WHERE id=%s", (ids[0],), False)
        if not s:
            raise HTTPException(404, "Imagen no encontrada")
        titulo, detalle = s["nombre"], []
        patron = "%/img/" + ids[0] + "%"
        uso = uno(
            c,
            """SELECT
                 (SELECT count(*) FROM plantillas WHERE cuerpo_html LIKE %s) AS plantillas,
                 (SELECT count(*) FROM pasos WHERE cuerpo_html LIKE %s) AS pasos,
                 (SELECT count(*) FROM propuesta_versiones WHERE contenido::text LIKE %s) AS versiones,
                 (SELECT count(*) FROM mensajes WHERE cuerpo_html LIKE %s) AS mensajes""",
            (patron, patron, patron, patron),
        )
        if s["ejemplo"]:
            bloqueo = "Las imágenes de ejemplo de NEXUS no se eliminan."
        elif any(uso.values()):
            bloqueo = (
                "La imagen está en uso"
                + (f": {uso['plantillas']} plantilla(s)" if uso["plantillas"] else "")
                + (
                    f", {uso['pasos'] + uso['versiones']} correo(s) de secuencias"
                    if uso["pasos"] or uso["versiones"]
                    else ""
                )
                + (
                    f", {uso['mensajes']} correo(s) ya enviados o programados (dejarían de verse)"
                    if uso["mensajes"]
                    else ""
                )
                + "."
            )
        efectos.append("Deja de estar disponible en /img.")
    if activas and recurso == "contactos":
        efectos.insert(
            0, "Afecta a secuencias activas: sus envíos pendientes para este contacto se cancelan."
        )
    return {
        "titulo": titulo,
        "detalle": detalle,
        "efectos": efectos,
        "secuenciasActivas": [
            {
                "id": str(a["id"]),
                "nombre": a["nombre"],
                "estado": a["estado"],
                "secuenciaId": str(a["secuencia_id"]) if a.get("secuencia_id") else None,
                **({"contactos": int(a["contactos"])} if "contactos" in a else {}),
            }
            for a in activas
        ],
        "bloqueo": bloqueo,
        "puedeEliminar": bloqueo is None,
    }


def eliminar(c: Conn, body: Any) -> dict[str, Any]:
    recurso, ids = _ids(body)
    imp = _impacto(c, recurso, ids)
    if imp["bloqueo"]:
        raise HTTPException(409, imp["bloqueo"])
    if recurso == "contactos":
        for i in ids:
            c.execute("SELECT eliminar_contacto(%s, %s)", (i, "eliminado desde la aplicación"))
            auditar(c, "eliminar_contacto", "contactos", i)
        return {"ok": True, "eliminados": len(ids)}
    tabla = recurso
    try:
        with c.transaction():
            r = c.execute(f"DELETE FROM {tabla} WHERE id=%s RETURNING id", (ids[0],)).fetchone()  # noqa: S608
    except pg_errors.ForeignKeyViolation as exc:
        raise HTTPException(
            409, "Otro registro todavía lo usa; archívelo en lugar de eliminarlo"
        ) from exc
    if not r:
        raise HTTPException(404, "Registro no encontrado")
    auditar(c, "eliminar_" + recurso.rstrip("s"), recurso, ids[0])
    return {"ok": True, "eliminados": 1}
