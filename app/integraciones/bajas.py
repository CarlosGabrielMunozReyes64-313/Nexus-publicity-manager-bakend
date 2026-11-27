"""Copia de las bajas a la base secundaria (UNSUBSCRIBE_DATABASE_URL).

La baja surte efecto en la base principal en el mismo instante en que se confirma: la
supresión, la exclusión, la salida de listas, la detención de inscripciones y la
cancelación de envíos ocurren en esa transacción, y un disparador deja la copia pendiente
en `sincronizacion_bajas` (bandeja de salida durable). Este módulo, que corre en el
trabajador, copia después las pendientes:

- INSERT ... ON CONFLICT (correo) DO NOTHING: repetir nunca duplica.
- Si la base secundaria está apagada o rechaza la conexión, las filas siguen pendientes
  con su intento, el tipo de error y el próximo intento (espera creciente hasta 1 hora).
- Si la copia se guardó pero la base principal no alcanzó a marcarla, el siguiente intento
  la encuentra y no la duplica.
"""

from typing import Any

import psycopg

from app.config import config
from app.db import Conn, todos, transaccion, uno

LOTE = 200
ESPERA_MAX_S = 3600


def configurada() -> bool:
    return bool(config().unsubscribe_database_url.get_secret_value().strip())


def _url() -> str:
    url = config().unsubscribe_database_url.get_secret_value().strip()
    for prefijo in ("postgresql+psycopg://", "postgres://"):
        if url.startswith(prefijo):
            return "postgresql://" + url[len(prefijo) :]
    return url


def sincronizar(lote: int = LOTE) -> dict[str, Any]:
    """Copia hasta `lote` bajas pendientes. Devuelve {copiadas, pendientes, error}."""
    if not configurada():
        return {"copiadas": 0, "error": "UNSUBSCRIBE_DATABASE_URL sin configurar"}
    with transaccion("proceso:sincronizador_bajas", accion="sincronizar_bajas") as c:
        filas = todos(
            c,
            "SELECT * FROM sincronizacion_bajas WHERE estado='pendiente' "
            "AND proximo_intento<=now() ORDER BY id LIMIT %s FOR UPDATE SKIP LOCKED",
            (lote,),
        )
        if not filas:
            return {"copiadas": 0, "error": None}
        try:
            with psycopg.connect(_url(), connect_timeout=5) as s:
                with s.cursor() as cur:
                    for f in filas:
                        cur.execute(
                            "INSERT INTO bajas.usuarios_baja"
                            "(correo,dado_de_baja_en,origen,supresion_id) VALUES (%s,%s,%s,%s) "
                            "ON CONFLICT (correo) DO NOTHING",
                            (
                                str(f["correo"]).lower(),
                                f["dado_de_baja_en"],
                                f["origen"],
                                f["supresion_id"],
                            ),
                        )
                s.commit()
        except psycopg.Error as exc:
            motivo = _motivo(exc)
            c.execute(
                """UPDATE sincronizacion_bajas SET intentos=intentos+1, ultimo_error=%s,
                proximo_intento=now()+make_interval(secs=>least(%s, 30*power(2,least(intentos,7))))
                WHERE id=ANY(%s)""",
                (motivo, ESPERA_MAX_S, [f["id"] for f in filas]),
            )
            return {"copiadas": 0, "error": motivo}
        c.execute(
            "UPDATE sincronizacion_bajas SET estado='sincronizada', sincronizada_en=now(), "
            "ultimo_error=NULL, intentos=intentos+1 WHERE id=ANY(%s)",
            ([f["id"] for f in filas],),
        )
        return {"copiadas": len(filas), "error": None}


def _motivo(exc: Exception) -> str:
    """Motivo sin credenciales ni datos: clase del error y, si aplica, código SQLSTATE."""
    texto = str(exc).lower()
    if isinstance(exc, psycopg.OperationalError):
        if "timeout" in texto or "timed out" in texto:
            return "La base de bajas no respondió a tiempo (¿servicio apagado?)"
        if "could not translate host" in texto or "name or service" in texto:
            return "No se encontró el servidor de la base de bajas (revise la URL)"
        if "password authentication" in texto:
            return "La base de bajas rechazó el usuario o la contraseña"
        return "No fue posible conectar con la base de bajas (¿servicio apagado?)"
    if isinstance(exc, psycopg.errors.UndefinedTable):
        return (
            "Falta la tabla bajas.usuarios_baja: aplique alembic -c alembic_bajas.ini upgrade head"
        )
    codigo = getattr(exc, "sqlstate", None) or ""
    return f"Error de la base de bajas ({type(exc).__name__}{' ' + codigo if codigo else ''})"


def hay_pendientes(c: Conn) -> bool:
    return bool(
        uno(
            c,
            "SELECT 1 AS si FROM sincronizacion_bajas WHERE estado='pendiente' "
            "AND proximo_intento<=now() LIMIT 1",
            (),
            False,
        )
    )


def resumen(c: Conn) -> dict[str, Any]:
    r = uno(
        c,
        """SELECT count(*) FILTER (WHERE estado='pendiente') AS pendientes,
                  count(*) FILTER (WHERE estado='sincronizada') AS sincronizadas,
                  max(sincronizada_en) AS ultima_sincronizacion,
                  (SELECT ultimo_error FROM sincronizacion_bajas WHERE estado='pendiente'
                    AND ultimo_error IS NOT NULL ORDER BY id DESC LIMIT 1) AS ultimo_error,
                  min(proximo_intento) FILTER (WHERE estado='pendiente') AS proximo_intento
           FROM sincronizacion_bajas""",
    )
    return {"configurada": configurada(), **r}
