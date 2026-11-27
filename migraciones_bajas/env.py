"""Entorno de Alembic para la base de bajas. Nunca toca la base principal."""
from dotenv import load_dotenv

load_dotenv()
import os  # noqa: E402

from alembic import context  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

raw = os.environ.get("UNSUBSCRIBE_DATABASE_URL", "").strip()
if not raw:
    raise SystemExit("Defina UNSUBSCRIBE_DATABASE_URL con la base de bajas antes de migrarla")
if raw == os.environ.get("DATABASE_URL", "").strip():
    raise SystemExit("UNSUBSCRIBE_DATABASE_URL no puede ser la misma base que DATABASE_URL")
url = raw.replace("postgresql://", "postgresql+psycopg://", 1).replace(
    "postgres://", "postgresql+psycopg://", 1
)
with create_engine(url, poolclass=NullPool).connect() as connection:
    context.configure(
        connection=connection, transactional_ddl=True, version_table="alembic_version_bajas"
    )
    with context.begin_transaction():
        context.run_migrations()
