from pathlib import Path
from alembic import op
revision = '0001'
down_revision = None

def upgrade():
    original = (Path(__file__).resolve().parents[2] / 'sql/nexus_publicity_manager_schema.sql').read_text()
    script = '\n'.join(line for line in original.splitlines() if line.strip() not in ('BEGIN;', 'COMMIT;'))
    op.get_bind().connection.driver_connection.execute(script)

def downgrade():
    raise RuntimeError('La migración original contiene auditoría inmutable: restaure un respaldo; no se destruye automáticamente.')
