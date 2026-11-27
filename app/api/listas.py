"""Listas de envío.

- Estática: guarda sus miembros. «Crear desde un segmento» copia los miembros actuales; si
  el segmento cambia después, la lista no cambia.
- Vinculada: mantiene la audiencia unida al segmento; sus miembros se calculan al usarla.
- Archivar no borra nada: la lista deja de poder elegirse y una campaña que la use no se
  puede aprobar ni programar hasta cambiar la audiencia.
- Las bajas retiran al contacto de las listas (disparador de supresiones) y, en cualquier
  caso, motivo_no_elegible excluye a un suprimido al programar.
"""

from typing import Any
from uuid import UUID

from fastapi import HTTPException

from app.api import datos, lecturas
from app.db import Conn, Row, actualizar, auditar, insertar, obtener, todos, uno
from app.motor import audiencia

MAX_MIEMBROS_POR_PETICION = 5000


def _nombre(body: Any) -> str:
    nombre = " ".join(str((body or {}).get("nombre") or "").split())
    if not 1 <= len(nombre) <= 120:
        raise HTTPException(400, "El nombre de la lista requiere entre 1 y 120 caracteres")
    return nombre


def _ids(valor: Any) -> list[str]:
    if not isinstance(valor, list) or not valor:
        raise HTTPException(400, "Envíe la lista de contactos")
    if len(valor) > MAX_MIEMBROS_POR_PETICION:
        raise HTTPException(400, f"Máximo {MAX_MIEMBROS_POR_PETICION} contactos por petición")
    out = []
    for x in valor:
        try:
            out.append(str(UUID(str(x))))
        except ValueError as exc:
            raise HTTPException(400, "Identificador de contacto no válido") from exc
    return list(dict.fromkeys(out))


def armar(c: Conn, filas: list[Row]) -> list[Row]:
    out = []
    conteos = (
        {
            str(r["lista_id"]): r
            for r in todos(
                c,
                """SELECT lista_id, count(*) FILTER (WHERE activo) AS activos,
                      count(*) FILTER (WHERE NOT activo) AS retirados
               FROM lista_miembros WHERE lista_id=ANY(%s::uuid[]) GROUP BY lista_id""",
                ([str(f["id"]) for f in filas],),
            )
        }
        if filas
        else {}
    )
    campanas: dict[str, list[Row]] = {}
    for r in (
        todos(
            c,
            "SELECT id,nombre,estado,lista_id,secuencia_id FROM campanas WHERE lista_id=ANY(%s::uuid[])",
            ([str(f["id"]) for f in filas],),
        )
        if filas
        else []
    ):
        campanas.setdefault(str(r["lista_id"]), []).append(
            {k: r[k] for k in ("id", "nombre", "estado", "secuencia_id")}
        )
    for f in filas:
        ids = audiencia.ids_lista(c, f)
        elegibles = audiencia.filtrar_elegibles(c, ids)
        k = conteos.get(str(f["id"]), {})
        out.append(
            {
                **f,
                "miembros": len(ids),
                "elegibles": len(elegibles),
                "retirados": int(k.get("retirados") or 0),
                "archivada": f["archivada_en"] is not None,
                "campanas": campanas.get(str(f["id"]), []),
            }
        )
    return out


def lista(c: Conn, params: Any) -> list[Row]:
    archivadas = str(params.get("archivadas", "")).lower() == "true"
    filas = todos(
        c,
        "SELECT * FROM listas"
        + ("" if archivadas else " WHERE archivada_en IS NULL")
        + " ORDER BY lower(nombre), id",
    )
    return armar(c, filas)


def detalle(c: Conn, ident: str) -> Row:
    return armar(c, [obtener(c, "listas", ident)])[0]


def miembros(c: Conn, ident: str, params: Any) -> dict[str, Any]:
    lst = obtener(c, "listas", ident)
    ids = audiencia.ids_lista(c, lst)
    limite = max(1, min(500, int(params.get("limite") or 200)))
    desde = max(0, int(params.get("desplazamiento") or 0))
    pagina = ids[desde : desde + limite]
    filas = (
        todos(
            c,
            """SELECT ct.id, ct.nombre, ct.correo::text AS correo, ct.cargo, e.razon_social AS empresa,
                      motivo_no_elegible(ct.id) AS motivo
               FROM unnest(%s::uuid[]) WITH ORDINALITY AS x(id,n)
               JOIN contactos ct ON ct.id=x.id JOIN empresas e ON e.id=ct.empresa_id
               ORDER BY x.n""",
            (pagina,),
        )
        if pagina
        else []
    )
    retirados = todos(
        c,
        """SELECT lm.contacto_id AS id, ct.nombre, lm.retirado_en, lm.motivo_retiro
           FROM lista_miembros lm JOIN contactos ct ON ct.id=lm.contacto_id
           WHERE lm.lista_id=%s AND NOT lm.activo ORDER BY lm.retirado_en DESC LIMIT 100""",
        (ident,),
    )
    return {
        "total": len(ids),
        "miembros": [{**f, "elegible": f["motivo"] is None} for f in filas],
        "retirados": retirados,
    }


def crear(c: Conn, body: Any) -> Row:
    body = body if isinstance(body, dict) else {}
    nombre = _nombre(body)
    if uno(
        c,
        "SELECT 1 AS si FROM listas WHERE lower(nombre)=lower(%s) AND archivada_en IS NULL",
        (nombre,),
        False,
    ):
        raise HTTPException(409, "Ya existe una lista activa con ese nombre")
    origen = body.get("segmentoId")
    modo = body.get("modo") or "estatica"
    if modo not in ("estatica", "vinculada"):
        raise HTTPException(400, "Modo de lista no válido")
    if modo == "vinculada" and not origen:
        raise HTTPException(400, "Una lista vinculada necesita un segmento")
    seg = obtener(c, "segmentos", str(origen)) if origen else None
    lst = insertar(
        c,
        "listas",
        {
            "nombre": nombre,
            "tipo": modo,
            "segmento_id": seg["id"] if seg and modo == "vinculada" else None,
            "descripcion": str(body.get("descripcion") or "")[:500],
        },
    )
    copiados = 0
    if seg and modo == "estatica":
        # Foto de los miembros actuales del segmento: no cambia si el segmento cambia.
        ids = lecturas.ids_segmento(c, seg["reglas"])
        copiados = agregar_ids(c, str(lst["id"]), ids)
    elif body.get("contactoIds"):
        copiados = agregar_ids(c, str(lst["id"]), _ids(body["contactoIds"]))
    auditar(c, "crear_lista", "listas", str(lst["id"]))
    return {"ok": True, "id": str(lst["id"]), "miembros": copiados}


def agregar_ids(c: Conn, ident: str, ids: list[str]) -> int:
    """Añade o reactiva miembros (salvo retirados por baja). Ignora suprimidos."""
    if not ids:
        return 0
    filas = c.execute(
        """INSERT INTO lista_miembros(lista_id,contacto_id)
           SELECT %s, ct.id FROM contactos ct WHERE ct.id=ANY(%s::uuid[])
             AND ct.anonimizado_en IS NULL AND NOT contacto_bloqueado(ct.id)
           ON CONFLICT (lista_id,contacto_id) DO UPDATE
             SET activo=true, retirado_en=NULL, motivo_retiro=NULL, agregado_en=now()
             WHERE lista_miembros.motivo_retiro IS DISTINCT FROM 'baja'
           RETURNING contacto_id""",
        (ident, ids),
    ).fetchall()
    return len(filas)


def modificar(c: Conn, p: list[str], method: str, body: Any) -> Any:
    if len(p) == 1 and method == "POST":
        return crear(c, body)
    ident = p[1]
    accion = p[2] if len(p) > 2 else None
    lst = obtener(c, "listas", ident, True)
    if accion is None and method == "PATCH":
        cambios: Row = {}
        if "nombre" in (body or {}):
            nombre = _nombre(body)
            if uno(
                c,
                "SELECT 1 AS si FROM listas WHERE lower(nombre)=lower(%s) AND id<>%s "
                "AND archivada_en IS NULL",
                (nombre, ident),
                False,
            ):
                raise HTTPException(409, "Ya existe una lista activa con ese nombre")
            cambios["nombre"] = nombre
        if "descripcion" in (body or {}):
            cambios["descripcion"] = str(body.get("descripcion") or "")[:500]
        actualizar(c, "listas", ident, cambios)
        return {"ok": True}
    if method != "POST":
        raise HTTPException(404, "Ruta no encontrada")
    if accion == "archivar":
        activa = uno(
            c,
            "SELECT nombre FROM campanas WHERE lista_id=%s AND estado='activa' LIMIT 1",
            (ident,),
            False,
        )
        if activa:
            raise HTTPException(
                409, f"La campaña «{activa['nombre']}» usa esta lista: páusela antes de archivar"
            )
        actualizar(c, "listas", ident, {"archivada_en": datos.ahora()})
        auditar(c, "archivar_lista", "listas", ident)
        return {"ok": True}
    if accion == "restaurar":
        if uno(
            c,
            "SELECT 1 AS si FROM listas WHERE lower(nombre)=lower(%s) AND id<>%s "
            "AND archivada_en IS NULL",
            (lst["nombre"], ident),
            False,
        ):
            raise HTTPException(409, "Ya existe una lista activa con ese nombre; renómbrela antes")
        actualizar(c, "listas", ident, {"archivada_en": None})
        return {"ok": True}
    if lst["archivada_en"]:
        raise HTTPException(409, "La lista está archivada")
    if accion in ("agregar", "quitar") and lst["tipo"] == "vinculada":
        raise HTTPException(
            409,
            "Esta lista sigue al segmento: sus miembros cambian con sus reglas. Para editarlos "
            "a mano, cree una lista estática desde el segmento.",
        )
    if accion == "agregar":
        n = agregar_ids(c, ident, _ids((body or {}).get("contactoIds")))
        auditar(c, "agregar_a_lista", "listas", ident)
        return {"ok": True, "agregados": n}
    if accion == "quitar":
        ids = _ids((body or {}).get("contactoIds"))
        r = c.execute(
            "UPDATE lista_miembros SET activo=false, retirado_en=now(), motivo_retiro='manual' "
            "WHERE lista_id=%s AND contacto_id=ANY(%s::uuid[]) AND activo RETURNING contacto_id",
            (ident, ids),
        ).fetchall()
        auditar(c, "quitar_de_lista", "listas", ident)
        return {"ok": True, "quitados": len(r)}
    raise HTTPException(404, "Ruta no encontrada")
