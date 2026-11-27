"""Bitácora del trabajador para la pantalla Terminal (migración 0007).

Cada línea la arma este backend con textos fijos y nombres de empresas o búsquedas. Nunca
lleva correos, teléfonos, claves, cuerpos de correo ni contenido de páginas web.
"""

from datetime import datetime
from typing import Any

import structlog

from app.db import Conn, Row, todos, transaccion, uno

log = structlog.get_logger()

NIVELES = ("info", "exito", "aviso", "error")
# Tareas que corren cada minuto: solo se anotan si fallan, para que la terminal no se llene
# de ruido.
RUTINARIAS = {
    "programar_envios",
    "sincronizar_gmail",
    "liberar_tareas_colgadas",
    "sincronizar_bajas",
    "conciliar_envios",
}
NOMBRES = {
    "descubrir_empresas": "Búsqueda de empresas",
    "enriquecer_empresa": "Investigar empresa",
    "clasificar_respuesta": "Clasificar una respuesta",
    "proponer_segmentos": "Proponer segmentos",
    "proponer_campana": "Proponer secuencia de correo",
    "revisar_campana": "Revisar secuencia de correo",
    "reescribir_correo": "Mejorar un correo con IA",
    "informe_semanal": "Informe semanal",
    "verificar_correo": "Verificar un correo",
    "enviar_mensaje": "Enviar un correo programado",
    "programar_envios": "Programar envíos",
    "conciliar_envios": "Conciliar envíos inciertos con Gmail",
    "sincronizar_bajas": "Copiar bajas a la base secundaria",
    "sincronizar_gmail": "Revisar respuestas en Gmail",
    "liberar_tareas_colgadas": "Liberar tareas colgadas",
    "sincronizar_ga4": "Sincronizar Google Analytics",
    "sincronizar_search_console": "Sincronizar Search Console",
    "buscar_duplicados": "Buscar empresas duplicadas",
    "calcular_puntajes": "Calcular puntajes",
    "calcular_puntaje": "Calcular el puntaje de una empresa",
}
RETENCION = "3 days"


def plural(n: int, uno: str, varios: str) -> str:
    return f"{n} {uno if n == 1 else varios}"


def nombre_empresa(texto: str) -> str:
    """Sin el punto final («S.A.S.»), para no cerrar la frase con dos puntos seguidos."""
    return corto(texto, 80).rstrip(".")


def corto(texto: str, n: int = 90) -> str:
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= n else texto[: n - 1].rstrip() + "…"


def evento(c: Conn, mensaje: str, nivel: str = "info", tarea_id: Any = None) -> None:
    c.execute(
        "INSERT INTO eventos_trabajador(nivel,tarea_id,mensaje) VALUES(%s,%s,%s)",
        (nivel if nivel in NIVELES else "info", tarea_id, corto(mensaje, 500)),
    )


def anotar(mensaje: str, nivel: str = "info", tarea_id: Any = None) -> None:
    """Anota en su propia transacción. Un fallo de la bitácora nunca detiene una tarea."""
    try:
        with transaccion("proceso:trabajador") as c:
            evento(c, mensaje, nivel, tarea_id)
    except Exception as exc:  # pragma: no cover - solo si la base de datos falla
        log.warning("bitacora_fallida", clase=type(exc).__name__)


def latido(c: Conn, trabajador: str) -> None:
    c.execute(
        "INSERT INTO latidos_trabajador(trabajador,visto_en) VALUES(%s,now()) "
        "ON CONFLICT (trabajador) DO UPDATE SET visto_en=now()",
        (trabajador,),
    )


def limpiar(c: Conn) -> None:
    c.execute(f"DELETE FROM eventos_trabajador WHERE creado_en < now() - interval '{RETENCION}'")
    c.execute("DELETE FROM latidos_trabajador WHERE visto_en < now() - interval '1 day'")


def describir(c: Conn, t: Row) -> str:
    """Qué hace una tarea, en palabras del administrador y sin datos personales."""
    datos = t.get("datos") or {}
    tipo = str(t["tipo"])
    nombre: str = NOMBRES.get(tipo, tipo.replace("_", " ").capitalize())
    try:
        if t["tipo"] == "descubrir_empresas" and datos.get("busquedaId"):
            b = uno(
                c,
                "SELECT consulta,municipios FROM busquedas_descubrimiento WHERE id=%s",
                (datos["busquedaId"],),
                False,
            )
            if b:
                consulta = str(b["consulta"]).split(":", 1)[-1]
                lugar = f" en {', '.join(b['municipios'][:3])}" if b["municipios"] else ""
                mas = f" y {len(b['municipios']) - 3} más" if len(b["municipios"]) > 3 else ""
                meta = " (meta diaria)" if datos.get("meta") else ""
                return f"{nombre}{meta}: «{corto(consulta, 70)}»{lugar}{mas}"
        if t["tipo"] in ("enriquecer_empresa", "calcular_puntaje") and datos.get("empresaId"):
            e = uno(
                c, "SELECT razon_social FROM empresas WHERE id=%s", (datos["empresaId"],), False
            )
            if e:
                return f"{nombre}: {corto(e['razon_social'], 70)}"
    except Exception:  # pragma: no cover - una descripción nunca debe tumbar la tarea
        return nombre
    return nombre


def describir_aparte(t: Row) -> str:
    try:
        with transaccion("proceso:trabajador") as c:
            return describir(c, t)
    except Exception:  # pragma: no cover
        return NOMBRES.get(str(t["tipo"]), str(t["tipo"]))


def hora(momento: datetime) -> str:
    from app.dominio.reglas import BOGOTA

    return momento.astimezone(BOGOTA).strftime("%H:%M")


def terminal(c: Conn, desde: int | None) -> dict[str, Any]:
    """GET /terminal: estado del trabajador, cola y líneas nuevas de la bitácora."""
    latido_row = uno(
        c,
        "SELECT visto_en, extract(epoch FROM now()-visto_en)::int AS hace "
        "FROM latidos_trabajador ORDER BY visto_en DESC LIMIT 1",
        (),
        False,
    )
    # Una tarea larga (hasta 10 minutos) no deja latir al trabajador: si tiene una tarea
    # tomada hace poco, sigue vivo.
    trabajando = uno(
        c,
        "SELECT 1 AS si FROM tareas WHERE estado='en_proceso' "
        "AND tomada_en > now() - interval '11 minutes' LIMIT 1",
        (),
        False,
    )
    cola = uno(
        c,
        """SELECT
             count(*) FILTER (WHERE estado='en_proceso') AS en_proceso,
             count(*) FILTER (WHERE estado='pendiente' AND ejecutar_desde<=now()) AS en_cola,
             count(*) FILTER (WHERE estado='pendiente' AND ejecutar_desde>now()) AS esperando,
             count(*) FILTER (WHERE estado='fallida' AND terminada_en>now()-interval '24 hours')
               AS fallidas_24h
           FROM tareas""",
    )
    ahora_mismo = []
    for t in todos(
        c,
        """SELECT id,tipo,datos,estado,intentos,max_intentos,tomada_en,ejecutar_desde,ultimo_error
           FROM tareas
           WHERE estado='en_proceso'
              OR (estado='pendiente' AND tipo<>ALL(%s))
           ORDER BY CASE WHEN estado='en_proceso' THEN 0
                         WHEN ultimo_error IS NOT NULL THEN 1 ELSE 2 END,
                    ejecutar_desde
           LIMIT 20""",
        (sorted(RUTINARIAS),),
    ):
        ahora_mismo.append(
            {
                "tarea_id": t["id"],
                "descripcion": describir(c, t),
                "estado": (
                    "en_proceso"
                    if t["estado"] == "en_proceso"
                    else "esperando"
                    if t["ultimo_error"]
                    else "en_cola"
                ),
                "desde": t["tomada_en"] if t["estado"] == "en_proceso" else t["ejecutar_desde"],
                "intento": t["intentos"],
                "max_intentos": t["max_intentos"],
            }
        )
    filtro = "WHERE id > %s" if desde else ""
    eventos = todos(
        c,
        f"SELECT id,creado_en AS fecha,nivel,mensaje FROM eventos_trabajador {filtro} "
        "ORDER BY id DESC LIMIT 300",
        (desde,) if desde else (),
    )
    return {
        "trabajador": {
            "activo": bool(latido_row) and (latido_row["hace"] <= 90 or bool(trabajando)),
            **({"visto_en": latido_row["visto_en"]} if latido_row else {}),
        },
        "cola": cola,
        "ahora_mismo": ahora_mismo,
        "eventos": list(reversed(eventos)),
    }
