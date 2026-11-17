from uuid import uuid4
import pytest
import psycopg
from psycopg import sql
from app.db import transaccion,uno
from test_base import client
from test_datos import auth


def test_rol_no_puede_modificar_auditoria(auth):
    role='nexus_test_role_'+uuid4().hex
    with transaccion() as c:
        c.execute(sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE ON SCHEMA nexus TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA nexus TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA nexus TO {}').format(sql.Identifier(role)))
        c.execute(sql.SQL('REVOKE UPDATE,DELETE,TRUNCATE ON registro_auditoria FROM {}').format(sql.Identifier(role)))
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with transaccion() as c:
                c.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
                c.execute("SELECT set_config('nexus.anonimizando','on',true)")
                c.execute("UPDATE registro_auditoria SET actor='alterado'")
    finally:
        with transaccion() as c:
            c.execute(sql.SQL('DROP OWNED BY {}').format(sql.Identifier(role)))
            c.execute(sql.SQL('DROP ROLE {}').format(sql.Identifier(role)))


def test_reset_conserva_auditoria(auth):
    with transaccion() as c:
        before=uno(c,'SELECT count(*) AS n FROM registro_auditoria')['n']
        c.execute("UPDATE tareas SET estado='cancelada',terminada_en=now() WHERE estado='en_proceso'")
    res=auth.post('/api/dev/reset',json={})
    assert res.status_code==200,res.text
    assert len(auth.get('/api/empresas').json())==1
    with transaccion() as c:
        assert uno(c,'SELECT count(*) AS n FROM registro_auditoria')['n']>before
    assert auth.get('/api/auth/session').status_code==200
