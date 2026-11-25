"""La pausa automática por queja de spam avisa una vez a los administradores."""

from uuid import uuid4

from test_base import client  # noqa: F401  (abre la base de la app)
from test_correcciones import admin_id, aislar_campanas, crear_campana

from app.config import config
from app.db import todos, transaccion, uno


def avisos_pausa():
    with transaccion() as c:
        return todos(
            c,
            "SELECT n.* FROM notificaciones n JOIN administradores a ON a.id=n.destinatario_id "
            "WHERE a.correo=%s AND n.clave LIKE 'pausa_correo:queja:%%'",
            (config().admin_correo,),
        )


def test_pausa_por_queja_avisa_una_vez(client):  # noqa: F811
    """También la reciben los usuarios aprobados: pueden resolverla."""
    from app.motor.alertas import revisar

    with transaccion("admin") as c:
        admin = admin_id(c)
        # Usuario aprobado (rol «usuario»): también recibe los avisos operativos.
        usuario = uno(
            c,
            "INSERT INTO administradores(correo,nombre,rol) VALUES(%s,'Usuaria','usuario') RETURNING id",
            (f"usuaria-{uuid4().hex[:8]}@gmail.example",),
        )
        aislar_campanas(c)
        cam, ids, _, _ = crear_campana(c, admin, "Queja " + uuid4().hex[:6], contactos=1)
        assert uno(c, "SELECT estado FROM campanas WHERE id=%s", (cam["id"],))["estado"] == "activa"
        evento = uno(
            c,
            "INSERT INTO eventos_correo(contacto_id,tipo) VALUES(%s,'queja') RETURNING id",
            (ids[0],),
        )
    try:
        with transaccion("proceso:motor") as c:
            revisar(c)
        with transaccion("proceso:motor") as c:
            revisar(c)  # siguiente revisión periódica: nada activo, sin aviso nuevo
        avisos = avisos_pausa()
        assert len(avisos) == 1 and avisos[0]["repeticiones"] == 1
        with transaccion() as c:
            recibio = uno(
                c,
                "SELECT count(*) AS n FROM notificaciones "
                "WHERE destinatario_id=%s AND clave LIKE 'pausa_correo:queja:%%'",
                (usuario["id"],),
            )["n"]
        assert recibio == 1
        assert avisos[0]["tipo"] == "pausa_envios" and avisos[0]["enlace"] == "/campanas"
        assert "queja" in avisos[0]["descripcion"] and "Campañas pausadas: 1" in avisos[0]["descripcion"]
        with transaccion() as c:
            assert uno(c, "SELECT estado FROM campanas WHERE id=%s", (cam["id"],))["estado"] == "pausada"
            # Se reanuda y vuelve a pausarse el mismo día: se agrupa en el mismo aviso.
            c.execute("UPDATE campanas SET estado='activa' WHERE id=%s", (cam["id"],))
        with transaccion("proceso:motor") as c:
            revisar(c)
        avisos = avisos_pausa()
        assert len(avisos) == 1 and avisos[0]["repeticiones"] == 2
    finally:
        with transaccion() as c:
            c.execute("DELETE FROM eventos_correo WHERE id=%s", (evento["id"],))
            aislar_campanas(c)
