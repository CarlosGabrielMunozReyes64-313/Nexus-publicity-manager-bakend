"""Aplica las migraciones de la base de bajas (UNSUBSCRIBE_DATABASE_URL).

Pensado para el «Pre-deploy command» de Railway después de `alembic upgrade head`:
- Sin UNSUBSCRIBE_DATABASE_URL: avisa y termina bien (las bajas funcionan sin ella).
- Si la base secundaria no responde: avisa y termina bien; el despliegue no se bloquea y
  las bajas quedan pendientes de copia hasta que se migre y responda.
- Con --estricto: cualquier fallo termina con error (para aplicarla a mano y comprobar).

Uso:  python scripts/migrar_bajas.py [--estricto]
"""

import os
import subprocess
import sys

from dotenv import load_dotenv

load_dotenv()
estricto = "--estricto" in sys.argv
url = os.environ.get("UNSUBSCRIBE_DATABASE_URL", "").strip()
if not url:
    print("migrar_bajas: UNSUBSCRIBE_DATABASE_URL vacía; se omite la base de bajas.")
    sys.exit(1 if estricto else 0)
r = subprocess.run(
    [sys.executable, "-m", "alembic", "-c", "alembic_bajas.ini", "upgrade", "head"],
    capture_output=True,
    text=True,
)
if r.returncode == 0:
    print("migrar_bajas: base de bajas al día (alembic_bajas.ini → head).")
    sys.exit(0)
# Sin credenciales en la salida: solo la última línea del error de Alembic/psycopg.
lineas = [x.strip() for x in r.stderr.splitlines() if x.strip()]
ultima = next((x for x in reversed(lineas) if "Error:" in x or "Error)" in x), None) or (
    lineas[-1] if lineas else "error desconocido"
)
ultima = ultima.split("(Background on this error")[0][:200]
print(f"migrar_bajas: no se pudo migrar la base de bajas: {ultima}")
print("migrar_bajas: las bajas siguen funcionando; quedarán pendientes de copia.")
sys.exit(1 if estricto else 0)
