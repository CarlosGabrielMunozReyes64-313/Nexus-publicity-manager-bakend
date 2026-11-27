"""Audiencia de una campaña (segmento o lista) y activación de envíos.

Una sola lógica para la vista previa, la revisión, la aprobación, la inscripción y la
incorporación de nuevos miembros:

- candidatos(): contactos de la audiencia (reglas del segmento o miembros de la lista),
  sin duplicados.
- elegibles(): de esos, los que hoy pueden recibir correos según la base de datos
  (motivo_no_elegible: supresiones, exclusión, base legal, correo válido, cliente).
- activar(): deja la campaña activa y programa el primer paso de cada elegible
  (inscripción activa con próximo envío). El trabajador crea los mensajes respetando
  ventana, cuotas y separación. Nunca envía nada por sí misma.
"""

from collections import Counter
from datetime import datetime
from typing import Any

from app.api import lecturas
from app.db import Conn, Row, actualizar, auditar, obtener, todos, uno
from app.dominio.reglas import ventanas_compatibles
from app.motor.pausas import proximo_al_reanudar

SEGMENTO_SIN_APROBAR = "El segmento requiere aprobación"
LISTA_ARCHIVADA = "La lista de envío está archivada"
SIN_GMAIL = (
    "Gmail no está conectado: la aprobación quedó guardada, pero no se programó ningún envío. "
    "Conecte Gmail en Configuración → Integraciones y pulse «Programar envíos»."
)
SIN_ELEGIBLES = (
    "No hay destinatarios elegibles en la audiencia: revise base legal, verificación de "
    "correo y bajas de sus contactos."
)


def describir(c: Conn, cam: Row) -> dict[str, Any]:
    """Tipo, nombre y estado de la audiencia; «bloqueo» si impide aprobar o programar."""
    if cam.get("lista_id"):
        lista = obtener(c, "listas", str(cam["lista_id"]))
        bloqueo = LISTA_ARCHIVADA if lista["archivada_en"] else None
        seg = None
        if lista["tipo"] == "vinculada":
            seg = obtener(c, "segmentos", str(lista["segmento_id"]))
            if not bloqueo and seg["estado"] != "aprobado":
                bloqueo = SEGMENTO_SIN_APROBAR
        return {
            "tipo": "lista",
            "id": str(lista["id"]),
            "nombre": lista["nombre"],
            "listaTipo": lista["tipo"],
            "segmentoId": str(seg["id"]) if seg else None,
            "segmentoVersion": seg["version"] if seg else 0,
            "reglas": seg["reglas"] if seg else [],
            "bloqueo": bloqueo,
            "incorporarNuevos": bool(cam.get("incorporar_nuevos")),
        }
    seg = obtener(c, "segmentos", str(cam["segmento_id"]))
    return {
        "tipo": "segmento",
        "id": str(seg["id"]),
        "nombre": seg["nombre"],
        "segmentoId": str(seg["id"]),
        "segmentoVersion": seg["version"],
        "estadoSegmento": seg["estado"],
        "reglas": seg["reglas"],
        "bloqueo": None if seg["estado"] == "aprobado" else SEGMENTO_SIN_APROBAR,
        "incorporarNuevos": bool(cam.get("incorporar_nuevos")),
    }


def ids_lista(c: Conn, lista: Row) -> list[str]:
    if lista["tipo"] == "vinculada":
        seg = obtener(c, "segmentos", str(lista["segmento_id"]))
        return lecturas.ids_segmento(c, seg["reglas"])
    return [
        str(r["contacto_id"])
        for r in todos(
            c,
            """SELECT lm.contacto_id FROM lista_miembros lm JOIN contactos ct ON ct.id=lm.contacto_id
            WHERE lm.lista_id=%s AND lm.activo AND ct.anonimizado_en IS NULL
            ORDER BY lm.agregado_en, lm.contacto_id""",
            (lista["id"],),
        )
    ]


def candidatos(c: Conn, cam: Row) -> list[str]:
    if cam.get("lista_id"):
        ids = ids_lista(c, obtener(c, "listas", str(cam["lista_id"])))
    else:
        seg = obtener(c, "segmentos", str(cam["segmento_id"]))
        ids = lecturas.ids_segmento(c, seg["reglas"])
    return list(dict.fromkeys(ids))  # sin duplicados, conserva el orden


def filtrar_elegibles(c: Conn, ids: list[str]) -> list[str]:
    if not ids:
        return []
    return [
        str(r["id"])
        for r in todos(
            c,
            "SELECT x.id FROM unnest(%s::uuid[]) WITH ORDINALITY AS x(id,n) "
            "WHERE motivo_no_elegible(x.id) IS NULL ORDER BY x.n",
            (ids,),
        )
    ]


def elegibles(c: Conn, cam: Row) -> list[str]:
    return filtrar_elegibles(c, candidatos(c, cam))


def motivos(c: Conn, ids: list[str]) -> dict[str, int]:
    """Cuántos candidatos quedan fuera y por qué (sin datos personales)."""
    if not ids:
        return {}
    filas = todos(
        c,
        "SELECT motivo_no_elegible(x.id) AS motivo FROM unnest(%s::uuid[]) AS x(id)",
        (ids,),
    )
    return dict(Counter(r["motivo"] for r in filas if r["motivo"]))


def activar(c: Conn, cam_id: str, ahora: datetime, accion: str = "programar_envios") -> Row:
    """Activa la campaña y programa el primer paso de cada destinatario elegible.

    Idempotente: no duplica inscripciones (UNIQUE campaña-contacto) ni toca las que ya
    terminaron. Las inscripciones pausadas retoman su paso conservando la espera.
    Devuelve {activada, inscritos, reanudadas, elegibles, motivo}.
    """
    cam = obtener(c, "campanas", cam_id, True)
    resultado: Row = {"activada": False, "inscritos": 0, "reanudadas": 0, "elegibles": 0}

    def no_programada(motivo: str) -> Row:
        actualizar(c, "campanas", cam_id, {"motivo_no_programada": motivo})
        return {**resultado, "motivo": motivo}

    a = uno(
        c,
        "SELECT * FROM aprobaciones_campana WHERE id=%s AND revocada_en IS NULL",
        (cam["aprobacion_vigente_id"],),
        False,
    )
    if not a or cam["estado"] not in ("aprobada", "pausada", "activa"):
        return no_programada("La campaña requiere una aprobación vigente")
    if obtener(c, "integraciones", "gmail")["estado"] != "conectada":
        return no_programada(SIN_GMAIL)
    limites = uno(c, "SELECT valor FROM configuracion WHERE clave='limites'")["valor"]
    congelada = a["contenido_congelado"]["secuencia"]
    if not ventanas_compatibles(congelada["ventanaEnvio"], limites["ventana"]):
        return no_programada(
            "La ventana de envío aprobada no coincide con el horario global "
            "(Configuración → Límites y horarios)."
        )
    aud = describir(c, cam)
    if aud["bloqueo"]:
        return no_programada(aud["bloqueo"])
    ids = elegibles(c, cam)
    resultado["elegibles"] = len(ids)
    existentes = {
        str(r["contacto_id"]): r
        for r in todos(c, "SELECT * FROM inscripciones WHERE campana_id=%s FOR UPDATE", (cam_id,))
    }
    if not ids and not any(
        r["motivo_salida"] is None and r["estado"] != "finalizada" for r in existentes.values()
    ):
        return no_programada(SIN_ELEGIBLES)
    actualizar(
        c,
        "campanas",
        cam_id,
        {"estado": "activa", "programada_en": ahora, "motivo_no_programada": None},
    )
    elegible = set(ids)
    for ins in existentes.values():
        cid = str(ins["contacto_id"])
        if ins["estado"] == "finalizada" or ins["motivo_salida"] is not None:
            continue
        if ins["estado"] == "activa":
            continue
        if cid in elegible:
            actualizar(
                c,
                "inscripciones",
                str(ins["id"]),
                {
                    "estado": "activa",
                    "proximo_envio": proximo_al_reanudar(
                        c, ins, congelada, limites["ventana"], ahora
                    ),
                },
            )
            resultado["reanudadas"] += 1
    nuevos = [cid for cid in ids if cid not in existentes]
    if nuevos:
        r = c.execute(
            """INSERT INTO inscripciones(campana_id,contacto_id,estado,proximo_envio)
            SELECT %s, x.id, 'activa', %s FROM unnest(%s::uuid[]) AS x(id)
            ON CONFLICT (campana_id,contacto_id) DO NOTHING RETURNING id""",
            (cam_id, ahora, nuevos),
        ).fetchall()
        resultado["inscritos"] = len(r)
    resultado["activada"] = True
    auditar(c, accion, "campanas", cam_id)
    return resultado


def incorporar_nuevos(c: Conn, ahora: datetime) -> int:
    """Inscribe a los miembros nuevos de las campañas activas con incorporar_nuevos."""
    total = 0
    for cam in todos(
        c, "SELECT * FROM campanas WHERE estado='activa' AND incorporar_nuevos ORDER BY id"
    ):
        aud = describir(c, cam)
        if aud["bloqueo"]:
            continue
        ids = elegibles(c, cam)
        if not ids:
            continue
        r = c.execute(
            """INSERT INTO inscripciones(campana_id,contacto_id,estado,proximo_envio)
            SELECT %s, x.id, 'activa', %s FROM unnest(%s::uuid[]) AS x(id)
            WHERE NOT EXISTS (SELECT 1 FROM inscripciones i
                              WHERE i.campana_id=%s AND i.contacto_id=x.id)
            ON CONFLICT (campana_id,contacto_id) DO NOTHING RETURNING id""",
            (cam["id"], ahora, ids, cam["id"]),
        ).fetchall()
        total += len(r)
    return total
