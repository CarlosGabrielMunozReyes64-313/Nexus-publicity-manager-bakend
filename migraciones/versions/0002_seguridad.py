from alembic import op
revision = '0002'
down_revision = '0001'

def upgrade():
    op.execute('''CREATE TABLE nexus.oauth_estados (
      hash bytea PRIMARY KEY, nonce text NOT NULL, verifier text NOT NULL,
      finalidad text NOT NULL CHECK(finalidad IN ('login','gmail')),
      administrador_id uuid REFERENCES nexus.administradores(id),
      expira_en timestamptz NOT NULL DEFAULT now()+interval '10 minutes')''')
    op.execute('''ALTER FUNCTION nexus.anonimizar_contacto(uuid,text) SECURITY DEFINER''')
    op.execute('''ALTER FUNCTION nexus.anonimizar_contacto(uuid,text) SET search_path = nexus, pg_temp''')
    op.execute('''REVOKE EXECUTE ON FUNCTION nexus.anonimizar_contacto(uuid,text) FROM PUBLIC''')

def downgrade():
    op.execute('DROP TABLE nexus.oauth_estados')
    op.execute('ALTER FUNCTION nexus.anonimizar_contacto(uuid,text) SECURITY INVOKER')
    op.execute('ALTER FUNCTION nexus.anonimizar_contacto(uuid,text) RESET search_path')
