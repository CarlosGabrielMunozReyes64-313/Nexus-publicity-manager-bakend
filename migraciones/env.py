from dotenv import load_dotenv
load_dotenv()
import os
from alembic import context
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
url = os.environ['DATABASE_URL'].replace('postgresql://', 'postgresql+psycopg://', 1)
with create_engine(url, poolclass=NullPool).connect() as connection:
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
