"""Crea .env con claves aleatorias. No sobrescribe archivos existentes."""
import secrets
from pathlib import Path
import base64
import os
text = Path('.env.example').read_text()
values = {'CLAVE_CIFRADO': base64.urlsafe_b64encode(os.urandom(32)).decode(), 'SESION_SECRETO': secrets.token_urlsafe(48),
          'DB_OWNER_PASSWORD': secrets.token_urlsafe(24), 'DB_APP_PASSWORD': secrets.token_urlsafe(24),
          'FORMULARIO_WEBHOOK_SECRETO': secrets.token_urlsafe(32)}
for key, value in values.items():
    text = text.replace(key + '=\n', key + '=' + value + '\n')
text = text.replace('nexus_app:CAMBIAR@localhost', 'nexus_app:' + values['DB_APP_PASSWORD'] + '@localhost')
with Path('.env').open('x') as f:
    f.write(text)
Path('.env').chmod(0o600)
print('.env creado; revise ADMIN_CORREO y las integraciones antes de producción.')
