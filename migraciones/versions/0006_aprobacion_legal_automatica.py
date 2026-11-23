"""Política de cumplimiento: aprobación automática de la base legal (apagada por defecto).

Esquema: la clave de `configuracion` admite también 'cumplimiento' (se amplía la lista de
claves; ninguna regla existente se relaja). Datos: se crea la fila
{"aprobacionLegalAutomatica": false}. Encenderla es una decisión del administrador, que
queda como quien aprueba (`bases_legales.aprobada_por`) y en la auditoría.

La bajada borra la fila y devuelve la restricción a las tres claves originales. Las bases
legales aprobadas mientras estuvo encendida se conservan: son aprobaciones válidas.
"""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE nexus.configuracion DROP CONSTRAINT configuracion_clave_check;
        ALTER TABLE nexus.configuracion ADD CONSTRAINT configuracion_clave_check
          CHECK (clave IN ('marca', 'limites', 'cliente_ideal', 'cumplimiento'));
        INSERT INTO nexus.configuracion (clave, valor)
        VALUES ('cumplimiento', '{"aprobacionLegalAutomatica": false}'::jsonb)
        ON CONFLICT (clave) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM nexus.configuracion WHERE clave = 'cumplimiento';
        ALTER TABLE nexus.configuracion DROP CONSTRAINT configuracion_clave_check;
        ALTER TABLE nexus.configuracion ADD CONSTRAINT configuracion_clave_check
          CHECK (clave IN ('marca', 'limites', 'cliente_ideal'));
        """
    )
