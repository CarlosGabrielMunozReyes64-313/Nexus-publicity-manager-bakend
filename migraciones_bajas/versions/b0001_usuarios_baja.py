"""Tabla de usuarios dados de baja en la base secundaria.

Solo datos mínimos: correo normalizado (único), fecha de la baja, origen y la referencia a
la supresión de la base principal. No guarda nombres, empresas ni contenido de correos.
La copia la escribe el trabajador desde `nexus.sincronizacion_bajas` (idempotente: ON
CONFLICT por correo), así que repetir la sincronización nunca crea duplicados.
"""
from alembic import op

revision = "b0001"
down_revision = None


def upgrade() -> None:
    op.execute(
        """
        CREATE SCHEMA IF NOT EXISTS bajas;
        CREATE TABLE bajas.usuarios_baja (
          id                    bigserial PRIMARY KEY,
          correo                text NOT NULL UNIQUE
                                CHECK (correo = lower(correo) AND correo ~ '^[^@\\s]+@[^@\\s]+$'),
          dado_de_baja_en       timestamptz NOT NULL,
          origen                text NOT NULL CHECK (char_length(origen) BETWEEN 1 AND 40),
          supresion_id          uuid,
          sincronizado_en       timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE bajas.usuarios_baja; DROP SCHEMA bajas;")
