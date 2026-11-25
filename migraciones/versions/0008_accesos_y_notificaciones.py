"""Solicitudes de acceso con Google, roles y centro de notificaciones.

Esquema:
- `administradores.rol`: 'administrador' o 'usuario'. Todas las cuentas que ya existían
  quedan como 'administrador' (conservan su acceso y su estado activo/inactivo). Las altas
  nuevas usan 'usuario' por omisión: el menor privilegio.
- `solicitudes_acceso`: una por correo verificado (UNIQUE), estado pendiente → aprobada o
  rechazada. Guarda quién decidió y cuándo. Se audita con el disparador general.
- `notificaciones`: bandeja por destinatario. `clave` identifica el incidente: el mismo
  incidente no se repite para el mismo destinatario (los reintentos suman `repeticiones`).
  La lectura (`leida_en`) es por destinatario y persiste tras cerrar sesión.

La bajada desactiva las cuentas con rol 'usuario' y revoca sus sesiones antes de quitar la
columna: sin ella, esas cuentas pasarían a ser administradores.
"""

from alembic import op

revision = "0008"
down_revision = "0007"


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE nexus.administradores
          ADD COLUMN rol text NOT NULL DEFAULT 'administrador'
          CONSTRAINT administradores_rol_check CHECK (rol IN ('administrador', 'usuario'));
        ALTER TABLE nexus.administradores ALTER COLUMN rol SET DEFAULT 'usuario';

        CREATE TABLE nexus.solicitudes_acceso (
          id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          correo                    citext NOT NULL CONSTRAINT solicitudes_acceso_correo_uq UNIQUE,
          nombre                    text CHECK (char_length(nombre) <= 200),
          google_sub                text CHECK (char_length(google_sub) <= 255),
          estado                    text NOT NULL DEFAULT 'pendiente'
                                    CHECK (estado IN ('pendiente', 'aprobada', 'rechazada')),
          creada_en                 timestamptz NOT NULL DEFAULT now(),
          ultima_identificacion_en  timestamptz NOT NULL DEFAULT now(),
          identificaciones          integer NOT NULL DEFAULT 1 CHECK (identificaciones >= 1),
          decidida_por              uuid REFERENCES nexus.administradores(id),
          decidida_en               timestamptz,
          cuenta_id                 uuid REFERENCES nexus.administradores(id),
          CONSTRAINT solicitudes_acceso_decision_check CHECK (
            (estado = 'pendiente' AND decidida_en IS NULL AND decidida_por IS NULL)
            OR (estado <> 'pendiente' AND decidida_en IS NOT NULL AND decidida_por IS NOT NULL)
          ),
          CONSTRAINT solicitudes_acceso_cuenta_check CHECK (estado = 'aprobada' OR cuenta_id IS NULL)
        );
        CREATE INDEX solicitudes_acceso_estado_idx ON nexus.solicitudes_acceso (estado, creada_en);
        CREATE TRIGGER auditar AFTER INSERT OR UPDATE OR DELETE ON nexus.solicitudes_acceso
          FOR EACH ROW EXECUTE FUNCTION nexus.fn_auditar();

        CREATE TABLE nexus.notificaciones (
          id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          destinatario_id  uuid NOT NULL REFERENCES nexus.administradores(id) ON DELETE CASCADE,
          tipo             text NOT NULL CHECK (tipo IN (
                             'solicitud_acceso', 'acceso_aprobado', 'pausa_envios',
                             'fallo_integracion', 'tarea_fallida')),
          titulo           text NOT NULL CHECK (char_length(titulo) BETWEEN 1 AND 200),
          descripcion      text NOT NULL CHECK (char_length(descripcion) BETWEEN 1 AND 1000),
          enlace           text CHECK (enlace ~ '^/[A-Za-z0-9/_?=&.-]*$'),
          clave            text NOT NULL CHECK (char_length(clave) BETWEEN 1 AND 300),
          repeticiones     integer NOT NULL DEFAULT 1 CHECK (repeticiones >= 1),
          creada_en        timestamptz NOT NULL DEFAULT now(),
          ultima_vez       timestamptz NOT NULL DEFAULT now(),
          leida_en         timestamptz,
          CONSTRAINT notificaciones_incidente_uq UNIQUE (destinatario_id, clave)
        );
        CREATE INDEX notificaciones_bandeja_idx
          ON nexus.notificaciones (destinatario_id, ultima_vez DESC);
        CREATE INDEX notificaciones_sin_leer_idx
          ON nexus.notificaciones (destinatario_id) WHERE leida_en IS NULL;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TABLE nexus.notificaciones;
        DROP TABLE nexus.solicitudes_acceso;
        UPDATE nexus.sesiones s SET revocada_en = now()
          FROM nexus.administradores a
         WHERE a.id = s.administrador_id AND a.rol = 'usuario' AND s.revocada_en IS NULL;
        UPDATE nexus.administradores SET activo = false WHERE rol = 'usuario';
        ALTER TABLE nexus.administradores DROP COLUMN rol;
        """
    )
