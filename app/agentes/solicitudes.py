"""Pedidos «a pedido» a los agentes que proponen (Segmentador y Estratega-redactor).

Los agentes solo escriben propuestas; aquí únicamente se validan los requisitos para
dar un mensaje claro antes de gastar presupuesto de IA, y se encola la tarea.
"""

from typing import Any
from uuid import uuid4

from fastapi import HTTPException

from app.api import lecturas
from app.config import config
from app.db import Conn, auditar, encolar, todos, uno

TIPOS = ("proponer_segmentos", "proponer_campana", "revisar_campana")
EN_CURSO = ("pendiente", "en_proceso")


def proveedor_configurado() -> None:
    """409 claro si el proveedor activo no tiene clave o precios en el servidor."""
    from app.agentes.ejecutor import precios_de

    cfg = config()
    if cfg.agente_proveedor == "gemini":
        listo = bool(cfg.gemini_api_key.get_secret_value()) and bool(
            precios_de(cfg.gemini_modelo, cfg.gemini_precios_json)
        )
        faltan = "GEMINI_API_KEY y GEMINI_PRECIOS_JSON"
    else:
        listo = bool(cfg.anthropic_api_key.get_secret_value()) and bool(
            precios_de(cfg.claude_modelo_mayor, cfg.claude_precios_json)
        )
        faltan = "ANTHROPIC_API_KEY y CLAUDE_PRECIOS_JSON"
    if not listo:
        raise HTTPException(
            409,
            f"Configure el proveedor de IA en el servidor ({faltan}) antes de pedir propuestas",
        )


def contactos_elegibles(c: Conn, reglas: list[dict[str, str]]) -> int:
    ids = lecturas.ids_segmento(c, reglas)
    if not ids:
        return 0
    return int(
        uno(
            c,
            "SELECT count(*) AS n FROM unnest(%s::uuid[]) AS x(id) "
            "WHERE motivo_no_elegible(x.id) IS NULL",
            (ids,),
        )["n"]
    )


def pedir_segmentos(c: Conn) -> dict[str, Any]:
    proveedor_configurado()
    if not uno(c, "SELECT count(*) AS n FROM empresas WHERE fusionada_en_id IS NULL")["n"]:
        raise HTTPException(
            409,
            "Primero incorpore empresas (Descubrimiento o importación) para que el "
            "Segmentador tenga datos con qué proponer",
        )
    if uno(
        c,
        "SELECT count(*) AS n FROM tareas WHERE tipo='proponer_segmentos' AND estado=ANY(%s)",
        (list(EN_CURSO),),
    )["n"]:
        raise HTTPException(409, "Ya hay un pedido de segmentos en curso; espere a que termine")
    clave = f"proponer_segmentos:{uuid4()}"
    encolar(c, "proponer_segmentos", {}, clave, 6)
    tarea = uno(c, "SELECT id FROM tareas WHERE clave_unica=%s", (clave,))
    auditar(c, "pedir_propuestas_segmentos", "tareas", str(tarea["id"]))
    return {"tareaId": str(tarea["id"])}


def pedir_campana(c: Conn, segmento_id: str) -> dict[str, Any]:
    """Ruta anterior (se conserva): pide una ruta de 3 correos con el objetivo por defecto."""
    from app.api.rutas_correo import OBJETIVO_POR_DEFECTO, encolar_ruta

    tarea = encolar_ruta(
        c, {"segmentoId": segmento_id, "objetivo": OBJETIVO_POR_DEFECTO, "numeroCorreos": 3}
    )
    return {"tareaId": tarea}


def lista(c: Conn) -> list[dict[str, Any]]:
    """Últimos pedidos con su estado y, si fallaron, el motivo seguro registrado."""
    filas = todos(
        c,
        """SELECT t.id,t.tipo,t.estado,t.intentos,t.max_intentos,t.ultimo_error,t.creada_en,
                  t.terminada_en,t.ejecutar_desde,t.datos->>'segmentoId' AS segmento_id,
                  t.datos->>'propuestaId' AS propuesta_id,s.nombre AS segmento
           FROM tareas t
           LEFT JOIN segmentos s ON s.id::text = t.datos->>'segmentoId'
           WHERE t.tipo = ANY(%s)
           ORDER BY t.creada_en DESC
           LIMIT 10""",
        (list(TIPOS),),
    )
    return [
        {
            "id": f["id"],
            "tipo": f["tipo"],
            "estado": f["estado"],
            "intentos": f["intentos"],
            "max_intentos": f["max_intentos"],
            # Solo se muestra el último error si la tarea falló o espera reintento.
            "error": f["ultimo_error"] if f["estado"] in ("fallida", "pendiente") else None,
            "creada_en": f["creada_en"],
            "terminada_en": f["terminada_en"],
            "proximo_intento": f["ejecutar_desde"]
            if f["estado"] == "pendiente" and f["intentos"]
            else None,
            "segmento_id": f["segmento_id"],
            "segmento": f["segmento"],
            "propuesta_id": f["propuesta_id"],
        }
        for f in filas
    ]
