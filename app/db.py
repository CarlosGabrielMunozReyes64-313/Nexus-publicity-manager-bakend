from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException
from psycopg import Connection, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

Row = dict[str, Any]
Conn = Connection[Row]
pool: ConnectionPool[Conn] | None = None


def abrir(url: str) -> None:
    global pool
    pool = ConnectionPool(
        url,
        open=True,
        min_size=1,
        max_size=8,
        kwargs={"row_factory": dict_row, "options": "-c search_path=nexus,public"},
    )
    pool.wait(timeout=30)


def cerrar() -> None:
    if pool:
        pool.close()


@contextmanager
def transaccion(
    actor: str = "sistema", admin: str = "", accion: str = "", evento: str = ""
) -> Iterator[Conn]:
    assert pool is not None
    with pool.connection() as c, c.transaction():
        for k, v in {
            "actor": actor,
            "administrador_id": admin,
            "accion": accion,
            "evento": evento,
        }.items():
            c.execute("SELECT set_config(%s, %s, true)", ("nexus." + k, v))
        yield c


def uno(c: Conn, query: str, params: tuple[Any, ...] = (), requerido: bool = True) -> Row:
    r = c.execute(query, params).fetchone()
    if r is None and requerido:
        raise HTTPException(404, "Registro no encontrado")
    return r or {}


def todos(c: Conn, query: str, params: tuple[Any, ...] = ()) -> list[Row]:
    return list(c.execute(query, params).fetchall())


def obtener(c: Conn, tabla: str, ident: str, lock: bool = False) -> Row:
    q = sql.SQL("SELECT * FROM {} WHERE id=%s" + (" FOR UPDATE" if lock else "")).format(
        sql.Identifier(tabla)
    )
    r = c.execute(q, (ident,)).fetchone()
    if not r:
        raise HTTPException(404, "Registro no encontrado")
    return r


def insertar(c: Conn, tabla: str, valores: Row) -> Row:
    q = sql.SQL("INSERT INTO {} ({}) VALUES ({}) RETURNING *").format(
        sql.Identifier(tabla),
        sql.SQL(",").join(map(sql.Identifier, valores)),
        sql.SQL(",").join(sql.Placeholder() for _ in valores),
    )
    r = c.execute(q, tuple(valores.values())).fetchone()
    assert r is not None
    return r


def actualizar(c: Conn, tabla: str, ident: str, valores: Row) -> Row:
    if not valores:
        return obtener(c, tabla, ident)
    q = sql.SQL("UPDATE {} SET {} WHERE id=%s RETURNING *").format(
        sql.Identifier(tabla),
        sql.SQL(",").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in valores),
    )
    r = c.execute(q, (*valores.values(), ident)).fetchone()
    if not r:
        raise HTTPException(404, "Registro no encontrado")
    return r


def encolar(c: Conn, tipo: str, datos: Row, clave: str | None = None, prioridad: int = 5) -> None:
    c.execute(
        "INSERT INTO tareas(tipo,datos,clave_unica,prioridad,max_intentos) VALUES(%s,%s,%s,%s,4) ON CONFLICT(clave_unica) DO NOTHING",
        (tipo, Jsonb(datos), clave, prioridad),
    )


def auditar(c: Conn, accion: str, entidad: str, ident: str, costo: float | None = None) -> None:
    c.execute(
        "INSERT INTO registro_auditoria(actor,administrador_id,accion,entidad,entidad_id,costo_usd) VALUES(ajuste('nexus.actor'),ajuste('nexus.administrador_id')::uuid,%s,%s,%s,%s)",
        (accion, entidad, ident, costo),
    )


UNIQUE = {
    "contactos_correo_uq": "Ya existe un contacto con ese correo",
    "empresas_dominio_uq": "Ya existe una empresa con ese dominio",
    "empresas_nit_uq": "Ya existe una empresa con ese NIT",
}
