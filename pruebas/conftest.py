"""Every pytest session uses a new PostgreSQL database; never clears user tables."""
import os
import subprocess
import sys
from uuid import uuid4

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
import pytest


@pytest.fixture(scope='session',autouse=True)
def base_aislada():
    base=os.environ.get('TEST_ADMIN_DATABASE_URL') or os.environ.get('DATABASE_URL')
    if not base:
        pytest.fail('Configure TEST_ADMIN_DATABASE_URL para PostgreSQL de pruebas')
    params=conninfo_to_dict(base)
    params['dbname']='postgres'
    admin=make_conninfo(**params)
    name='nexus_test_'+uuid4().hex
    with psycopg.connect(admin,autocommit=True) as c:
        c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
    params['dbname']=name
    # SQLAlchemy URL is also required by Alembic; preserve the provided authority.
    from sqlalchemy.engine import URL
    url=URL.create('postgresql',username=params.get('user'),password=params.get('password'),host=params.get('host'),port=int(params.get('port','5432')),database=name).render_as_string(hide_password=False)
    original=os.environ.get('DATABASE_URL')
    os.environ['DATABASE_URL']=url
    try:
        subprocess.run([sys.executable,'-m','alembic','upgrade','head'],check=True)
        yield
    finally:
        from app import db
        db.cerrar()
        with psycopg.connect(admin,autocommit=True) as c:
            c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
        if original is None:
            os.environ.pop('DATABASE_URL',None)
        else:
            os.environ['DATABASE_URL']=original
