-- =====================================================================
-- Nexus-publicity-manager · Pruebas del esquema (37 casos)
-- SOLO en una base de prueba recién creada: inserta datos ficticios.
--   createdb nexus_prueba
--   psql -d nexus_prueba -v ON_ERROR_STOP=1 -f nexus_publicity_manager_schema.sql
--   psql -d nexus_prueba -f nexus_publicity_manager_pruebas.sql
-- Cada caso imprime «OK». Si una regla falla, el archivo se detiene con FALLA.
-- =====================================================================

\set ON_ERROR_STOP 1
SET search_path = nexus, public;
SET client_min_messages = warning;

-- Ayudante: ejecuta SQL y confirma que falla.
CREATE FUNCTION pg_temp.debe_fallar(p_sql text, p_nombre text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE p_sql;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'OK (bloqueado): % -> %', p_nombre, SQLERRM;
    RETURN;
  END;
  RAISE EXCEPTION 'FALLA: se permitió %', p_nombre;
END $$;
SET client_min_messages = notice;

-- ---------- Datos base ----------
INSERT INTO administradores (id, correo, nombre) VALUES ('00000000-0000-0000-0000-0000000000a1', 'admin@nexus.co', 'Admin');
SET nexus.actor = 'admin';
SET nexus.administrador_id = '00000000-0000-0000-0000-0000000000a1';

INSERT INTO empresas (id, razon_social, nit, sector, tamano, municipio, sitio_web, dominio) VALUES
 ('00000000-0000-0000-0000-0000000000e1', 'Agroinsumos del Valle SAS', '900111222', 'Agroindustria', '11-50', 'Candelaria', 'https://agrovalle.co', 'agrovalle.co'),
 ('00000000-0000-0000-0000-0000000000e2', 'Logística Pacífico SAS', '900333444', 'Logística', '51-200', 'Palmira', 'https://logpac.co', 'logpac.co');

INSERT INTO contactos (id, empresa_id, nombre, cargo, correo, estado_correo, etapa, origen) VALUES
 ('00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000e1', 'Laura Gómez', 'Gerente', 'laura@agrovalle.co', 'valido', 'contactable', 'registro'),
 ('00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-0000000000e1', 'Pedro Ruiz', 'Compras', 'pedro@agrovalle.co', 'valido', 'contactable', 'registro'),
 ('00000000-0000-0000-0000-0000000000c3', '00000000-0000-0000-0000-0000000000e2', 'Ana Torres', 'Gerente', 'ana@logpac.co', 'valido', 'contactable', 'manual');

INSERT INTO bases_legales (contacto_id, tipo, texto, prueba, obtenida_en, aprobada_por, aprobada_en) VALUES
 ('00000000-0000-0000-0000-0000000000c1', 'corporativo_generico', 'Correo corporativo publicado', 'https://agrovalle.co/contacto', now(), '00000000-0000-0000-0000-0000000000a1', now()),
 ('00000000-0000-0000-0000-0000000000c2', 'corporativo_generico', 'Correo corporativo publicado', 'https://agrovalle.co/contacto', now(), '00000000-0000-0000-0000-0000000000a1', now());
-- c3 queda sin base legal aprobada.

SELECT pg_temp.debe_fallar($$INSERT INTO bases_legales (contacto_id, tipo, aprobada_por, aprobada_en)
  VALUES ('00000000-0000-0000-0000-0000000000c3', 'pendiente', '00000000-0000-0000-0000-0000000000a1', now())$$,
  'aprobar una base legal pendiente');
SELECT pg_temp.debe_fallar($$INSERT INTO evidencias (contacto_id, campo, fuente, estado) VALUES
  ('00000000-0000-0000-0000-0000000000c1', 'cargo', 'agente', 'verificado')$$,
  'dato de agente «verificado» sin cita comprobada');
SELECT pg_temp.debe_fallar($$UPDATE contactos SET excluido = true WHERE id = '00000000-0000-0000-0000-0000000000c1'$$,
  'excluir sin motivo');
SELECT pg_temp.debe_fallar($$INSERT INTO empresas (razon_social, municipio, dominio) VALUES ('Copia', 'Cali', 'AGROVALLE.CO')$$,
  'empresa con dominio repetido (sin importar mayúsculas)');

-- ---------- Secuencia, segmento, propuesta y aprobación ----------
INSERT INTO secuencias (id, nombre, objetivo, llamada_accion) VALUES
 ('00000000-0000-0000-0000-0000000000b1', 'RSE Express · proveedores', 'Agendar diagnóstico', 'Haz el diagnóstico gratuito');
SELECT pg_temp.debe_fallar($$INSERT INTO pasos (secuencia_id, orden, asunto, cuerpo) VALUES
  ('00000000-0000-0000-0000-0000000000b1', 1, 'Hola', 'Texto suficientemente largo pero sin enlace de baja. {{firma}}')$$,
  'paso sin {{baja}}');
SELECT pg_temp.debe_fallar($$INSERT INTO pasos (secuencia_id, orden, cuerpo) VALUES
  ('00000000-0000-0000-0000-0000000000b1', 1, 'Hola {{nombre}}, texto largo del primer paso. {{firma}} {{baja}}')$$,
  'primer paso sin asunto');
INSERT INTO pasos (secuencia_id, orden, asunto, cuerpo, espera_dias_habiles) VALUES
 ('00000000-0000-0000-0000-0000000000b1', 1, '¿Tus clientes ya piden RSE?', E'Hola {{nombre}},\nte escribo porque... \n{{firma}}\n{{baja}}', 0),
 ('00000000-0000-0000-0000-0000000000b1', 2, NULL, E'Hola {{nombre}}, retomo el correo anterior.\n{{firma}}\n{{baja}}', 3);

INSERT INTO segmentos (id, nombre, reglas, justificacion, estado, propuesto_por, aprobado_por, aprobado_en) VALUES
 ('00000000-0000-0000-0000-0000000000d1', 'Proveedores agro', '[{"campo":"sector","operador":"es","valor":"Agroindustria"}]',
  'Ajuste alto', 'aprobado', 'agente', '00000000-0000-0000-0000-0000000000a1', now());

INSERT INTO campanas (id, nombre, segmento_id, secuencia_id) VALUES
 ('00000000-0000-0000-0000-0000000000f1', 'Piloto proveedores', '00000000-0000-0000-0000-0000000000d1', '00000000-0000-0000-0000-0000000000b1');

SELECT pg_temp.debe_fallar($$UPDATE campanas SET estado = 'activa' WHERE id = '00000000-0000-0000-0000-0000000000f1'$$,
  'activar campaña sin aprobación');
SELECT pg_temp.debe_fallar($$INSERT INTO inscripciones (campana_id, contacto_id) VALUES
  ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c1')$$,
  'inscribir en campaña sin aprobación');

INSERT INTO prompts (id, agente, version, modelo, texto, activo) VALUES
 ('00000000-0000-0000-0000-000000000011', 'estratega_redactor', 1, 'claude-sonnet', 'Redacta...', true);
SELECT pg_temp.debe_fallar($$INSERT INTO prompts (agente, version, modelo, texto, activo) VALUES
  ('estratega_redactor', 2, 'claude-sonnet', 'Otro', true)$$, 'dos prompts activos para un agente');

INSERT INTO propuestas (id, agente, tipo, resumen, objetivo_campana_id) VALUES
 ('00000000-0000-0000-0000-000000000021', 'estratega_redactor', 'campana', 'Campaña piloto', '00000000-0000-0000-0000-0000000000f1');
INSERT INTO propuesta_versiones (id, propuesta_id, version, contenido, autor) VALUES
 ('00000000-0000-0000-0000-000000000022', '00000000-0000-0000-0000-000000000021', 1, '{"x":1}', 'agente');
SELECT pg_temp.debe_fallar($$UPDATE propuesta_versiones SET contenido = '{"x":2}' WHERE id = '00000000-0000-0000-0000-000000000022'$$,
  'reescribir una versión de propuesta');
SELECT pg_temp.debe_fallar($$UPDATE propuesta_versiones SET decision = 'rechazada', decidida_por = '00000000-0000-0000-0000-0000000000a1', decidida_en = now()
  WHERE id = '00000000-0000-0000-0000-000000000022'$$, 'rechazar sin comentario');
UPDATE propuesta_versiones SET decision = 'aprobada', decidida_por = '00000000-0000-0000-0000-0000000000a1', decidida_en = now()
 WHERE id = '00000000-0000-0000-0000-000000000022';
SELECT pg_temp.debe_fallar($$INSERT INTO propuestas (agente, tipo, estado, resumen, objetivo_contacto_id) VALUES
  ('investigador', 'correo_inferido', 'aprobada', 'Correo deducido', '00000000-0000-0000-0000-0000000000c3')$$,
  'aprobar un correo inferido');

INSERT INTO aprobaciones_campana (id, campana_id, propuesta_version_id, version_secuencia, version_segmento, version_marca, contenido_congelado, aprobada_por)
SELECT '00000000-0000-0000-0000-000000000031', '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-000000000022',
       s.version, 1, 1,
       jsonb_build_object('pasos', (SELECT jsonb_agg(to_jsonb(p) ORDER BY orden) FROM pasos p WHERE p.secuencia_id = s.id)),
       '00000000-0000-0000-0000-0000000000a1'
  FROM secuencias s WHERE s.id = '00000000-0000-0000-0000-0000000000b1';

DO $$ BEGIN
  ASSERT (SELECT huella ~ '^[0-9a-f]{64}$' FROM nexus.aprobaciones_campana WHERE id = '00000000-0000-0000-0000-000000000031'),
         'la huella no se calculó';
  RAISE NOTICE 'OK: huella SHA-256 calculada con saltos de línea en el contenido';
END $$;
SELECT pg_temp.debe_fallar($$UPDATE aprobaciones_campana SET contenido_congelado = '{}' WHERE id = '00000000-0000-0000-0000-000000000031'$$,
  'editar el contenido congelado');

UPDATE campanas SET aprobacion_vigente_id = '00000000-0000-0000-0000-000000000031', estado = 'activa'
 WHERE id = '00000000-0000-0000-0000-0000000000f1';

-- ---------- Inscripción y envíos ----------
SELECT pg_temp.debe_fallar($$INSERT INTO inscripciones (campana_id, contacto_id) VALUES
  ('00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c3')$$,
  'inscribir contacto sin base legal');

INSERT INTO inscripciones (id, campana_id, contacto_id, estado, proximo_envio) VALUES
 ('00000000-0000-0000-0000-000000000041', '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c1', 'activa', now()),
 ('00000000-0000-0000-0000-000000000042', '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-0000000000c2', 'activa', now());

INSERT INTO mensajes (id, tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, asunto, cuerpo)
VALUES ('00000000-0000-0000-0000-000000000051', 'secuencia', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000041',
        '00000000-0000-0000-0000-000000000031', 1, 'ins41-p1', 'laura@agrovalle.co', 'Asunto', 'Cuerpo');
SELECT pg_temp.debe_fallar($$INSERT INTO mensajes (tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, cuerpo)
  VALUES ('secuencia', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000041',
          '00000000-0000-0000-0000-000000000031', 1, 'ins41-p1-otra', 'laura@agrovalle.co', 'Cuerpo')$$,
  'enviar dos veces el mismo paso');
SELECT pg_temp.debe_fallar($$INSERT INTO mensajes (tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, cuerpo)
  VALUES ('secuencia', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000041',
          '00000000-0000-0000-0000-000000000031', 2, 'ins41-p1', 'laura@agrovalle.co', 'Cuerpo')$$,
  'repetir la clave de idempotencia');
SELECT pg_temp.debe_fallar($$INSERT INTO mensajes (tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, cuerpo)
  VALUES ('secuencia', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000041',
          '00000000-0000-0000-0000-000000000031', 2, 'ins41-p2', 'otra@persona.co', 'Cuerpo')$$,
  'enviar a un destinatario distinto del contacto');

-- Marcar como enviado.
UPDATE mensajes SET estado = 'enviado', enviado_en = now(), gmail_message_id = 'g-1', gmail_thread_id = 't-1'
 WHERE id = '00000000-0000-0000-0000-000000000051';

-- Programar el paso 2 y luego llega una respuesta: debe pausar y cancelar.
INSERT INTO mensajes (id, tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, cuerpo, programado_para)
VALUES ('00000000-0000-0000-0000-000000000052', 'secuencia', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000041',
        '00000000-0000-0000-0000-000000000031', 2, 'ins41-p2', 'laura@agrovalle.co', 'Cuerpo', now() + interval '3 days');

INSERT INTO respuestas (id, contacto_id, campana_id, inscripcion_id, mensaje_origen_id, gmail_message_id, gmail_thread_id, remitente, recibida_en, texto)
VALUES ('00000000-0000-0000-0000-000000000061', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-0000000000f1',
        '00000000-0000-0000-0000-000000000041', '00000000-0000-0000-0000-000000000051', 'g-r1', 't-1', 'laura@agrovalle.co', now(),
        'Me interesa, ¿podemos hablar el jueves?');

DO $$ BEGIN
  ASSERT (SELECT estado FROM nexus.inscripciones WHERE id = '00000000-0000-0000-0000-000000000041') = 'pausada', 'la respuesta no pausó';
  ASSERT (SELECT estado FROM nexus.mensajes WHERE id = '00000000-0000-0000-0000-000000000052') = 'cancelado', 'no se canceló el paso 2';
  RAISE NOTICE 'OK: una respuesta pausa la secuencia y cancela el siguiente correo';
END $$;

-- Respuesta manual del administrador en el mismo hilo: permitida.
INSERT INTO mensajes (tipo, contacto_id, en_respuesta_a, clave_idempotencia, destinatario, cuerpo)
VALUES ('respuesta_manual', '00000000-0000-0000-0000-0000000000c1', '00000000-0000-0000-0000-000000000061', 'resp-61', 'laura@agrovalle.co', 'Claro, el jueves a las 9.');

-- ---------- Cambio de contenido revoca la aprobación ----------
UPDATE pasos SET cuerpo = cuerpo || ' (editado)' WHERE secuencia_id = '00000000-0000-0000-0000-0000000000b1' AND orden = 2;
DO $$ BEGIN
  ASSERT (SELECT estado FROM nexus.campanas WHERE id = '00000000-0000-0000-0000-0000000000f1') = 'en_revision', 'la campaña sigue activa';
  ASSERT (SELECT aprobacion_vigente_id IS NULL FROM nexus.campanas WHERE id = '00000000-0000-0000-0000-0000000000f1'), 'aprobación no retirada';
  ASSERT (SELECT revocada_en IS NOT NULL FROM nexus.aprobaciones_campana WHERE id = '00000000-0000-0000-0000-000000000031'), 'no se revocó';
  ASSERT (SELECT estado FROM nexus.inscripciones WHERE id = '00000000-0000-0000-0000-000000000042') = 'pausada', 'inscripción sigue activa';
  RAISE NOTICE 'OK: editar un paso revoca la aprobación, devuelve la campaña a revisión y pausa las inscripciones';
END $$;
SELECT pg_temp.debe_fallar($$INSERT INTO mensajes (tipo, contacto_id, inscripcion_id, aprobacion_id, paso_orden, clave_idempotencia, destinatario, cuerpo)
  VALUES ('secuencia', '00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-000000000042',
          '00000000-0000-0000-0000-000000000031', 1, 'ins42-p1', 'pedro@agrovalle.co', 'Cuerpo')$$,
  'enviar con una aprobación revocada');

-- ---------- Supresiones y eventos ----------
INSERT INTO eventos_correo (tipo, contacto_id, id_externo) VALUES ('rebote_duro', '00000000-0000-0000-0000-0000000000c2', 'bounce-1');
DO $$ BEGIN
  ASSERT (SELECT estado_correo FROM nexus.contactos WHERE id = '00000000-0000-0000-0000-0000000000c2') = 'invalido', 'correo no marcado';
  ASSERT EXISTS (SELECT 1 FROM nexus.supresiones WHERE correo = 'pedro@agrovalle.co' AND origen = 'rebote_duro'), 'sin supresión';
  ASSERT (SELECT excluido FROM nexus.contactos WHERE id = '00000000-0000-0000-0000-0000000000c2'), 'no excluido';
  ASSERT (SELECT estado || '/' || motivo_salida FROM nexus.inscripciones WHERE id = '00000000-0000-0000-0000-000000000042') = 'finalizada/rebote', 'inscripción no finalizada';
  RAISE NOTICE 'OK: un rebote duro invalida el correo, lo suprime y saca al contacto de la campaña';
END $$;

INSERT INTO supresiones (dominio, motivo, origen) VALUES ('logpac.co', 'La empresa pidió no ser contactada', 'manual');
DO $$ BEGIN
  ASSERT (SELECT etapa FROM nexus.contactos WHERE id = '00000000-0000-0000-0000-0000000000c3') = 'excluido', 'supresión por dominio no aplicó';
  ASSERT nexus.motivo_no_elegible('00000000-0000-0000-0000-0000000000c3') IS NOT NULL;
  RAISE NOTICE 'OK: suprimir un dominio excluye a todos sus contactos';
END $$;
SELECT pg_temp.debe_fallar($$INSERT INTO mensajes (tipo, contacto_id, en_respuesta_a, clave_idempotencia, destinatario, cuerpo)
  VALUES ('respuesta_manual', '00000000-0000-0000-0000-0000000000c2', '00000000-0000-0000-0000-000000000061', 'resp-x', 'pedro@agrovalle.co', 'Hola')$$,
  'respuesta manual a un contacto suprimido');

-- ---------- Marca: editarla revoca todo ----------
INSERT INTO aprobaciones_campana (id, campana_id, propuesta_version_id, version_secuencia, version_segmento, version_marca, contenido_congelado, aprobada_por)
VALUES ('00000000-0000-0000-0000-000000000032', '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-000000000022', 4, 1, 1, '{"v":2}', '00000000-0000-0000-0000-0000000000a1');
UPDATE campanas SET aprobacion_vigente_id = '00000000-0000-0000-0000-000000000032', estado = 'activa' WHERE id = '00000000-0000-0000-0000-0000000000f1';
UPDATE configuracion SET valor = valor || '{"firma":"Equipo NEXUS"}' WHERE clave = 'marca';
DO $$ BEGIN
  ASSERT (SELECT version FROM nexus.configuracion WHERE clave = 'marca') = 2, 'versión de marca no subió';
  ASSERT (SELECT estado FROM nexus.campanas WHERE id = '00000000-0000-0000-0000-0000000000f1') = 'en_revision', 'marca no revocó';
  RAISE NOTICE 'OK: cambiar la marca revoca las aprobaciones vigentes';
END $$;

-- ---------- Comercial: ganar convierte en cliente ----------
INSERT INTO aprobaciones_campana (id, campana_id, propuesta_version_id, version_secuencia, version_segmento, version_marca, contenido_congelado, aprobada_por)
VALUES ('00000000-0000-0000-0000-000000000033', '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-000000000022', 4, 1, 2, '{"v":3}', '00000000-0000-0000-0000-0000000000a1');
UPDATE campanas SET aprobacion_vigente_id = '00000000-0000-0000-0000-000000000033', estado = 'activa' WHERE id = '00000000-0000-0000-0000-0000000000f1';
UPDATE inscripciones SET estado = 'activa', proximo_envio = now() WHERE id = '00000000-0000-0000-0000-000000000041';
SET nexus.evento = 'reunion_realizada';
INSERT INTO oportunidades (id, empresa_id, contacto_id, etapa, reto, valor_estimado_cop, campana_origen_id, respuesta_origen_id)
VALUES ('00000000-0000-0000-0000-000000000071', '00000000-0000-0000-0000-0000000000e1', '00000000-0000-0000-0000-0000000000c1', 'discovery',
        'Reporte de sostenibilidad para su cliente ancla', 12000000, '00000000-0000-0000-0000-0000000000f1', '00000000-0000-0000-0000-000000000061');
SELECT pg_temp.debe_fallar($$UPDATE oportunidades SET etapa = 'cliente' WHERE id = '00000000-0000-0000-0000-000000000071'$$,
  'cerrar oportunidad sin fecha de cierre');
UPDATE oportunidades SET etapa = 'cliente', cerrada_en = now() WHERE id = '00000000-0000-0000-0000-000000000071';
DO $$ BEGIN
  ASSERT (SELECT estado_cuenta FROM nexus.empresas WHERE id = '00000000-0000-0000-0000-0000000000e1') = 'cliente', 'empresa no es cliente';
  ASSERT (SELECT estado || '/' || motivo_salida FROM nexus.inscripciones WHERE id = '00000000-0000-0000-0000-000000000041') = 'finalizada/cliente', 'no salió';
  RAISE NOTICE 'OK: ganar la oportunidad vuelve cliente a la empresa y la saca de las secuencias';
END $$;
RESET nexus.evento;

-- ---------- Cola de tareas ----------
INSERT INTO tareas (tipo, prioridad, clave_unica) VALUES ('sincronizar_gmail', 2, 'sync-1'), ('enviar_mensaje', 5, 'env-1'), ('ejecutar_agente', 8, 'ag-1');
SELECT pg_temp.debe_fallar($$INSERT INTO tareas (tipo, clave_unica) VALUES ('sincronizar_gmail', 'sync-1')$$, 'encolar dos veces la misma tarea');
DO $$
DECLARE t nexus.tareas; r text;
BEGIN
  SELECT * INTO t FROM nexus.tomar_tareas('trabajador-1', 1);
  ASSERT t.tipo = 'sincronizar_gmail', 'no tomó la de mayor prioridad';
  r := nexus.terminar_tarea(t.id, false, 'Gmail no respondió');
  ASSERT r = 'reprogramada', r;
  ASSERT (SELECT ejecutar_desde > now() FROM nexus.tareas WHERE id = t.id), 'sin espera';
  RAISE NOTICE 'OK: la cola entrega por prioridad y reprograma con espera al fallar';
END $$;

-- ---------- Auditoría ----------
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM nexus.registro_auditoria) > 20, 'auditoría vacía';
  ASSERT (SELECT count(*) FROM nexus.registro_auditoria WHERE actor = 'admin') > 0, 'actor no registrado';
  ASSERT NOT EXISTS (SELECT 1 FROM nexus.registro_auditoria WHERE despues ? 'credenciales_cifradas'), 'se auditó una credencial';
  RAISE NOTICE 'OK: % filas de auditoría con actor', (SELECT count(*) FROM nexus.registro_auditoria);
END $$;
SELECT pg_temp.debe_fallar($$UPDATE registro_auditoria SET actor = 'otro'$$, 'editar la auditoría');
SELECT pg_temp.debe_fallar($$DELETE FROM registro_auditoria$$, 'borrar la auditoría');
SELECT pg_temp.debe_fallar($$TRUNCATE registro_auditoria$$, 'vaciar la auditoría');

-- ---------- Embudo ----------
DO $$ BEGIN
  ASSERT (SELECT count(*) FROM nexus.cambios_etapa WHERE contacto_id = '00000000-0000-0000-0000-0000000000c2') >= 2, 'sin historial';
  RAISE NOTICE 'OK: el historial de etapas registra cada cambio';
END $$;

-- ---------- Ley 1581: anonimizar ----------
SELECT anonimizar_contacto('00000000-0000-0000-0000-0000000000c1', 'Solicitud por correo del titular');
DO $$ BEGIN
  ASSERT (SELECT correo IS NULL AND nombre = 'Contacto anonimizado' FROM nexus.contactos WHERE id = '00000000-0000-0000-0000-0000000000c1'), 'no anonimizó';
  ASSERT EXISTS (SELECT 1 FROM nexus.supresiones WHERE correo = 'laura@agrovalle.co'), 'correo no quedó suprimido';
  ASSERT NOT EXISTS (SELECT 1 FROM nexus.registro_auditoria WHERE coalesce(antes::text, '') || coalesce(despues::text, '') LIKE '%Laura%'), 'nombre en auditoría';
  ASSERT NOT EXISTS (SELECT 1 FROM nexus.respuestas WHERE texto LIKE '%jueves%'), 'texto de respuesta visible';
  RAISE NOTICE 'OK: anonimizar borra los datos personales, también en la auditoría, y mantiene la supresión';
END $$;
SELECT pg_temp.debe_fallar($$UPDATE registro_auditoria SET actor = 'otro'$$, 'editar la auditoría después de anonimizar');

-- ---------- Vistas ----------
SELECT 'v_embudo' AS vista, count(*) FROM v_embudo
UNION ALL SELECT 'v_contactos_elegibilidad', count(*) FROM v_contactos_elegibilidad
UNION ALL SELECT 'v_base_legal_vigente', count(*) FROM v_base_legal_vigente
UNION ALL SELECT 'v_puntaje_actual', count(*) FROM v_puntaje_actual
UNION ALL SELECT 'v_metricas_campana', count(*) FROM v_metricas_campana
UNION ALL SELECT 'v_linea_tiempo', count(*) FROM v_linea_tiempo
UNION ALL SELECT 'v_resumen_panel', count(*) FROM v_resumen_panel
UNION ALL SELECT 'v_metricas_correo_diarias', count(*) FROM v_metricas_correo_diarias;
SELECT * FROM v_metricas_campana;
SELECT * FROM v_resumen_panel;
DO $$ BEGIN
  ASSERT NOT EXISTS (SELECT 1 FROM nexus.mensajes WHERE contacto_id = '00000000-0000-0000-0000-0000000000c1' AND estado = 'programado'),
         'quedó un correo programado para un contacto anonimizado';
  RAISE NOTICE 'OK: no queda ningún correo programado para el contacto anonimizado';
END $$;
