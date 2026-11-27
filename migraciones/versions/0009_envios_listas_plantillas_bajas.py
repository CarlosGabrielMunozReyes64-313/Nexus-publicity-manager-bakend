"""Envíos programados al aprobar, listas de envío, plantillas y sincronización de bajas.

Esquema (base principal, DATABASE_URL):
- `listas` y `lista_miembros`: listas estáticas (miembros guardados) o vinculadas a un
  segmento (los miembros se calculan al usarla). Un miembro retirado conserva su fila
  (`activo=false`, motivo y fecha) para no perder el historial.
- `campanas.lista_id`: audiencia alternativa al segmento. `segmento_id` deja de ser
  obligatorio, pero toda campaña tiene al menos una de las dos (CHECK).
- `campanas.incorporar_nuevos`: si la audiencia gana miembros después de aprobar, el
  trabajador los inscribe mientras la campaña esté activa (true) o solo cuentan los que
  había al aprobar o reanudar (false, valor por omisión: el comportamiento anterior).
- `campanas.programada_en` y `campanas.motivo_no_programada`: resultado visible del
  último intento de programar los envíos al aprobar o reanudar.
- `mensajes.conciliacion`: «pendiente» cuando Gmail pudo aceptar el correo pero el
  proceso falló antes de guardar la respuesta. Nunca se reenvía solo.
- `plantillas`: contenido reutilizable. Un paso copia el contenido; la versión aprobada
  congela su copia, así que editar o archivar la plantilla no cambia lo aprobado.
- `sincronizacion_bajas`: bandeja de salida durable hacia la base de bajas
  (UNSUBSCRIBE_DATABASE_URL). Un disparador la llena en la misma transacción que crea la
  supresión; el trabajador la copia después. Se precarga con las supresiones existentes.
- Contactos: un correo suprimido queda excluido al crearse o al cambiar de correo
  (reimportar un correo dado de baja nunca lo reactiva).
- Una supresión también retira al contacto de las listas activas.

Ningún CHECK ni disparador existente se debilita, salvo `campanas.segmento_id NOT NULL`,
que se sustituye por «segmento o lista».
"""

from alembic import op

revision = "0009"
down_revision = "0008"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE TABLE nexus.listas (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          nombre          text NOT NULL CHECK (char_length(trim(nombre)) BETWEEN 1 AND 120),
          tipo            text NOT NULL DEFAULT 'estatica' CHECK (tipo IN ('estatica', 'vinculada')),
          segmento_id     uuid REFERENCES nexus.segmentos(id),
          descripcion     text NOT NULL DEFAULT '' CHECK (char_length(descripcion) <= 500),
          archivada_en    timestamptz,
          creado_en       timestamptz NOT NULL DEFAULT now(),
          actualizado_en  timestamptz NOT NULL DEFAULT now(),
          CHECK (tipo <> 'vinculada' OR segmento_id IS NOT NULL)
        );
        CREATE UNIQUE INDEX listas_nombre_activa_uq ON nexus.listas (lower(nombre))
          WHERE archivada_en IS NULL;

        CREATE TABLE nexus.lista_miembros (
          lista_id       uuid NOT NULL REFERENCES nexus.listas(id) ON DELETE CASCADE,
          contacto_id    uuid NOT NULL REFERENCES nexus.contactos(id) ON DELETE CASCADE,
          activo         boolean NOT NULL DEFAULT true,
          agregado_en    timestamptz NOT NULL DEFAULT now(),
          retirado_en    timestamptz,
          motivo_retiro  text CHECK (motivo_retiro IN ('manual', 'baja', 'anonimizado')),
          PRIMARY KEY (lista_id, contacto_id),
          CHECK (activo = (retirado_en IS NULL)),
          CHECK (activo OR motivo_retiro IS NOT NULL)
        );
        CREATE INDEX lista_miembros_contacto_idx ON nexus.lista_miembros (contacto_id) WHERE activo;

        ALTER TABLE nexus.campanas
          ADD COLUMN lista_id uuid REFERENCES nexus.listas(id),
          ADD COLUMN incorporar_nuevos boolean NOT NULL DEFAULT false,
          ADD COLUMN programada_en timestamptz,
          ADD COLUMN motivo_no_programada text CHECK (char_length(motivo_no_programada) <= 500);
        ALTER TABLE nexus.campanas ALTER COLUMN segmento_id DROP NOT NULL;
        ALTER TABLE nexus.campanas ADD CONSTRAINT campanas_audiencia_check
          CHECK (segmento_id IS NOT NULL OR lista_id IS NOT NULL);
        CREATE INDEX campanas_lista_idx ON nexus.campanas (lista_id);

        ALTER TABLE nexus.mensajes
          ADD COLUMN conciliacion text CHECK (conciliacion IN ('pendiente', 'enviado', 'no_enviado'));
        CREATE INDEX mensajes_conciliacion_idx ON nexus.mensajes (creado_en)
          WHERE conciliacion = 'pendiente';

        CREATE TABLE nexus.plantillas (
          id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          nombre          text NOT NULL CHECK (char_length(trim(nombre)) BETWEEN 1 AND 120),
          asunto          text NOT NULL DEFAULT '' CHECK (char_length(asunto) <= 200),
          cuerpo          text NOT NULL CHECK (char_length(cuerpo) BETWEEN 1 AND 3000),
          formato         text NOT NULL DEFAULT 'texto' CHECK (formato IN ('texto', 'html')),
          cuerpo_html     text CHECK (char_length(cuerpo_html) <= 20000),
          version         integer NOT NULL DEFAULT 1 CHECK (version > 0),
          archivada_en    timestamptz,
          creado_en       timestamptz NOT NULL DEFAULT now(),
          actualizado_en  timestamptz NOT NULL DEFAULT now(),
          CHECK (formato <> 'html' OR cuerpo_html IS NOT NULL)
        );

        CREATE TABLE nexus.sincronizacion_bajas (
          id               bigserial PRIMARY KEY,
          supresion_id     uuid REFERENCES nexus.supresiones(id) ON DELETE SET NULL,
          correo           citext NOT NULL UNIQUE,
          origen           text NOT NULL,
          dado_de_baja_en  timestamptz NOT NULL,
          estado           text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'sincronizada')),
          intentos         integer NOT NULL DEFAULT 0 CHECK (intentos >= 0),
          ultimo_error     text CHECK (char_length(ultimo_error) <= 300),
          proximo_intento  timestamptz NOT NULL DEFAULT now(),
          creado_en        timestamptz NOT NULL DEFAULT now(),
          sincronizada_en  timestamptz,
          CHECK ((estado = 'sincronizada') = (sincronizada_en IS NOT NULL))
        );
        CREATE INDEX sincronizacion_bajas_pendientes_idx
          ON nexus.sincronizacion_bajas (proximo_intento) WHERE estado = 'pendiente';
        INSERT INTO nexus.sincronizacion_bajas (supresion_id, correo, origen, dado_de_baja_en)
          SELECT id, lower(correo::text), origen, creado_en FROM nexus.supresiones
           WHERE correo IS NOT NULL
          ON CONFLICT (correo) DO NOTHING;

        -- Una supresión nueva: además de excluir y detener envíos (supresion_aplica),
        -- retira de las listas y deja la copia a la base de bajas en la bandeja de salida,
        -- todo en la misma transacción.
        CREATE FUNCTION nexus.fn_supresion_listas_y_salida() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
          UPDATE nexus.lista_miembros lm
             SET activo = false, retirado_en = now(), motivo_retiro = 'baja'
            FROM nexus.contactos c
           WHERE lm.contacto_id = c.id AND lm.activo
             AND ((NEW.correo IS NOT NULL AND c.correo = NEW.correo)
               OR (NEW.dominio IS NOT NULL AND split_part(c.correo::text, '@', 2)::citext = NEW.dominio));
          IF NEW.correo IS NOT NULL THEN
            INSERT INTO nexus.sincronizacion_bajas (supresion_id, correo, origen, dado_de_baja_en)
            VALUES (NEW.id, lower(NEW.correo::text), NEW.origen, NEW.creado_en)
            ON CONFLICT (correo) DO NOTHING;
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER supresion_listas_y_salida AFTER INSERT ON nexus.supresiones
          FOR EACH ROW EXECUTE FUNCTION nexus.fn_supresion_listas_y_salida();

        -- Un correo suprimido nunca vuelve a ser contactable al crearse o al cambiarse.
        CREATE FUNCTION nexus.fn_contacto_suprimido() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
          s nexus.supresiones;
        BEGIN
          IF NEW.correo IS NULL OR NEW.anonimizado_en IS NOT NULL THEN
            RETURN NEW;
          END IF;
          IF TG_OP = 'UPDATE' AND NEW.correo IS NOT DISTINCT FROM OLD.correo AND NEW.excluido THEN
            RETURN NEW;
          END IF;
          SELECT * INTO s FROM nexus.supresiones
           WHERE correo = NEW.correo OR dominio = split_part(NEW.correo::text, '@', 2)::citext
           LIMIT 1;
          IF FOUND THEN
            NEW.excluido := true;
            NEW.etapa := 'excluido';
            NEW.motivo_exclusion := coalesce(NEW.motivo_exclusion, s.motivo);
            NEW.excluido_en := coalesce(NEW.excluido_en, now());
          END IF;
          RETURN NEW;
        END $$;
        CREATE TRIGGER contacto_suprimido BEFORE INSERT OR UPDATE OF correo, excluido ON nexus.contactos
          FOR EACH ROW EXECUTE FUNCTION nexus.fn_contacto_suprimido();

        DO $$
        DECLARE
          t text;
        BEGIN
          FOREACH t IN ARRAY ARRAY['listas', 'plantillas'] LOOP
            EXECUTE format('CREATE TRIGGER auditar AFTER INSERT OR UPDATE OR DELETE ON nexus.%I
                            FOR EACH ROW EXECUTE FUNCTION nexus.fn_auditar()', t);
            EXECUTE format('CREATE TRIGGER actualizado_en BEFORE UPDATE ON nexus.%I
                            FOR EACH ROW EXECUTE FUNCTION nexus.fn_actualizado_en()', t);
          END LOOP;
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT SELECT, INSERT, UPDATE, DELETE ON nexus.listas, nexus.lista_miembros,
              nexus.plantillas, nexus.sincronizacion_bajas TO nexus_app;
            GRANT USAGE, SELECT ON SEQUENCE nexus.sincronizacion_bajas_id_seq TO nexus_app;
            GRANT EXECUTE ON FUNCTION nexus.fn_supresion_listas_y_salida(),
              nexus.fn_contacto_suprimido() TO nexus_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER contacto_suprimido ON nexus.contactos;
        DROP FUNCTION nexus.fn_contacto_suprimido();
        DROP TRIGGER supresion_listas_y_salida ON nexus.supresiones;
        DROP FUNCTION nexus.fn_supresion_listas_y_salida();
        DROP TABLE nexus.sincronizacion_bajas;
        DROP TABLE nexus.plantillas;
        DROP INDEX nexus.mensajes_conciliacion_idx;
        ALTER TABLE nexus.mensajes DROP COLUMN conciliacion;
        ALTER TABLE nexus.campanas DROP CONSTRAINT campanas_audiencia_check;
        -- Las campañas cuya audiencia es solo una lista conservan a sus destinatarios en un
        -- segmento con la regla «contactoIds» (la forma de selección exacta de 0008).
        DO $$
        DECLARE
          r record;
          s uuid;
        BEGIN
          FOR r IN SELECT ca.id, l.nombre, l.tipo, l.segmento_id, ca.lista_id
                     FROM nexus.campanas ca JOIN nexus.listas l ON l.id = ca.lista_id
                    WHERE ca.segmento_id IS NULL LOOP
            IF r.tipo = 'vinculada' THEN
              s := r.segmento_id;
            ELSE
              INSERT INTO nexus.segmentos (nombre, reglas, justificacion, propuesto_por, estado,
                                           aprobado_en)
              SELECT 'Lista ' || r.nombre || ' (bajada de 0009)',
                     jsonb_build_array(jsonb_build_object('campo', 'contactoIds', 'operador', 'es',
                       'valor', coalesce(string_agg(lm.contacto_id::text, ','), ''))),
                     'Miembros de la lista al bajar la migración 0009', 'admin', 'aprobado', now()
                FROM nexus.lista_miembros lm WHERE lm.lista_id = r.lista_id AND lm.activo
              RETURNING id INTO s;
            END IF;
            UPDATE nexus.campanas SET segmento_id = s WHERE id = r.id;
          END LOOP;
        END $$;
        ALTER TABLE nexus.campanas ALTER COLUMN segmento_id SET NOT NULL;
        ALTER TABLE nexus.campanas DROP COLUMN lista_id, DROP COLUMN incorporar_nuevos,
          DROP COLUMN programada_en, DROP COLUMN motivo_no_programada;
        DROP TABLE nexus.lista_miembros;
        DROP TABLE nexus.listas;
        """
    )
