"""Ejecuta los mismos 37 casos; omite únicamente la directiva propia de psql."""
import os
from pathlib import Path
import psycopg
sql = '\n'.join(x for x in Path('sql/nexus_publicity_manager_pruebas.sql').read_text().splitlines() if not x.startswith('\\set '))
with psycopg.connect(os.environ['DATABASE_URL'], autocommit=True) as c:
    notices = []
    def notice(d):
        notices.append(d.message_primary)
        print(d.message_primary)
    c.add_notice_handler(notice)
    c.execute(sql)
    total = sum(x.startswith('OK') for x in notices)
    assert total == 37, total
    print('37/37 casos originales ejecutados correctamente')
