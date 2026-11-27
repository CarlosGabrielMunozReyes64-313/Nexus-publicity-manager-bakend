"""Eliminar un contacto desde la aplicación (distinto de darlo de baja).

`eliminar_contacto(id, motivo)`:
- Cancela sus envíos programados y finaliza sus inscripciones (motivo «manual»).
- Lo retira de todas las listas.
- Borra sus datos personales (nombre, correo, cargo, teléfono, evidencias, verificaciones y
  textos de mensajes y respuestas) y enmascara su rastro en la auditoría. Las filas
  quedan anónimas para que los totales y el historial de las campañas sigan cuadrando
  (mensajes, inscripciones y eventos no permiten borrado físico).
- NO crea una supresión: eliminar no es «darse de baja». Si el correo ya estaba dado de
  baja, la supresión se conserva y un contacto reimportado con ese correo queda excluido
  (disparador contacto_suprimido de la 0009). Sin baja previa, el correo queda libre para
  volver a importarse.
"""

from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade() -> None:
    op.execute(
        r"""
        CREATE FUNCTION nexus.eliminar_contacto(p_contacto uuid, p_motivo text) RETURNS void
        LANGUAGE plpgsql AS $$
        DECLARE
          c nexus.contactos;
        BEGIN
          SELECT * INTO c FROM nexus.contactos WHERE id = p_contacto FOR UPDATE;
          IF NOT FOUND THEN
            RAISE EXCEPTION 'Contacto % no encontrado', p_contacto;
          END IF;
          IF c.anonimizado_en IS NOT NULL THEN
            RETURN;
          END IF;

          PERFORM nexus.detener_envios(ARRAY[p_contacto], 'manual', true);
          UPDATE nexus.mensajes SET estado = 'cancelado',
                 ultimo_error = 'Contacto eliminado de la aplicación'
           WHERE contacto_id = p_contacto AND estado = 'programado';
          UPDATE nexus.lista_miembros
             SET activo = false, retirado_en = now(), motivo_retiro = 'anonimizado'
           WHERE contacto_id = p_contacto AND activo;

          UPDATE nexus.bases_legales
             SET vigente = false, revocada_en = now(), motivo_revocacion = 'Contacto eliminado'
           WHERE contacto_id = p_contacto AND vigente;
          DELETE FROM nexus.evidencias WHERE contacto_id = p_contacto;
          DELETE FROM nexus.verificaciones_correo WHERE contacto_id = p_contacto;
          UPDATE nexus.interacciones SET resumen = '[eliminado]' WHERE contacto_id = p_contacto;
          UPDATE nexus.respuestas SET texto = '[eliminado]', asunto = NULL, borrador = NULL,
                 remitente = 'eliminado@invalid'
           WHERE contacto_id = p_contacto;
          UPDATE nexus.mensajes SET cuerpo = '[eliminado]', cuerpo_html = NULL, asunto = NULL,
                 destinatario = 'eliminado@invalid'
           WHERE contacto_id = p_contacto;
          UPDATE nexus.envios_formulario SET nombre = NULL, correo = NULL, telefono = NULL,
                 cargo = NULL, datos = '{}'
           WHERE contacto_id = p_contacto;

          UPDATE nexus.contactos
             SET nombre = 'Contacto eliminado', cargo = NULL, correo = NULL, telefono = NULL,
                 estado_correo = 'desconocido', excluido = true, etapa = 'excluido',
                 motivo_exclusion = coalesce(motivo_exclusion, left('Eliminado: ' || p_motivo, 300)),
                 excluido_en = coalesce(excluido_en, now()), anonimizado_en = now()
           WHERE id = p_contacto;

          PERFORM set_config('nexus.anonimizando', 'on', true);
          UPDATE nexus.registro_auditoria
             SET antes = CASE WHEN antes IS NULL THEN NULL ELSE '{"eliminado": true}'::jsonb END,
                 despues = CASE WHEN despues IS NULL THEN NULL ELSE '{"eliminado": true}'::jsonb END
           WHERE (entidad = 'contactos' AND entidad_id = p_contacto::text)
              OR (antes ->> 'contacto_id') = p_contacto::text
              OR (despues ->> 'contacto_id') = p_contacto::text;
          PERFORM set_config('nexus.anonimizando', 'off', true);
        END $$;
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nexus_app') THEN
            GRANT EXECUTE ON FUNCTION nexus.eliminar_contacto(uuid, text) TO nexus_app;
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION nexus.eliminar_contacto(uuid, text);")
