"""Meta diaria de contactos en Descubrimiento y Terminal del trabajador.

Esquema:
- La clave de `configuracion` admite también 'descubrimiento' (se amplía la lista de claves;
  ninguna regla existente se relaja). Se crea la fila con la meta apagada.
- `eventos_trabajador`: líneas de lo que hace el trabajador en segundo plano, para la
  pantalla Terminal. Solo textos armados por el backend (sin correos, claves ni contenido de
  páginas). Se borran solas a los 3 días.
- `latidos_trabajador`: última señal de vida de cada trabajador.

La bajada borra las dos tablas y la fila, y devuelve la restricción a las cuatro claves de 0006.
"""

from alembic import op

revision = "0007"
down_revision = "0006"

META_INICIAL = (
    '{"activa": false, "metaDiaria": 20, "maxBusquedas": 40, '
    '"tipos": [], "municipios": [], "sectores": [], "tamano": ""}'
)


def upgrade() -> None:
    op.execute(
        f"""
        ALTER TABLE nexus.configuracion DROP CONSTRAINT configuracion_clave_check;
        ALTER TABLE nexus.configuracion ADD CONSTRAINT configuracion_clave_check
          CHECK (clave IN ('marca', 'limites', 'cliente_ideal', 'cumplimiento', 'descubrimiento'));
        INSERT INTO nexus.configuracion (clave, valor)
        VALUES ('descubrimiento', '{META_INICIAL}'::jsonb)
        ON CONFLICT (clave) DO NOTHING;

        CREATE TABLE nexus.eventos_trabajador (
          id         bigserial PRIMARY KEY,
          creado_en  timestamptz NOT NULL DEFAULT now(),
          nivel      text NOT NULL CHECK (nivel IN ('info', 'exito', 'aviso', 'error')),
          tarea_id   uuid REFERENCES nexus.tareas(id) ON DELETE SET NULL,
          mensaje    text NOT NULL CHECK (char_length(mensaje) BETWEEN 1 AND 500)
        );
        CREATE INDEX eventos_trabajador_creado_idx ON nexus.eventos_trabajador (creado_en);

        CREATE TABLE nexus.latidos_trabajador (
          trabajador  text PRIMARY KEY CHECK (char_length(trabajador) BETWEEN 1 AND 100),
          visto_en    timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE nexus.latidos_trabajador;
        DROP TABLE nexus.eventos_trabajador;
        DELETE FROM nexus.configuracion WHERE clave = 'descubrimiento';
        ALTER TABLE nexus.configuracion DROP CONSTRAINT configuracion_clave_check;
        ALTER TABLE nexus.configuracion ADD CONSTRAINT configuracion_clave_check
          CHECK (clave IN ('marca', 'limites', 'cliente_ideal', 'cumplimiento'));
        """
    )
