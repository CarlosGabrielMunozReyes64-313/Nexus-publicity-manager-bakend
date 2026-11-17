"""Ejecutar con el dueño de migraciones; jamás con el rol de aplicación."""
import os
from dotenv import load_dotenv
load_dotenv()
import psycopg
from psycopg import sql
with psycopg.connect(os.environ['DATABASE_URL']) as c:
    if not c.execute("SELECT 1 FROM pg_roles WHERE rolname='nexus_app'").fetchone():
        c.execute('CREATE ROLE nexus_app LOGIN')
    c.execute(sql.SQL('ALTER ROLE nexus_app PASSWORD {}').format(sql.Literal(os.environ['DB_APP_PASSWORD'])))
    c.execute('GRANT USAGE ON SCHEMA nexus TO nexus_app')
    c.execute('GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA nexus TO nexus_app')
    c.execute('GRANT USAGE,SELECT ON ALL SEQUENCES IN SCHEMA nexus TO nexus_app')
    c.execute('REVOKE UPDATE,DELETE,TRUNCATE ON nexus.registro_auditoria FROM nexus_app')
    c.execute('GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA nexus TO nexus_app')
    c.execute('ALTER ROLE nexus_app SET search_path=nexus,public')
