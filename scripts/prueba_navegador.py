import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
import httpx
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from dotenv import load_dotenv
load_dotenv()
base=make_url(os.environ['DATABASE_URL'])
name='nexus_browser_'+uuid4().hex
admin=base.set(database='postgres').render_as_string(hide_password=False)
with psycopg.connect(admin,autocommit=True) as c:
    c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
env=os.environ.copy()
env['DATABASE_URL']=base.set(database=name).render_as_string(hide_password=False)
env['APP_URL']='http://localhost:8000'
env['APP_ENV']='development'
env['AUTH_DEV_BYPASS']='true'
server=None
try:
    subprocess.run([sys.executable,'-m','alembic','upgrade','head'],env=env,check=True)
    subprocess.run([sys.executable,'scripts/demo.py'],env=env,check=True)
    log=Path('pruebas/salidas/navegador_api.log')
    log.parent.mkdir(parents=True,exist_ok=True)
    with log.open('w') as f:
        server=subprocess.Popen([sys.executable,'-m','uvicorn','app.main:crear_app','--factory','--host','127.0.0.1','--port','8000','--no-access-log'],env=env,stdout=f,stderr=f)
        for _ in range(100):
            try:
                if httpx.get(env['APP_URL']+'/health',trust_env=False).status_code==200:break
            except httpx.HTTPError: pass
            time.sleep(.1)
        subprocess.run(['node','scripts/prueba_navegador.cjs'],env=env,check=True)
finally:
    if server:
        server.terminate();server.wait(timeout=15)
    with psycopg.connect(admin,autocommit=True) as c:
        c.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
