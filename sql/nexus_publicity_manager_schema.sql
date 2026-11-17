-- =====================================================================
-- Nexus-publicity-manager · Esquema de base de datos
-- Motor: PostgreSQL 16 o superior
-- Versión 1 · Noviembre de 2026
--
-- Crea el esquema "nexus" con 40 tablas, sus reglas, disparadores,
-- funciones y vistas. Se ejecuta completo dentro de una transacción:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f nexus_publicity_manager_schema.sql
--
-- Convenciones:
--   · Nombres en español y snake_case. El backend los convierte a camelCase
--     para la API (contacto_id -> contactoId).
--   · Identificadores uuid (gen_random_uuid). Fechas timestamptz en UTC;
--     la presentación se hace en America/Bogota.
--   · Estados como text + CHECK, fáciles de ampliar con ALTER TABLE.
--   · El backend identifica quién actúa en cada transacción con:
--       SET LOCAL nexus.actor = 'admin' | 'agente:investigador' | 'proceso:motor';
--       SET LOCAL nexus.administrador_id = '<uuid>';   -- opcional
--       SET LOCAL nexus.accion = 'aprobar_propuesta';   -- opcional
--       SET LOCAL nexus.evento = 'respuesta_positiva';  -- opcional, cambios de etapa
-- =====================================================================

BEGIN;

CREATE EXTENSION IF NOT EXISTS citext;

CREATE SCHEMA IF NOT EXISTS nexus;
SET search_path = nexus, public;

-- Lee una variable de sesión de la app sin fallar si no existe.
CREATE FUNCTION ajuste(p_nombre text) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT nullif(current_setting(p_nombre, true), '')
$$;

-- Mantiene actualizado_en al día.
CREATE FUNCTION fn_actualizado_en() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.actualizado_en := now();
  RETURN NEW;
END $$;


-- =====================================================================
-- GRUPO 1 · ACCESO Y CONFIGURACIÓN
-- =====================================================================

-- Personas autorizadas a usar la app (hoy, un único administrador).
CREATE TABLE administradores (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  correo         citext NOT NULL UNIQUE,
  nombre         text NOT NULL,
  activo         boolean NOT NULL DEFAULT true,
  creado_en      timestamptz NOT NULL DEFAULT now(),
  ultimo_acceso  timestamptz
);

-- Sesiones iniciadas con Google. Solo se guarda el hash del token.
CREATE TABLE sesiones (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  administrador_id  uuid NOT NULL REFERENCES administradores(id) ON DELETE CASCADE,
  token_hash        bytea NOT NULL UNIQUE,
  creada_en         timestamptz NOT NULL DEFAULT now(),
  expira_en         timestamptz NOT NULL,
  revocada_en       timestamptz,
  navegador         text,
  CHECK (expira_en > creada_en)
);
CREATE INDEX sesiones_administrador_idx ON sesiones (administrador_id) WHERE revocada_en IS NULL;

-- Marca, límites de envío y cliente ideal (tres filas).
CREATE TABLE configuracion (
  clave            text PRIMARY KEY CHECK (clave IN ('marca', 'limites', 'cliente_ideal')),
  valor            jsonb NOT NULL CHECK (jsonb_typeof(valor) = 'object'),
  version          integer NOT NULL DEFAULT 1 CHECK (version > 0),
  actualizado_en   timestamptz NOT NULL DEFAULT now(),
  actualizado_por  uuid REFERENCES administradores(id)
);

-- Conexiones externas. Las credenciales se guardan cifradas por la app
-- (la llave vive en el gestor de secretos, nunca en la base).
CREATE TABLE integraciones (
  id                       text PRIMARY KEY
                           CHECK (id IN ('gmail', 'formulario', 'ga4', 'search_console', 'verificador', 'claude')),
  nombre                   text NOT NULL,
  estado                   text NOT NULL DEFAULT 'desconectada'
                           CHECK (estado IN ('conectada', 'desconectada', 'error')),
  detalle                  text,
  cuenta                   text,
  permisos                 text[] NOT NULL DEFAULT '{}',
  credenciales_cifradas    bytea,
  credenciales_expiran_en  timestamptz,
  cursor_sincronizacion    jsonb,          -- p. ej. el historyId de Gmail
  ultima_sincronizacion    timestamptz,
  actualizado_en           timestamptz NOT NULL DEFAULT now(),
  CHECK (estado <> 'conectada' OR credenciales_cifradas IS NOT NULL OR id = 'formulario')
);


-- =====================================================================
-- GRUPO 2 · EMPRESAS Y CONTACTOS
-- =====================================================================

CREATE TABLE empresas (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  razon_social     text NOT NULL CHECK (length(trim(razon_social)) > 0),
  nit              text,
  sector           text NOT NULL DEFAULT 'Otro',
  tamano           text NOT NULL DEFAULT 'desconocido'
                   CHECK (tamano IN ('1-10', '11-50', '51-200', '200+', 'desconocido')),
  municipio        text NOT NULL,
  sitio_web        text,
  dominio          citext,
  estado_cuenta    text NOT NULL DEFAULT 'prospecto' CHECK (estado_cuenta IN ('prospecto', 'cliente')),
  fusionada_en_id  uuid REFERENCES empresas(id),   -- si se fusionó, apunta a la empresa principal
  creado_en        timestamptz NOT NULL DEFAULT now(),
  actualizado_en   timestamptz NOT NULL DEFAULT now(),
  CHECK (fusionada_en_id IS NULL OR fusionada_en_id <> id),
  CHECK (dominio IS NULL OR dominio::text !~ '[/@\s]')
);
CREATE UNIQUE INDEX empresas_nit_uq ON empresas (nit) WHERE nit IS NOT NULL AND fusionada_en_id IS NULL;
CREATE UNIQUE INDEX empresas_dominio_uq ON empresas (dominio) WHERE dominio IS NOT NULL AND fusionada_en_id IS NULL;
CREATE INDEX empresas_municipio_sector_idx ON empresas (municipio, sector);

CREATE TABLE contactos (
  id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id        uuid NOT NULL REFERENCES empresas(id),
  nombre            text NOT NULL,
  cargo             text,
  correo            citext,
  estado_correo     text NOT NULL DEFAULT 'desconocido'
                    CHECK (estado_correo IN ('valido', 'riesgoso', 'invalido', 'desconocido', 'catch_all')),
  telefono          text,
  etapa             text NOT NULL DEFAULT 'identificado'
                    CHECK (etapa IN ('identificado', 'contactable', 'contactado', 'interesado', 'lead',
                                     'calificado', 'discovery', 'propuesta', 'cliente', 'postventa',
                                     'excluido', 'descartado', 'perdido')),
  origen            text NOT NULL
                    CHECK (origen IN ('organico_busqueda', 'organico_redes', 'pagado', 'correo', 'referencia',
                                      'directo_desconocido', 'manual', 'registro', 'descubrimiento')),
  excluido          boolean NOT NULL DEFAULT false,
  motivo_exclusion  text,
  excluido_en       timestamptz,
  anonimizado_en    timestamptz,
  creado_en         timestamptz NOT NULL DEFAULT now(),
  actualizado_en    timestamptz NOT NULL DEFAULT now(),
  CHECK (NOT excluido OR (length(trim(coalesce(motivo_exclusion, ''))) >= 3 AND excluido_en IS NOT NULL)),
  CHECK (correo IS NULL OR correo::text ~ '^[^@\s]+@[^@\s]+\.[^@\s]+$')
);
CREATE UNIQUE INDEX contactos_correo_uq ON contactos (correo) WHERE correo IS NOT NULL;
CREATE INDEX contactos_empresa_idx ON contactos (empresa_id);
CREATE INDEX contactos_etapa_idx ON contactos (etapa);

-- Historial de cambios de etapa: alimenta el embudo con transiciones reales.
CREATE TABLE cambios_etapa (
  id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  contacto_id     uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  etapa_anterior  text,
  etapa_nueva     text NOT NULL,
  evento          text NOT NULL,     -- p. ej. 'correo_verificado', 'primer_envio', 'respuesta_positiva'
  ocurrido_en     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX cambios_etapa_contacto_idx ON cambios_etapa (contacto_id, ocurrido_en);
CREATE INDEX cambios_etapa_fecha_idx ON cambios_etapa (ocurrido_en);

-- Señales comerciales de una empresa (p. ej. «proveedor de agroindustria»).
CREATE TABLE senales (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id    uuid NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
  tipo          text NOT NULL,
  descripcion   text NOT NULL,
  url           text,
  fuente        text NOT NULL DEFAULT 'agente'
                CHECK (fuente IN ('formulario', 'sitio_web', 'registro', 'manual', 'agente')),
  detectada_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX senales_empresa_idx ON senales (empresa_id);

-- Puntaje de ajuste al cliente ideal. Se guarda cada cálculo; vale el último.
CREATE TABLE puntajes (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id      uuid NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
  total           smallint NOT NULL CHECK (total BETWEEN 0 AND 100),
  incompleto      boolean NOT NULL,
  criterios       jsonb NOT NULL CHECK (jsonb_typeof(criterios) = 'array'),  -- [{nombre, cumple, peso, explicacion}]
  version_reglas  integer NOT NULL,      -- versión de configuracion.cliente_ideal usada
  calculado_en    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX puntajes_empresa_idx ON puntajes (empresa_id, calculado_en DESC);

-- Verificaciones de correo (DNS + verificador externo). Vale la última.
CREATE TABLE verificaciones_correo (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contacto_id    uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  correo         citext NOT NULL,
  resultado      text NOT NULL CHECK (resultado IN ('valido', 'riesgoso', 'invalido', 'desconocido', 'catch_all')),
  proveedor      text NOT NULL,          -- 'dns_mx', 'hunter'...
  detalle        jsonb,
  verificado_en  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX verificaciones_contacto_idx ON verificaciones_correo (contacto_id, verificado_en DESC);

-- Posibles empresas duplicadas. Nunca se fusionan solas.
CREATE TABLE posibles_duplicados (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  principal_id     uuid NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
  duplicado_id     uuid NOT NULL REFERENCES empresas(id) ON DELETE CASCADE,
  motivo           text NOT NULL,
  similitud        numeric(4, 3) CHECK (similitud BETWEEN 0 AND 1),
  estado           text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'fusionado', 'descartado')),
  campos_elegidos  jsonb,                -- {razonSocial: 'principal' | 'duplicado', ...}
  resuelto_por     uuid REFERENCES administradores(id),
  resuelto_en      timestamptz,
  detectado_en     timestamptz NOT NULL DEFAULT now(),
  CHECK (principal_id <> duplicado_id),
  CHECK ((estado = 'pendiente') = (resuelto_en IS NULL)),
  CHECK (estado <> 'fusionado' OR campos_elegidos IS NOT NULL)
);
CREATE UNIQUE INDEX posibles_duplicados_par_uq
  ON posibles_duplicados (LEAST(principal_id, duplicado_id), GREATEST(principal_id, duplicado_id));

-- Correos y dominios que nunca deben recibir mensajes.
CREATE TABLE supresiones (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  correo       citext,
  dominio      citext,
  motivo       text NOT NULL CHECK (length(trim(motivo)) >= 3),
  origen       text NOT NULL CHECK (origen IN ('enlace_baja', 'respuesta', 'rebote_duro', 'queja', 'manual')),
  contacto_id  uuid REFERENCES contactos(id) ON DELETE SET NULL,
  creado_en    timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(correo, dominio) = 1),
  CHECK (dominio IS NULL OR dominio::text !~ '[@/\s]')
);
CREATE UNIQUE INDEX supresiones_correo_uq ON supresiones (correo) WHERE correo IS NOT NULL;
CREATE UNIQUE INDEX supresiones_dominio_uq ON supresiones (dominio) WHERE dominio IS NOT NULL;

-- (La tabla «evidencias» y «bases_legales» pertenecen a este grupo pero se
--  crean más abajo, porque dependen de tablas de otros grupos.)


-- =====================================================================
-- GRUPO 8 (parte 1) · COLA DE TAREAS
-- Se crea aquí porque las ejecuciones de agentes la referencian.
-- =====================================================================

CREATE TABLE tareas (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tipo            text NOT NULL,         -- 'enviar_mensaje', 'sincronizar_gmail', 'ejecutar_agente', ...
  datos           jsonb NOT NULL DEFAULT '{}',
  estado          text NOT NULL DEFAULT 'pendiente'
                  CHECK (estado IN ('pendiente', 'en_proceso', 'completada', 'fallida', 'cancelada')),
  prioridad       smallint NOT NULL DEFAULT 5 CHECK (prioridad BETWEEN 1 AND 9),   -- 1 = más urgente
  ejecutar_desde  timestamptz NOT NULL DEFAULT now(),
  intentos        smallint NOT NULL DEFAULT 0 CHECK (intentos >= 0),
  max_intentos    smallint NOT NULL DEFAULT 3 CHECK (max_intentos > 0),
  tomada_por      text,
  tomada_en       timestamptz,
  ultimo_error    text,
  clave_unica     text UNIQUE,           -- evita encolar dos veces la misma tarea
  creada_en       timestamptz NOT NULL DEFAULT now(),
  terminada_en    timestamptz,
  CHECK (intentos <= max_intentos),
  CHECK (estado NOT IN ('completada', 'fallida', 'cancelada') OR terminada_en IS NOT NULL),
  CHECK (estado <> 'en_proceso' OR (tomada_por IS NOT NULL AND tomada_en IS NOT NULL))
);
CREATE INDEX tareas_pendientes_idx ON tareas (prioridad, ejecutar_desde) WHERE estado = 'pendiente';
CREATE INDEX tareas_en_proceso_idx ON tareas (tomada_en) WHERE estado = 'en_proceso';


-- =====================================================================
-- GRUPO 4 · AGENTES Y APROBACIONES (parte 1)
-- =====================================================================

-- Instrucciones de cada agente, versionadas. Solo una activa por agente.
CREATE TABLE prompts (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agente          text NOT NULL CHECK (agente IN ('investigador', 'segmentador', 'estratega_redactor', 'analista')),
  version         integer NOT NULL CHECK (version > 0),
  modelo          text NOT NULL,
  texto           text NOT NULL,
  esquema_salida  jsonb,                 -- JSON Schema que debe cumplir la respuesta
  activo          boolean NOT NULL DEFAULT false,
  creado_en       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (agente, version)
);
CREATE UNIQUE INDEX prompts_activo_uq ON prompts (agente) WHERE activo;

-- Cada vez que un agente trabaja: qué recibió, qué entregó y cuánto costó.
CREATE TABLE ejecuciones_agente (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agente          text NOT NULL CHECK (agente IN ('investigador', 'segmentador', 'estratega_redactor', 'analista')),
  tarea_id        uuid REFERENCES tareas(id) ON DELETE SET NULL,
  prompt_id       uuid NOT NULL REFERENCES prompts(id),
  modelo          text NOT NULL,
  entrada         jsonb NOT NULL,
  salida          jsonb,
  estado          text NOT NULL DEFAULT 'en_curso'
                  CHECK (estado IN ('en_curso', 'exito', 'error', 'requiere_revision')),
  tokens_entrada  integer CHECK (tokens_entrada >= 0),
  tokens_salida   integer CHECK (tokens_salida >= 0),
  costo_usd       numeric(10, 4) CHECK (costo_usd >= 0),
  error           text,
  iniciada_en     timestamptz NOT NULL DEFAULT now(),
  terminada_en    timestamptz,
  CHECK (terminada_en IS NULL OR terminada_en >= iniciada_en),
  CHECK (estado = 'en_curso' OR terminada_en IS NOT NULL)
);
CREATE INDEX ejecuciones_agente_fecha_idx ON ejecuciones_agente (agente, iniciada_en DESC);


-- =====================================================================
-- GRUPO 3 · CAPTACIÓN
-- =====================================================================

-- Cada envío del formulario del sitio (diagnóstico RSE Express o contacto).
-- Es la prueba de la autorización de datos.
CREATE TABLE envios_formulario (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  id_externo             text NOT NULL UNIQUE,      -- id del envío en el sitio: el webhook no duplica
  formulario             text NOT NULL CHECK (formulario IN ('diagnostico', 'contacto', 'otro')),
  nombre                 text,
  correo                 citext,
  telefono               text,
  cargo                  text,
  empresa_nombre         text,
  municipio              text,
  autorizacion_aceptada  boolean NOT NULL,
  texto_autorizacion     text,
  version_politica       text,
  utm_source             text,
  utm_medium             text,
  utm_campaign           text,
  utm_content            text,
  utm_term               text,
  gclid                  text,
  fbclid                 text,
  referencia_opaca       text,              -- la del enlace del correo, si vino de una campaña
  pagina                 text,
  datos                  jsonb NOT NULL DEFAULT '{}',   -- respuestas completas del diagnóstico
  contacto_id            uuid REFERENCES contactos(id) ON DELETE SET NULL,
  recibido_en            timestamptz NOT NULL DEFAULT now(),
  procesado_en           timestamptz,
  CHECK (NOT autorizacion_aceptada OR length(trim(coalesce(texto_autorizacion, ''))) > 0)
);
CREATE INDEX envios_formulario_referencia_idx ON envios_formulario (referencia_opaca) WHERE referencia_opaca IS NOT NULL;
CREATE INDEX envios_formulario_correo_idx ON envios_formulario (correo);
CREATE INDEX envios_formulario_fecha_idx ON envios_formulario (recibido_en);

-- Autorización (base legal) de cada contacto. Vale la fila vigente.
CREATE TABLE bases_legales (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contacto_id          uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  tipo                 text NOT NULL CHECK (tipo IN ('autorizacion_formulario', 'autorizacion_registrada',
                                                     'corporativo_generico', 'pendiente')),
  texto                text,
  prueba               text,
  envio_formulario_id  uuid REFERENCES envios_formulario(id),
  obtenida_en          timestamptz,
  aprobada_por         uuid REFERENCES administradores(id),
  aprobada_en          timestamptz,
  vigente              boolean NOT NULL DEFAULT true,
  revocada_en          timestamptz,
  motivo_revocacion    text,
  creado_en            timestamptz NOT NULL DEFAULT now(),
  CHECK ((aprobada_en IS NULL) = (aprobada_por IS NULL)),
  -- Solo se aprueba con tipo, texto y prueba completos.
  CHECK (aprobada_en IS NULL OR (tipo <> 'pendiente'
         AND length(trim(coalesce(texto, ''))) > 0
         AND length(trim(coalesce(prueba, ''))) > 0)),
  CHECK (tipo <> 'autorizacion_formulario' OR envio_formulario_id IS NOT NULL),
  CHECK (vigente OR revocada_en IS NOT NULL)
);
CREATE UNIQUE INDEX bases_legales_vigente_uq ON bases_legales (contacto_id) WHERE vigente;

CREATE TABLE busquedas_descubrimiento (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  consulta             text NOT NULL,
  municipios           text[] NOT NULL DEFAULT '{}',
  sectores             text[] NOT NULL DEFAULT '{}',
  tamano               text,
  estado               text NOT NULL DEFAULT 'en_cola' CHECK (estado IN ('en_cola', 'ejecutando', 'terminada', 'error')),
  progreso             smallint NOT NULL DEFAULT 0 CHECK (progreso BETWEEN 0 AND 100),
  error                text,
  ejecucion_agente_id  uuid REFERENCES ejecuciones_agente(id) ON DELETE SET NULL,
  creada_por           uuid REFERENCES administradores(id),
  creada_en            timestamptz NOT NULL DEFAULT now(),
  terminada_en         timestamptz,
  CHECK (estado <> 'error' OR error IS NOT NULL)
);

CREATE TABLE resultados_descubrimiento (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  busqueda_id           uuid NOT NULL REFERENCES busquedas_descubrimiento(id) ON DELETE CASCADE,
  razon_social          text NOT NULL,
  sector                text,
  municipio             text,
  sitio_web             text,
  dominio               citext,
  correo_corporativo    citext,
  telefono              text,
  whatsapp              text,
  formulario_url        text,
  posible_duplicado_de  uuid REFERENCES empresas(id) ON DELETE SET NULL,
  estado                text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aceptado', 'descartado')),
  empresa_creada_id     uuid REFERENCES empresas(id),
  decidido_por          uuid REFERENCES administradores(id),
  decidido_en           timestamptz,
  creado_en             timestamptz NOT NULL DEFAULT now(),
  CHECK ((estado = 'aceptado') = (empresa_creada_id IS NOT NULL)),
  CHECK ((estado = 'pendiente') = (decidido_en IS NULL)),
  CHECK (estado <> 'aceptado' OR posible_duplicado_de IS NULL)     -- un posible duplicado no se acepta
);
CREATE INDEX resultados_busqueda_idx ON resultados_descubrimiento (busqueda_id);

-- Personas que el agente encontró en sitios públicos. Nunca pasan solas a campañas.
CREATE TABLE personas_descubiertas (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  resultado_id         uuid NOT NULL REFERENCES resultados_descubrimiento(id) ON DELETE CASCADE,
  nombre               text NOT NULL,
  cargo                text,
  correo               citext,
  fuente_url           text NOT NULL,
  requiere_base_legal  boolean NOT NULL DEFAULT true CHECK (requiere_base_legal),
  encontrada_en        timestamptz NOT NULL DEFAULT now()
);


-- =====================================================================
-- GRUPO 5 · CAMPAÑAS Y ENVÍOS (parte 1)
-- =====================================================================

CREATE TABLE segmentos (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre          text NOT NULL,
  reglas          jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(reglas) = 'array'),  -- [{campo, operador, valor}]
  justificacion   text NOT NULL DEFAULT '',
  version         integer NOT NULL DEFAULT 1 CHECK (version > 0),
  estado          text NOT NULL DEFAULT 'propuesto' CHECK (estado IN ('propuesto', 'aprobado', 'archivado')),
  propuesto_por   text NOT NULL CHECK (propuesto_por IN ('admin', 'agente')),
  aprobado_por    uuid REFERENCES administradores(id),
  aprobado_en     timestamptz,
  creado_en       timestamptz NOT NULL DEFAULT now(),
  actualizado_en  timestamptz NOT NULL DEFAULT now(),
  CHECK (estado <> 'aprobado' OR aprobado_en IS NOT NULL)
);

CREATE TABLE secuencias (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre          text NOT NULL,
  objetivo        text NOT NULL,
  llamada_accion  text NOT NULL,
  metricas_exito  text[] NOT NULL DEFAULT '{}',
  ventana_dias    smallint[] NOT NULL DEFAULT '{1,2,3,4,5}',   -- 0 = domingo ... 6 = sábado (igual que el frontend)
  ventana_desde   time NOT NULL DEFAULT '08:00',
  ventana_hasta   time NOT NULL DEFAULT '17:00',
  version         integer NOT NULL DEFAULT 1 CHECK (version > 0),
  creado_en       timestamptz NOT NULL DEFAULT now(),
  actualizado_en  timestamptz NOT NULL DEFAULT now(),
  CHECK (cardinality(ventana_dias) > 0 AND ventana_dias <@ '{0,1,2,3,4,5,6}'::smallint[]),
  CHECK (ventana_desde < ventana_hasta)
);

-- Pasos de una secuencia. La base exige asunto en el primero y
-- {{firma}} y {{baja}} en todos.
CREATE TABLE pasos (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  secuencia_id         uuid NOT NULL REFERENCES secuencias(id) ON DELETE CASCADE,
  orden                smallint NOT NULL CHECK (orden > 0),
  asunto               text,
  cuerpo               text NOT NULL,
  espera_dias_habiles  smallint NOT NULL DEFAULT 0 CHECK (espera_dias_habiles >= 0),
  mismo_hilo           boolean NOT NULL DEFAULT true,
  variables            jsonb NOT NULL DEFAULT '[]' CHECK (jsonb_typeof(variables) = 'array'),  -- [{nombre, campoFuente, respaldo}]
  CONSTRAINT pasos_orden_uq UNIQUE (secuencia_id, orden) DEFERRABLE INITIALLY DEFERRED,
  CHECK (char_length(cuerpo) BETWEEN 30 AND 3000),
  CHECK (position('{{baja}}' IN cuerpo) > 0 AND position('{{firma}}' IN cuerpo) > 0),
  CHECK (orden <> 1 OR length(trim(coalesce(asunto, ''))) > 0)
);

CREATE TABLE campanas (
  id                     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  nombre                 text NOT NULL,
  segmento_id            uuid NOT NULL REFERENCES segmentos(id),
  secuencia_id           uuid NOT NULL REFERENCES secuencias(id),
  estado                 text NOT NULL DEFAULT 'borrador'
                         CHECK (estado IN ('borrador', 'en_revision', 'aprobada', 'activa', 'pausada', 'finalizada', 'rechazada')),
  tope_diario            smallint NOT NULL DEFAULT 20 CHECK (tope_diario > 0),
  aprobacion_vigente_id  uuid,          -- clave foránea al final del grupo
  creado_en              timestamptz NOT NULL DEFAULT now(),
  actualizado_en         timestamptz NOT NULL DEFAULT now(),
  CHECK (estado NOT IN ('aprobada', 'activa') OR aprobacion_vigente_id IS NOT NULL)
);
CREATE INDEX campanas_segmento_idx ON campanas (segmento_id);
CREATE INDEX campanas_secuencia_idx ON campanas (secuencia_id);


-- =====================================================================
-- GRUPO 4 · AGENTES Y APROBACIONES (parte 2)
-- =====================================================================

CREATE TABLE propuestas (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  agente                text NOT NULL CHECK (agente IN ('investigador', 'segmentador', 'estratega_redactor', 'analista')),
  tipo                  text NOT NULL CHECK (tipo IN ('enriquecimiento', 'segmento', 'campana', 'mejora', 'correo_inferido')),
  estado                text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aprobada', 'editada', 'rechazada')),
  resumen               text NOT NULL,
  version_actual        integer NOT NULL DEFAULT 1 CHECK (version_actual > 0),
  cambio_sustancial     boolean NOT NULL DEFAULT false,
  objetivo_campana_id   uuid REFERENCES campanas(id) ON DELETE CASCADE,
  objetivo_segmento_id  uuid REFERENCES segmentos(id) ON DELETE CASCADE,
  objetivo_empresa_id   uuid REFERENCES empresas(id) ON DELETE CASCADE,
  objetivo_contacto_id  uuid REFERENCES contactos(id) ON DELETE CASCADE,
  ejecucion_agente_id   uuid REFERENCES ejecuciones_agente(id) ON DELETE SET NULL,
  creado_en             timestamptz NOT NULL DEFAULT now(),
  actualizado_en        timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(objetivo_campana_id, objetivo_segmento_id, objetivo_empresa_id, objetivo_contacto_id) <= 1),
  CHECK (tipo <> 'campana' OR objetivo_campana_id IS NOT NULL),
  CHECK (tipo <> 'segmento' OR objetivo_segmento_id IS NOT NULL),
  CHECK (tipo <> 'correo_inferido' OR estado <> 'aprobada')    -- un correo inferido nunca se aprueba
);
CREATE INDEX propuestas_estado_idx ON propuestas (estado, creado_en DESC);

-- Cada versión de una propuesta. El contenido nunca se edita: se crea otra versión.
CREATE TABLE propuesta_versiones (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  propuesta_id  uuid NOT NULL REFERENCES propuestas(id) ON DELETE CASCADE,
  version       integer NOT NULL CHECK (version > 0),
  contenido     jsonb NOT NULL,
  autor         text NOT NULL CHECK (autor IN ('agente', 'admin')),
  creada_en     timestamptz NOT NULL DEFAULT now(),
  decision      text CHECK (decision IN ('aprobada', 'rechazada')),
  comentario    text,
  decidida_por  uuid REFERENCES administradores(id),
  decidida_en   timestamptz,
  UNIQUE (propuesta_id, version),
  CHECK ((decision IS NULL) = (decidida_en IS NULL)),
  CHECK (decision IS NULL OR decidida_por IS NOT NULL),
  CHECK (decision <> 'rechazada' OR length(trim(coalesce(comentario, ''))) >= 3)   -- rechazar exige motivo
);

-- Aprobación de una campaña: guarda una copia congelada de lo aprobado.
-- El motor envía desde esta copia, nunca desde la versión en edición.
CREATE TABLE aprobaciones_campana (
  id                    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campana_id            uuid NOT NULL REFERENCES campanas(id) ON DELETE CASCADE,
  propuesta_version_id  uuid NOT NULL REFERENCES propuesta_versiones(id),
  version_secuencia     integer NOT NULL,
  version_segmento      integer NOT NULL,
  version_marca         integer NOT NULL,
  contenido_congelado   jsonb NOT NULL,      -- {secuencia, pasos, reglas del segmento, firma, baja}
  huella                text NOT NULL,       -- SHA-256 del contenido congelado; la calcula un disparador
  aprobada_por          uuid NOT NULL REFERENCES administradores(id),
  aprobada_en           timestamptz NOT NULL DEFAULT now(),
  revocada_en           timestamptz,
  motivo_revocacion     text,
  CHECK ((revocada_en IS NULL) = (motivo_revocacion IS NULL)),
  UNIQUE (id, campana_id)
);

-- Calcula la huella al crear la aprobación y no deja cambiar lo aprobado:
-- después de creada solo se puede revocar (una vez).
CREATE FUNCTION fn_aprobacion_congelada() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    NEW.huella := encode(sha256(convert_to(NEW.contenido_congelado::text, 'UTF8')), 'hex');
    RETURN NEW;
  END IF;
  IF (NEW.campana_id, NEW.propuesta_version_id, NEW.version_secuencia, NEW.version_segmento, NEW.version_marca,
      NEW.contenido_congelado, NEW.huella, NEW.aprobada_por, NEW.aprobada_en)
     IS DISTINCT FROM
     (OLD.campana_id, OLD.propuesta_version_id, OLD.version_secuencia, OLD.version_segmento, OLD.version_marca,
      OLD.contenido_congelado, OLD.huella, OLD.aprobada_por, OLD.aprobada_en) THEN
    RAISE EXCEPTION 'Una aprobación no se modifica: revóquela y apruebe de nuevo' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.revocada_en IS NOT NULL AND (NEW.revocada_en, NEW.motivo_revocacion)
     IS DISTINCT FROM (OLD.revocada_en, OLD.motivo_revocacion) THEN
    RAISE EXCEPTION 'Esta aprobación ya fue revocada' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER aprobacion_congelada BEFORE INSERT OR UPDATE ON aprobaciones_campana
  FOR EACH ROW EXECUTE FUNCTION fn_aprobacion_congelada();

-- La aprobación vigente de una campaña debe ser de esa misma campaña.
ALTER TABLE campanas
  ADD CONSTRAINT campanas_aprobacion_vigente_fk
  FOREIGN KEY (aprobacion_vigente_id, id) REFERENCES aprobaciones_campana (id, campana_id)
  DEFERRABLE INITIALLY DEFERRED;


-- =====================================================================
-- GRUPO 2 · EMPRESAS Y CONTACTOS (parte 2): EVIDENCIAS
-- =====================================================================

-- Procedencia de cada dato. Pertenece a exactamente una cosa.
CREATE TABLE evidencias (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id          uuid REFERENCES empresas(id) ON DELETE CASCADE,
  contacto_id         uuid REFERENCES contactos(id) ON DELETE CASCADE,
  resultado_id        uuid REFERENCES resultados_descubrimiento(id) ON DELETE CASCADE,
  propuesta_id        uuid REFERENCES propuestas(id) ON DELETE CASCADE,
  campo               text NOT NULL,
  valor               text,
  fuente              text NOT NULL CHECK (fuente IN ('formulario', 'sitio_web', 'registro', 'manual', 'agente')),
  url                 text,
  cita                text,              -- texto exacto encontrado en la fuente
  metodo              text,
  estado              text NOT NULL CHECK (estado IN ('verificado', 'inferido', 'pendiente')),
  cita_verificada_en  timestamptz,       -- cuándo un proceso sin IA confirmó que la cita existe
  obtenido_en         timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(empresa_id, contacto_id, resultado_id, propuesta_id) = 1),
  -- Un dato traído por un agente solo es «verificado» si su cita fue comprobada.
  CHECK (fuente <> 'agente' OR estado <> 'verificado'
         OR (url IS NOT NULL AND cita IS NOT NULL AND cita_verificada_en IS NOT NULL))
);
CREATE INDEX evidencias_empresa_idx ON evidencias (empresa_id) WHERE empresa_id IS NOT NULL;
CREATE INDEX evidencias_contacto_idx ON evidencias (contacto_id) WHERE contacto_id IS NOT NULL;
CREATE INDEX evidencias_resultado_idx ON evidencias (resultado_id) WHERE resultado_id IS NOT NULL;
CREATE INDEX evidencias_propuesta_idx ON evidencias (propuesta_id) WHERE propuesta_id IS NOT NULL;


-- =====================================================================
-- GRUPO 5 · CAMPAÑAS Y ENVÍOS (parte 2)
-- =====================================================================

CREATE TABLE inscripciones (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  campana_id      uuid NOT NULL REFERENCES campanas(id) ON DELETE CASCADE,
  contacto_id     uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  paso_actual     smallint NOT NULL DEFAULT 1 CHECK (paso_actual > 0),
  proximo_envio   timestamptz,
  estado          text NOT NULL DEFAULT 'pausada' CHECK (estado IN ('activa', 'pausada', 'finalizada')),
  motivo_salida   text CHECK (motivo_salida IN ('respondio', 'baja', 'rebote', 'reunion', 'cliente', 'completada', 'manual')),
  inscrita_en     timestamptz NOT NULL DEFAULT now(),
  finalizada_en   timestamptz,
  actualizado_en  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (campana_id, contacto_id),
  CHECK ((estado = 'finalizada') = (finalizada_en IS NOT NULL)),
  CHECK (estado <> 'finalizada' OR motivo_salida IS NOT NULL),
  CHECK (estado = 'activa' OR proximo_envio IS NULL)
);
CREATE INDEX inscripciones_contacto_idx ON inscripciones (contacto_id);
CREATE INDEX inscripciones_proximas_idx ON inscripciones (proximo_envio) WHERE estado = 'activa';

-- Cada correo que sale (de una secuencia o una respuesta escrita por el administrador).
CREATE TABLE mensajes (
  id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tipo                text NOT NULL CHECK (tipo IN ('secuencia', 'respuesta_manual')),
  contacto_id         uuid NOT NULL REFERENCES contactos(id),
  inscripcion_id      uuid REFERENCES inscripciones(id),
  aprobacion_id       uuid REFERENCES aprobaciones_campana(id),
  paso_orden          smallint,
  en_respuesta_a      uuid,             -- respuesta que se contesta (clave foránea más abajo)
  clave_idempotencia  text NOT NULL UNIQUE,   -- también va en un encabezado del correo
  destinatario        citext NOT NULL,
  asunto              text,
  cuerpo              text NOT NULL,
  estado              text NOT NULL DEFAULT 'programado'
                      CHECK (estado IN ('programado', 'enviando', 'enviado', 'fallido', 'cancelado')),
  programado_para     timestamptz NOT NULL DEFAULT now(),
  enviado_en          timestamptz,
  intentos            smallint NOT NULL DEFAULT 0 CHECK (intentos >= 0),
  ultimo_error        text,
  gmail_message_id    text UNIQUE,
  gmail_thread_id     text,
  creado_en           timestamptz NOT NULL DEFAULT now(),
  CHECK (tipo <> 'secuencia' OR (inscripcion_id IS NOT NULL AND aprobacion_id IS NOT NULL AND paso_orden IS NOT NULL)),
  CHECK (tipo <> 'respuesta_manual' OR en_respuesta_a IS NOT NULL),
  CHECK ((estado = 'enviado') = (enviado_en IS NOT NULL AND gmail_message_id IS NOT NULL))
);
-- Un mismo paso nunca se envía dos veces a la misma inscripción.
CREATE UNIQUE INDEX mensajes_paso_uq ON mensajes (inscripcion_id, paso_orden)
  WHERE tipo = 'secuencia' AND estado <> 'cancelado';
CREATE INDEX mensajes_programados_idx ON mensajes (programado_para) WHERE estado = 'programado';
CREATE INDEX mensajes_contacto_idx ON mensajes (contacto_id);
CREATE INDEX mensajes_hilo_idx ON mensajes (gmail_thread_id);

-- Enlaces con referencia opaca (sin datos personales) para medir clics y diagnósticos.
CREATE TABLE enlaces_seguimiento (
  referencia    text PRIMARY KEY CHECK (char_length(referencia) >= 16),
  mensaje_id    uuid NOT NULL REFERENCES mensajes(id) ON DELETE CASCADE,
  url_destino   text NOT NULL,
  utm_source    text NOT NULL DEFAULT 'nexus-pm',
  utm_medium    text NOT NULL DEFAULT 'email',
  utm_campaign  text,
  utm_content   text,
  creado_en     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX enlaces_mensaje_idx ON enlaces_seguimiento (mensaje_id);

-- Correos que llegan: respuestas de los contactos.
CREATE TABLE respuestas (
  id                        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contacto_id               uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  campana_id                uuid REFERENCES campanas(id) ON DELETE SET NULL,
  inscripcion_id            uuid REFERENCES inscripciones(id) ON DELETE SET NULL,
  mensaje_origen_id         uuid REFERENCES mensajes(id) ON DELETE SET NULL,
  gmail_message_id          text NOT NULL UNIQUE,
  gmail_thread_id           text NOT NULL,
  remitente                 citext NOT NULL,
  recibida_en               timestamptz NOT NULL,
  asunto                    text,
  texto                     text NOT NULL,
  clasificacion             text CHECK (clasificacion IN ('interesado', 'pide_informacion', 'quiere_reunion', 'objecion',
                                                          'ahora_no', 'no_interesado', 'baja', 'fuera_oficina',
                                                          'remite_a_otra_persona', 'necesita_revision')),
  confianza                 numeric(3, 2) CHECK (confianza BETWEEN 0 AND 1),
  clasificacion_confirmada  boolean NOT NULL DEFAULT false,
  borrador                  text,
  estado                    text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'atendida')),
  atendida_en               timestamptz,
  ejecucion_agente_id       uuid REFERENCES ejecuciones_agente(id) ON DELETE SET NULL,
  creado_en                 timestamptz NOT NULL DEFAULT now(),
  CHECK ((estado = 'atendida') = (atendida_en IS NOT NULL))
);
CREATE INDEX respuestas_pendientes_idx ON respuestas (recibida_en DESC) WHERE estado = 'pendiente';
CREATE INDEX respuestas_hilo_idx ON respuestas (gmail_thread_id);
CREATE INDEX respuestas_contacto_idx ON respuestas (contacto_id);

ALTER TABLE mensajes
  ADD CONSTRAINT mensajes_en_respuesta_a_fk FOREIGN KEY (en_respuesta_a) REFERENCES respuestas(id);

-- Rebotes, clics, aperturas, bajas y quejas.
CREATE TABLE eventos_correo (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  tipo         text NOT NULL CHECK (tipo IN ('rebote_duro', 'rebote_suave', 'clic', 'apertura', 'baja', 'queja', 'fuera_oficina')),
  mensaje_id   uuid REFERENCES mensajes(id) ON DELETE SET NULL,
  contacto_id  uuid REFERENCES contactos(id) ON DELETE SET NULL,
  referencia   text REFERENCES enlaces_seguimiento(referencia) ON DELETE SET NULL,
  automatico   boolean NOT NULL DEFAULT false,   -- clic hecho por un escáner de seguridad
  id_externo   text UNIQUE,                      -- evita registrar dos veces el mismo aviso
  datos        jsonb,
  ocurrido_en  timestamptz NOT NULL DEFAULT now(),
  CHECK (num_nonnulls(mensaje_id, contacto_id) >= 1)
);
CREATE INDEX eventos_mensaje_idx ON eventos_correo (mensaje_id, tipo);
CREATE INDEX eventos_fecha_idx ON eventos_correo (ocurrido_en);


-- =====================================================================
-- GRUPO 6 · COMERCIAL
-- =====================================================================

CREATE TABLE oportunidades (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  empresa_id           uuid NOT NULL REFERENCES empresas(id),
  contacto_id          uuid REFERENCES contactos(id) ON DELETE SET NULL,
  etapa                text NOT NULL DEFAULT 'discovery' CHECK (etapa IN ('discovery', 'propuesta', 'cliente', 'perdido')),
  reto                 text,
  valor_estimado_cop   numeric(14, 0) CHECK (valor_estimado_cop >= 0),
  campana_origen_id    uuid REFERENCES campanas(id) ON DELETE SET NULL,
  respuesta_origen_id  uuid REFERENCES respuestas(id) ON DELETE SET NULL,
  motivo_perdida       text,
  creado_en            timestamptz NOT NULL DEFAULT now(),
  actualizado_en       timestamptz NOT NULL DEFAULT now(),
  cerrada_en           timestamptz,
  CHECK ((etapa IN ('cliente', 'perdido')) = (cerrada_en IS NOT NULL))
);
CREATE INDEX oportunidades_empresa_idx ON oportunidades (empresa_id);

-- Interacciones registradas a mano o por el sitio (visitas, formularios,
-- llamadas, reuniones, notas). Correos, respuestas y clics salen de sus tablas.
-- (Pertenece al grupo 2; se crea aquí porque puede apuntar a una oportunidad.)
CREATE TABLE interacciones (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  contacto_id     uuid NOT NULL REFERENCES contactos(id) ON DELETE CASCADE,
  tipo            text NOT NULL CHECK (tipo IN ('visita', 'formulario', 'llamada', 'reunion', 'nota')),
  resumen         text NOT NULL,
  origen          text CHECK (origen IN ('organico_busqueda', 'organico_redes', 'pagado', 'correo', 'referencia',
                                         'directo_desconocido', 'manual', 'registro', 'descubrimiento')),
  oportunidad_id  uuid REFERENCES oportunidades(id) ON DELETE SET NULL,
  registrada_por  uuid REFERENCES administradores(id),
  ocurrida_en     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX interacciones_contacto_idx ON interacciones (contacto_id, ocurrida_en DESC);


-- =====================================================================
-- GRUPO 7 · MEDICIÓN
-- =====================================================================

-- Visitas del sitio por día y canal, importadas de GA4. Agregadas: sin personas.
CREATE TABLE metricas_web_diarias (
  fecha           date NOT NULL,
  canal           text NOT NULL CHECK (canal IN ('organico_busqueda', 'organico_redes', 'pagado', 'correo',
                                                 'referencia', 'directo_desconocido')),
  fuente          text NOT NULL DEFAULT '',
  medio           text NOT NULL DEFAULT '',
  campana_utm     text NOT NULL DEFAULT '',
  sesiones        integer NOT NULL DEFAULT 0 CHECK (sesiones >= 0),
  usuarios        integer NOT NULL DEFAULT 0 CHECK (usuarios >= 0),
  leads           integer NOT NULL DEFAULT 0 CHECK (leads >= 0),            -- evento generate_lead
  clics_whatsapp  integer NOT NULL DEFAULT 0 CHECK (clics_whatsapp >= 0),   -- evento whatsapp_click
  importado_en    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (fecha, canal, fuente, medio, campana_utm)
);

-- Búsquedas en Google que llevaron al sitio, importadas de Search Console.
CREATE TABLE consultas_busqueda_diarias (
  fecha         date NOT NULL,
  consulta      text NOT NULL,
  pagina        text NOT NULL,
  clics         integer NOT NULL DEFAULT 0 CHECK (clics >= 0),
  impresiones   integer NOT NULL DEFAULT 0 CHECK (impresiones >= 0),
  posicion      numeric(6, 2),
  importado_en  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (fecha, consulta, pagina)
);

-- Costos del periodo. Cada monto guarda su moneda.
CREATE TABLE costos (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  fecha                date NOT NULL,
  categoria            text NOT NULL CHECK (categoria IN ('workspace', 'alojamiento', 'dominio', 'api_claude',
                                                          'verificador', 'busqueda', 'otro')),
  concepto             text NOT NULL,
  monto                numeric(14, 2) NOT NULL CHECK (monto >= 0),
  moneda               char(3) NOT NULL CHECK (moneda IN ('USD', 'COP')),
  campana_id           uuid REFERENCES campanas(id) ON DELETE SET NULL,
  ejecucion_agente_id  uuid REFERENCES ejecuciones_agente(id) ON DELETE SET NULL,
  registrado_por       uuid REFERENCES administradores(id),
  creado_en            timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX costos_fecha_idx ON costos (fecha);

-- Recomendaciones del agente Analista. Aprobar una crea una propuesta.
CREATE TABLE recomendaciones (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  texto                text NOT NULL,
  sustento             jsonb NOT NULL DEFAULT '{}',   -- cifras en las que se basa
  periodo_desde        date,
  periodo_hasta        date,
  estado               text NOT NULL DEFAULT 'pendiente' CHECK (estado IN ('pendiente', 'aprobada', 'descartada')),
  propuesta_id         uuid REFERENCES propuestas(id) ON DELETE SET NULL,
  ejecucion_agente_id  uuid REFERENCES ejecuciones_agente(id) ON DELETE SET NULL,
  creada_en            timestamptz NOT NULL DEFAULT now(),
  decidida_en          timestamptz,
  CHECK ((estado = 'pendiente') = (decidida_en IS NULL)),
  CHECK (periodo_desde IS NULL OR periodo_hasta IS NULL OR periodo_desde <= periodo_hasta)
);


-- =====================================================================
-- GRUPO 8 (parte 2) · AUDITORÍA
-- =====================================================================

-- Registro de todo lo que cambia. Solo se puede agregar: nunca editar ni borrar.
CREATE TABLE registro_auditoria (
  id                   bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  fecha                timestamptz NOT NULL DEFAULT now(),
  actor                text NOT NULL,     -- 'admin', 'agente:investigador', 'proceso:motor_secuencias', 'sistema'
  administrador_id     uuid REFERENCES administradores(id),
  accion               text NOT NULL,
  entidad              text NOT NULL,
  entidad_id           text NOT NULL,
  antes                jsonb,
  despues              jsonb,
  ejecucion_agente_id  uuid REFERENCES ejecuciones_agente(id),
  costo_usd            numeric(10, 4)
);
CREATE INDEX auditoria_entidad_idx ON registro_auditoria (entidad, entidad_id);
CREATE INDEX auditoria_fecha_idx ON registro_auditoria (fecha DESC);
CREATE INDEX auditoria_actor_idx ON registro_auditoria (actor);


-- =====================================================================
-- FUNCIONES DE NEGOCIO
-- =====================================================================

-- Por qué un contacto NO puede entrar a una campaña (NULL = sí puede).
-- Es la misma regla de src/lib/reglas.ts (motivoInscripcion).
CREATE FUNCTION motivo_no_elegible(p_contacto uuid) RETURNS text
LANGUAGE sql STABLE AS $$
  SELECT CASE
    WHEN c.id IS NULL THEN 'Contacto no encontrado'
    WHEN c.anonimizado_en IS NOT NULL THEN 'Contacto anonimizado'
    WHEN c.excluido OR c.etapa = 'excluido' THEN 'Contacto excluido'
    WHEN EXISTS (
      SELECT 1 FROM nexus.supresiones s
      WHERE s.correo = c.correo
         OR s.dominio = split_part(c.correo::text, '@', 2)::citext
    ) THEN 'Correo o dominio suprimido'
    WHEN NOT EXISTS (
      SELECT 1 FROM nexus.bases_legales b
      WHERE b.contacto_id = c.id AND b.vigente AND b.aprobada_en IS NOT NULL AND b.tipo <> 'pendiente'
    ) THEN 'Base legal sin aprobar'
    WHEN c.correo IS NULL OR c.estado_correo <> 'valido' THEN 'Se requiere un correo válido'
    WHEN c.etapa IN ('cliente', 'postventa') OR e.estado_cuenta = 'cliente' THEN 'La empresa ya es cliente'
  END
  FROM (SELECT p_contacto AS id) x
  LEFT JOIN nexus.contactos c ON c.id = x.id
  LEFT JOIN nexus.empresas e ON e.id = c.empresa_id
$$;

-- ¿Está suprimido o excluido? (para respuestas escritas a mano)
CREATE FUNCTION contacto_bloqueado(p_contacto uuid) RETURNS boolean
LANGUAGE sql STABLE AS $$
  SELECT c.excluido OR c.anonimizado_en IS NOT NULL OR EXISTS (
    SELECT 1 FROM nexus.supresiones s
    WHERE s.correo = c.correo OR s.dominio = split_part(c.correo::text, '@', 2)::citext
  )
  FROM nexus.contactos c WHERE c.id = p_contacto
$$;

-- Detiene envíos: finaliza o pausa inscripciones y cancela mensajes programados.
CREATE FUNCTION detener_envios(p_contactos uuid[], p_motivo text, p_finalizar boolean) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE nexus.mensajes m SET estado = 'cancelado'
   WHERE m.contacto_id = ANY (p_contactos) AND m.estado = 'programado' AND m.tipo = 'secuencia';

  IF p_finalizar THEN
    UPDATE nexus.inscripciones i
       SET estado = 'finalizada', motivo_salida = p_motivo, proximo_envio = NULL, finalizada_en = now()
     WHERE i.contacto_id = ANY (p_contactos) AND i.estado <> 'finalizada';
  ELSE
    UPDATE nexus.inscripciones i
       SET estado = 'pausada', motivo_salida = p_motivo, proximo_envio = NULL
     WHERE i.contacto_id = ANY (p_contactos) AND i.estado = 'activa';
  END IF;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Revoca la aprobación de campañas cuyo contenido cambió: vuelven a revisión,
-- sus inscripciones se pausan y los mensajes programados se cancelan.
CREATE FUNCTION revocar_aprobaciones(p_campanas uuid[], p_motivo text) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE nexus.aprobaciones_campana a
     SET revocada_en = now(), motivo_revocacion = p_motivo
    FROM nexus.campanas c
   WHERE c.id = ANY (p_campanas) AND a.id = c.aprobacion_vigente_id AND a.revocada_en IS NULL;

  UPDATE nexus.campanas c
     SET estado = 'en_revision', aprobacion_vigente_id = NULL
   WHERE c.id = ANY (p_campanas) AND c.estado NOT IN ('finalizada', 'rechazada', 'borrador');
  GET DIAGNOSTICS n = ROW_COUNT;

  UPDATE nexus.mensajes m SET estado = 'cancelado'
    FROM nexus.inscripciones i
   WHERE m.inscripcion_id = i.id AND i.campana_id = ANY (p_campanas) AND m.estado = 'programado';

  UPDATE nexus.inscripciones i
     SET estado = 'pausada', proximo_envio = NULL
   WHERE i.campana_id = ANY (p_campanas) AND i.estado = 'activa';

  RETURN n;
END $$;

-- Toma tareas de la cola sin que dos trabajadores tomen la misma.
CREATE FUNCTION tomar_tareas(p_trabajador text, p_limite integer DEFAULT 10, p_tipos text[] DEFAULT NULL)
RETURNS SETOF tareas
LANGUAGE sql AS $$
  UPDATE nexus.tareas t
     SET estado = 'en_proceso', tomada_por = p_trabajador, tomada_en = now(), intentos = t.intentos + 1
   WHERE t.id IN (
     SELECT id FROM nexus.tareas
      WHERE estado = 'pendiente' AND ejecutar_desde <= now()
        AND (p_tipos IS NULL OR tipo = ANY (p_tipos))
      ORDER BY prioridad, ejecutar_desde
      LIMIT p_limite
      FOR UPDATE SKIP LOCKED
   )
  RETURNING t.*
$$;

-- Marca una tarea como completada o fallida; si quedan intentos, la reprograma
-- con espera creciente (1, 5 y 30 minutos).
CREATE FUNCTION terminar_tarea(p_tarea uuid, p_exito boolean, p_error text DEFAULT NULL) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  t nexus.tareas;
BEGIN
  SELECT * INTO t FROM nexus.tareas WHERE id = p_tarea FOR UPDATE;
  IF NOT FOUND OR t.estado <> 'en_proceso' THEN
    RAISE EXCEPTION 'La tarea % no está en proceso', p_tarea;
  END IF;

  IF p_exito THEN
    UPDATE nexus.tareas SET estado = 'completada', terminada_en = now(), ultimo_error = NULL,
                            tomada_por = NULL, tomada_en = NULL
     WHERE id = p_tarea;
    RETURN 'completada';
  ELSIF t.intentos < t.max_intentos THEN
    UPDATE nexus.tareas
       SET estado = 'pendiente', ultimo_error = p_error, tomada_por = NULL, tomada_en = NULL,
           ejecutar_desde = now() + (CASE t.intentos WHEN 1 THEN interval '1 minute'
                                                     WHEN 2 THEN interval '5 minutes'
                                                     ELSE interval '30 minutes' END)
     WHERE id = p_tarea;
    RETURN 'reprogramada';
  ELSE
    UPDATE nexus.tareas SET estado = 'fallida', terminada_en = now(), ultimo_error = p_error,
                            tomada_por = NULL, tomada_en = NULL
     WHERE id = p_tarea;
    RETURN 'fallida';
  END IF;
END $$;

-- Devuelve a la cola las tareas de un trabajador que se cayó.
CREATE FUNCTION liberar_tareas_colgadas(p_despues_de interval DEFAULT interval '15 minutes') RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
  n integer;
BEGIN
  UPDATE nexus.tareas
     SET estado = 'pendiente', tomada_por = NULL, tomada_en = NULL,
         ultimo_error = coalesce(ultimo_error, 'Liberada: el trabajador no respondió')
   WHERE estado = 'en_proceso' AND tomada_en < now() - p_despues_de AND intentos < max_intentos;
  GET DIAGNOSTICS n = ROW_COUNT;
  RETURN n;
END $$;

-- Atiende una solicitud de supresión de datos (Ley 1581): borra o
-- enmascara los datos personales de un contacto, pero mantiene su correo
-- en la lista de supresión para no volver a escribirle.
CREATE FUNCTION anonimizar_contacto(p_contacto uuid, p_motivo text) RETURNS void
LANGUAGE plpgsql AS $$
DECLARE
  c nexus.contactos;
BEGIN
  SELECT * INTO c FROM nexus.contactos WHERE id = p_contacto FOR UPDATE;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'Contacto % no encontrado', p_contacto;
  END IF;

  IF c.correo IS NOT NULL THEN
    INSERT INTO nexus.supresiones (correo, motivo, origen, contacto_id)
    VALUES (c.correo, 'Solicitud de supresión de datos: ' || p_motivo, 'manual', p_contacto)
    ON CONFLICT DO NOTHING;
  END IF;

  PERFORM nexus.detener_envios(ARRAY[p_contacto], 'baja', true);
  UPDATE nexus.mensajes SET estado = 'cancelado' WHERE contacto_id = p_contacto AND estado = 'programado';

  UPDATE nexus.bases_legales
     SET vigente = false, revocada_en = now(), motivo_revocacion = 'Solicitud de supresión de datos'
   WHERE contacto_id = p_contacto AND vigente;
  DELETE FROM nexus.evidencias WHERE contacto_id = p_contacto;
  DELETE FROM nexus.verificaciones_correo WHERE contacto_id = p_contacto;
  UPDATE nexus.interacciones SET resumen = '[anonimizado]' WHERE contacto_id = p_contacto;
  UPDATE nexus.respuestas SET texto = '[anonimizado]', asunto = NULL, borrador = NULL, remitente = 'anonimizado@invalid'
   WHERE contacto_id = p_contacto;
  UPDATE nexus.mensajes SET cuerpo = '[anonimizado]', asunto = NULL, destinatario = 'anonimizado@invalid'
   WHERE contacto_id = p_contacto;
  UPDATE nexus.envios_formulario SET nombre = NULL, correo = NULL, telefono = NULL, cargo = NULL, datos = '{}'
   WHERE contacto_id = p_contacto;

  UPDATE nexus.contactos
     SET nombre = 'Contacto anonimizado', cargo = NULL, correo = NULL, telefono = NULL,
         estado_correo = 'desconocido', excluido = true, etapa = 'excluido',
         motivo_exclusion = coalesce(motivo_exclusion, 'Solicitud de supresión de datos'),
         excluido_en = coalesce(excluido_en, now()), anonimizado_en = now()
   WHERE id = p_contacto;

  -- Enmascara también el historial de auditoría de este contacto.
  PERFORM set_config('nexus.anonimizando', 'on', true);
  UPDATE nexus.registro_auditoria
     SET antes = CASE WHEN antes IS NULL THEN NULL ELSE '{"anonimizado": true}'::jsonb END,
         despues = CASE WHEN despues IS NULL THEN NULL ELSE '{"anonimizado": true}'::jsonb END
   WHERE (entidad = 'contactos' AND entidad_id = p_contacto::text)
      OR (antes ->> 'contacto_id') = p_contacto::text
      OR (despues ->> 'contacto_id') = p_contacto::text;
  PERFORM set_config('nexus.anonimizando', 'off', true);
END $$;


-- =====================================================================
-- DISPARADORES (GUARDIANES)
-- =====================================================================

-- 1. Inscribir solo contactos elegibles en campañas con aprobación vigente.
CREATE FUNCTION fn_guardian_inscripcion() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  motivo text;
  cam nexus.campanas;
BEGIN
  IF TG_OP = 'INSERT' OR (NEW.estado = 'activa' AND OLD.estado <> 'activa') THEN
    motivo := nexus.motivo_no_elegible(NEW.contacto_id);
    IF motivo IS NOT NULL THEN
      RAISE EXCEPTION 'No se puede inscribir: %', motivo USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO cam FROM nexus.campanas WHERE id = NEW.campana_id;
    IF cam.aprobacion_vigente_id IS NULL THEN
      RAISE EXCEPTION 'La campaña requiere una aprobación vigente' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.estado = 'activa' AND cam.estado <> 'activa' THEN
      RAISE EXCEPTION 'Solo una campaña activa puede tener inscripciones activas' USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guardian_inscripcion BEFORE INSERT OR UPDATE OF estado ON inscripciones
  FOR EACH ROW EXECUTE FUNCTION fn_guardian_inscripcion();

-- 2. Ningún correo se programa ni se envía si las reglas no lo permiten.
CREATE FUNCTION fn_guardian_mensaje() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  motivo text;
  ins nexus.inscripciones;
  cam nexus.campanas;
  correo_actual citext;
BEGIN
  SELECT correo INTO correo_actual FROM nexus.contactos WHERE id = NEW.contacto_id;
  IF correo_actual IS DISTINCT FROM NEW.destinatario THEN
    RAISE EXCEPTION 'El destinatario no coincide con el correo del contacto' USING ERRCODE = 'check_violation';
  END IF;

  IF NEW.tipo = 'respuesta_manual' THEN
    IF nexus.contacto_bloqueado(NEW.contacto_id) THEN
      RAISE EXCEPTION 'El contacto está suprimido o excluido' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;

  motivo := nexus.motivo_no_elegible(NEW.contacto_id);
  IF motivo IS NOT NULL THEN
    RAISE EXCEPTION 'Envío bloqueado: %', motivo USING ERRCODE = 'check_violation';
  END IF;

  SELECT * INTO ins FROM nexus.inscripciones WHERE id = NEW.inscripcion_id;
  IF ins.contacto_id <> NEW.contacto_id OR ins.estado <> 'activa' THEN
    RAISE EXCEPTION 'Envío bloqueado: la inscripción no está activa' USING ERRCODE = 'check_violation';
  END IF;

  SELECT * INTO cam FROM nexus.campanas WHERE id = ins.campana_id;
  IF cam.estado <> 'activa' OR cam.aprobacion_vigente_id IS DISTINCT FROM NEW.aprobacion_id THEN
    RAISE EXCEPTION 'Envío bloqueado: la campaña no está activa o su aprobación cambió' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER guardian_mensaje BEFORE INSERT OR UPDATE OF estado ON mensajes
  FOR EACH ROW WHEN (NEW.estado IN ('programado', 'enviando'))
  EXECUTE FUNCTION fn_guardian_mensaje();

-- 3. Una respuesta pausa al instante las secuencias de ese contacto.
CREATE FUNCTION fn_respuesta_pausa() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  PERFORM nexus.detener_envios(ARRAY[NEW.contacto_id], 'respondio', false);
  RETURN NEW;
END $$;
CREATE TRIGGER respuesta_pausa AFTER INSERT ON respuestas
  FOR EACH ROW EXECUTE FUNCTION fn_respuesta_pausa();

-- 4. Una supresión excluye a los contactos afectados y detiene sus envíos.
CREATE FUNCTION fn_supresion_aplica() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  afectados uuid[];
BEGIN
  SELECT coalesce(array_agg(id), '{}') INTO afectados
    FROM nexus.contactos
   WHERE (NEW.correo IS NOT NULL AND correo = NEW.correo)
      OR (NEW.dominio IS NOT NULL AND split_part(correo::text, '@', 2)::citext = NEW.dominio);

  IF cardinality(afectados) > 0 THEN
    UPDATE nexus.contactos
       SET excluido = true, etapa = 'excluido',
           motivo_exclusion = coalesce(motivo_exclusion, NEW.motivo),
           excluido_en = coalesce(excluido_en, now())
     WHERE id = ANY (afectados) AND anonimizado_en IS NULL;
    PERFORM nexus.detener_envios(afectados, CASE WHEN NEW.origen = 'rebote_duro' THEN 'rebote' ELSE 'baja' END, true);
    -- También las respuestas escritas a mano que aún no salieron.
    UPDATE nexus.mensajes SET estado = 'cancelado'
     WHERE contacto_id = ANY (afectados) AND estado = 'programado';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER supresion_aplica AFTER INSERT ON supresiones
  FOR EACH ROW EXECUTE FUNCTION fn_supresion_aplica();

-- 5. Rebote duro, baja o queja crean la supresión automáticamente.
CREATE FUNCTION fn_evento_correo() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  cid uuid;
  correo_c citext;
BEGIN
  cid := coalesce(NEW.contacto_id, (SELECT contacto_id FROM nexus.mensajes WHERE id = NEW.mensaje_id));
  IF cid IS NULL OR NEW.tipo NOT IN ('rebote_duro', 'baja', 'queja') THEN
    RETURN NEW;
  END IF;
  SELECT correo INTO correo_c FROM nexus.contactos WHERE id = cid;
  IF NEW.tipo = 'rebote_duro' THEN
    UPDATE nexus.contactos SET estado_correo = 'invalido' WHERE id = cid;
  END IF;
  IF correo_c IS NOT NULL THEN
    INSERT INTO nexus.supresiones (correo, motivo, origen, contacto_id)
    VALUES (correo_c,
            CASE NEW.tipo WHEN 'rebote_duro' THEN 'Rebote duro' WHEN 'baja' THEN 'Pidió la baja' ELSE 'Reportó spam' END,
            CASE NEW.tipo WHEN 'rebote_duro' THEN 'rebote_duro' WHEN 'baja' THEN 'enlace_baja' ELSE 'queja' END,
            cid)
    ON CONFLICT DO NOTHING;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER evento_correo AFTER INSERT ON eventos_correo
  FOR EACH ROW EXECUTE FUNCTION fn_evento_correo();

-- 6. Una oportunidad ganada convierte la empresa en cliente...
CREATE FUNCTION fn_oportunidad_cliente() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.etapa = 'cliente' AND (TG_OP = 'INSERT' OR OLD.etapa <> 'cliente') THEN
    UPDATE nexus.empresas SET estado_cuenta = 'cliente' WHERE id = NEW.empresa_id AND estado_cuenta <> 'cliente';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER oportunidad_cliente AFTER INSERT OR UPDATE OF etapa ON oportunidades
  FOR EACH ROW EXECUTE FUNCTION fn_oportunidad_cliente();

-- ...y una empresa cliente sale con todos sus contactos de las secuencias.
CREATE FUNCTION fn_empresa_cliente() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  afectados uuid[];
BEGIN
  IF NEW.estado_cuenta = 'cliente' AND OLD.estado_cuenta <> 'cliente' THEN
    SELECT coalesce(array_agg(id), '{}') INTO afectados FROM nexus.contactos WHERE empresa_id = NEW.id;
    PERFORM nexus.detener_envios(afectados, 'cliente', true);
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER empresa_cliente AFTER UPDATE OF estado_cuenta ON empresas
  FOR EACH ROW EXECUTE FUNCTION fn_empresa_cliente();

-- 7. Cambiar el contenido revoca las aprobaciones que lo usaban.
--    Pasos -> sube la versión de su secuencia.
CREATE FUNCTION fn_paso_cambia_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE nexus.secuencias SET version = version + 1
   WHERE id = coalesce(NEW.secuencia_id, OLD.secuencia_id);
  IF TG_OP = 'UPDATE' AND NEW.secuencia_id <> OLD.secuencia_id THEN
    UPDATE nexus.secuencias SET version = version + 1 WHERE id = OLD.secuencia_id;
  END IF;
  RETURN coalesce(NEW, OLD);
END $$;
CREATE TRIGGER paso_cambia_version AFTER INSERT OR UPDATE OR DELETE ON pasos
  FOR EACH ROW EXECUTE FUNCTION fn_paso_cambia_version();

--    Secuencia: si cambia su contenido sube la versión; si sube la versión, revoca.
CREATE FUNCTION fn_secuencia_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.version = OLD.version AND (
       NEW.nombre, NEW.objetivo, NEW.llamada_accion, NEW.metricas_exito,
       NEW.ventana_dias, NEW.ventana_desde, NEW.ventana_hasta)
     IS DISTINCT FROM (
       OLD.nombre, OLD.objetivo, OLD.llamada_accion, OLD.metricas_exito,
       OLD.ventana_dias, OLD.ventana_desde, OLD.ventana_hasta) THEN
    NEW.version := OLD.version + 1;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER secuencia_version BEFORE UPDATE ON secuencias
  FOR EACH ROW EXECUTE FUNCTION fn_secuencia_version();

CREATE FUNCTION fn_secuencia_revoca() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.version <> OLD.version THEN
    PERFORM nexus.revocar_aprobaciones(
      ARRAY(SELECT id FROM nexus.campanas WHERE secuencia_id = NEW.id AND aprobacion_vigente_id IS NOT NULL),
      'La secuencia cambió');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER secuencia_revoca AFTER UPDATE OF version ON secuencias
  FOR EACH ROW EXECUTE FUNCTION fn_secuencia_revoca();

--    Segmento: cambiar las reglas sube la versión, lo devuelve a revisión y revoca.
CREATE FUNCTION fn_segmento_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.reglas IS DISTINCT FROM OLD.reglas THEN
    NEW.version := OLD.version + 1;
    IF NEW.estado = 'aprobado' THEN
      NEW.estado := 'propuesto';
      NEW.aprobado_por := NULL;
      NEW.aprobado_en := NULL;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER segmento_version BEFORE UPDATE ON segmentos
  FOR EACH ROW EXECUTE FUNCTION fn_segmento_version();

CREATE FUNCTION fn_segmento_revoca() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.version <> OLD.version THEN
    PERFORM nexus.revocar_aprobaciones(
      ARRAY(SELECT id FROM nexus.campanas WHERE segmento_id = NEW.id AND aprobacion_vigente_id IS NOT NULL),
      'Las reglas del segmento cambiaron');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER segmento_revoca AFTER UPDATE ON segmentos
  FOR EACH ROW EXECUTE FUNCTION fn_segmento_revoca();

--    Marca: editarla sube su versión y revoca todas las aprobaciones vigentes.
CREATE FUNCTION fn_configuracion_version() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.valor IS DISTINCT FROM OLD.valor THEN
    NEW.version := OLD.version + 1;
    NEW.actualizado_en := now();
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER configuracion_version BEFORE UPDATE ON configuracion
  FOR EACH ROW EXECUTE FUNCTION fn_configuracion_version();

CREATE FUNCTION fn_marca_revoca() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.clave = 'marca' AND NEW.version <> OLD.version THEN
    PERFORM nexus.revocar_aprobaciones(
      ARRAY(SELECT id FROM nexus.campanas WHERE aprobacion_vigente_id IS NOT NULL),
      'La identidad de marca cambió');
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER marca_revoca AFTER UPDATE ON configuracion
  FOR EACH ROW EXECUTE FUNCTION fn_marca_revoca();

-- 8. Las versiones de una propuesta no se reescriben; solo se decide una vez.
CREATE FUNCTION fn_version_inmutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF (NEW.propuesta_id, NEW.version, NEW.contenido, NEW.autor, NEW.creada_en)
     IS DISTINCT FROM (OLD.propuesta_id, OLD.version, OLD.contenido, OLD.autor, OLD.creada_en) THEN
    RAISE EXCEPTION 'Una versión no se edita: cree una versión nueva' USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.decision IS NOT NULL AND (NEW.decision, NEW.comentario, NEW.decidida_por, NEW.decidida_en)
     IS DISTINCT FROM (OLD.decision, OLD.comentario, OLD.decidida_por, OLD.decidida_en) THEN
    RAISE EXCEPTION 'Esta versión ya fue decidida' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER version_inmutable BEFORE UPDATE ON propuesta_versiones
  FOR EACH ROW EXECUTE FUNCTION fn_version_inmutable();

-- 9. Cada cambio de etapa queda en el historial del embudo.
CREATE FUNCTION fn_cambio_etapa() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'INSERT' OR NEW.etapa <> OLD.etapa THEN
    INSERT INTO nexus.cambios_etapa (contacto_id, etapa_anterior, etapa_nueva, evento)
    VALUES (NEW.id, CASE WHEN TG_OP = 'UPDATE' THEN OLD.etapa END, NEW.etapa,
            coalesce(nexus.ajuste('nexus.evento'), CASE WHEN TG_OP = 'INSERT' THEN 'creacion' ELSE 'sin_detalle' END));
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER cambio_etapa AFTER INSERT OR UPDATE OF etapa ON contactos
  FOR EACH ROW EXECUTE FUNCTION fn_cambio_etapa();

-- 10. La última verificación actualiza el estado del correo del contacto.
CREATE FUNCTION fn_verificacion_aplica() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  UPDATE nexus.contactos SET estado_correo = NEW.resultado
   WHERE id = NEW.contacto_id AND correo = NEW.correo;
  RETURN NEW;
END $$;
CREATE TRIGGER verificacion_aplica AFTER INSERT ON verificaciones_correo
  FOR EACH ROW EXECUTE FUNCTION fn_verificacion_aplica();

-- 11. El registro de auditoría solo admite agregar filas.
CREATE FUNCTION fn_auditoria_inmutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' AND nexus.ajuste('nexus.anonimizando') = 'on' THEN
    RETURN NEW;      -- única excepción: anonimizar_contacto()
  END IF;
  RAISE EXCEPTION 'El registro de auditoría no se puede modificar ni borrar' USING ERRCODE = 'insufficient_privilege';
END $$;
CREATE TRIGGER auditoria_inmutable BEFORE UPDATE OR DELETE ON registro_auditoria
  FOR EACH ROW EXECUTE FUNCTION fn_auditoria_inmutable();
CREATE TRIGGER auditoria_sin_vaciar BEFORE TRUNCATE ON registro_auditoria
  FOR EACH STATEMENT EXECUTE FUNCTION fn_auditoria_inmutable();

-- 12. Auditoría automática de las tablas importantes.
CREATE FUNCTION fn_auditar() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
  v_antes jsonb;
  v_despues jsonb;
  v_id text;
BEGIN
  IF TG_OP <> 'INSERT' THEN v_antes := to_jsonb(OLD) - 'credenciales_cifradas' - 'token_hash'; END IF;
  IF TG_OP <> 'DELETE' THEN v_despues := to_jsonb(NEW) - 'credenciales_cifradas' - 'token_hash'; END IF;
  IF TG_OP = 'UPDATE' AND v_antes = v_despues THEN
    RETURN NEW;
  END IF;
  v_id := coalesce(v_despues ->> 'id', v_antes ->> 'id', v_despues ->> 'clave', v_antes ->> 'clave');

  INSERT INTO nexus.registro_auditoria (actor, administrador_id, accion, entidad, entidad_id, antes, despues)
  VALUES (coalesce(nexus.ajuste('nexus.actor'), 'sistema'),
          nexus.ajuste('nexus.administrador_id')::uuid,
          coalesce(nexus.ajuste('nexus.accion'), lower(TG_OP)),
          TG_TABLE_NAME, v_id, v_antes, v_despues);
  RETURN coalesce(NEW, OLD);
END $$;

DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'administradores', 'configuracion', 'integraciones',
    'empresas', 'contactos', 'bases_legales', 'posibles_duplicados', 'supresiones',
    'resultados_descubrimiento', 'segmentos', 'secuencias', 'pasos', 'campanas',
    'propuestas', 'propuesta_versiones', 'aprobaciones_campana', 'inscripciones',
    'respuestas', 'oportunidades', 'recomendaciones', 'prompts', 'costos'
  ] LOOP
    EXECUTE format('CREATE TRIGGER auditar AFTER INSERT OR UPDATE OR DELETE ON nexus.%I
                    FOR EACH ROW EXECUTE FUNCTION nexus.fn_auditar()', t);
  END LOOP;
END $$;

-- 13. actualizado_en automático.
DO $$
DECLARE
  t text;
BEGIN
  FOREACH t IN ARRAY ARRAY[
    'integraciones', 'empresas', 'contactos', 'segmentos', 'secuencias',
    'campanas', 'propuestas', 'inscripciones', 'oportunidades'
  ] LOOP
    EXECUTE format('CREATE TRIGGER actualizado_en BEFORE UPDATE ON nexus.%I
                    FOR EACH ROW EXECUTE FUNCTION nexus.fn_actualizado_en()', t);
  END LOOP;
END $$;


-- =====================================================================
-- VISTAS PARA LA API
-- =====================================================================

-- Elegibilidad de cada contacto (igual que motivoInscripcion del frontend).
CREATE VIEW v_contactos_elegibilidad AS
SELECT c.id AS contacto_id,
       nexus.motivo_no_elegible(c.id) AS motivo,
       nexus.motivo_no_elegible(c.id) IS NULL AS elegible
  FROM nexus.contactos c;

-- Base legal vigente de cada contacto (Contacto.baseLegal).
CREATE VIEW v_base_legal_vigente AS
SELECT b.contacto_id, b.tipo, b.texto, b.prueba, b.obtenida_en AS fecha,
       (b.aprobada_en IS NOT NULL) AS aprobada_por_admin
  FROM nexus.bases_legales b
 WHERE b.vigente;

-- Puntaje actual de cada empresa (Empresa.puntaje).
CREATE VIEW v_puntaje_actual AS
SELECT DISTINCT ON (empresa_id) empresa_id, total, incompleto, criterios, calculado_en
  FROM nexus.puntajes
 ORDER BY empresa_id, calculado_en DESC;

-- Métricas de cada campaña (Campana.metricas). Se calculan, no se guardan.
CREATE VIEW v_metricas_campana AS
SELECT c.id AS campana_id,
       (SELECT count(*) FROM nexus.inscripciones i WHERE i.campana_id = c.id) AS inscritos,
       count(DISTINCT m.id) FILTER (WHERE m.estado = 'enviado') AS enviados,
       count(DISTINCT m.id) FILTER (WHERE m.estado = 'enviado')
         - count(DISTINCT e.mensaje_id) FILTER (WHERE e.tipo = 'rebote_duro') AS entregados_estimados,
       count(DISTINCT e.mensaje_id) FILTER (WHERE e.tipo IN ('rebote_duro', 'rebote_suave')) AS rebotes,
       (SELECT count(*) FROM nexus.inscripciones i WHERE i.campana_id = c.id AND i.motivo_salida = 'baja') AS bajas,
       count(DISTINCT e.mensaje_id) FILTER (WHERE e.tipo = 'clic' AND NOT e.automatico) AS clics,
       count(DISTINCT e.mensaje_id) FILTER (WHERE e.tipo = 'apertura') AS aperturas_poco_fiables,
       (SELECT count(*) FROM nexus.respuestas r WHERE r.campana_id = c.id
           AND coalesce(r.clasificacion, '') NOT IN ('fuera_oficina')) AS respuestas,
       (SELECT count(*) FROM nexus.respuestas r WHERE r.campana_id = c.id AND r.clasificacion_confirmada
           AND r.clasificacion IN ('interesado', 'pide_informacion', 'quiere_reunion')) AS respuestas_positivas,
       (SELECT count(DISTINCT f.id) FROM nexus.envios_formulario f
          JOIN nexus.enlaces_seguimiento l ON l.referencia = f.referencia_opaca
          JOIN nexus.mensajes mm ON mm.id = l.mensaje_id
          JOIN nexus.inscripciones ii ON ii.id = mm.inscripcion_id
         WHERE ii.campana_id = c.id AND f.formulario = 'diagnostico') AS diagnosticos,
       (SELECT count(*) FROM nexus.inscripciones i WHERE i.campana_id = c.id AND i.motivo_salida = 'reunion') AS reuniones
  FROM nexus.campanas c
  LEFT JOIN nexus.inscripciones i ON i.campana_id = c.id
  LEFT JOIN nexus.mensajes m ON m.inscripcion_id = i.id AND m.tipo = 'secuencia'
  LEFT JOIN nexus.eventos_correo e ON e.mensaje_id = m.id
 GROUP BY c.id;

-- Distribución de contactos por etapa (GET /embudo).
CREATE VIEW v_embudo AS
SELECT x.etapa, x.orden, count(c.id) AS conteo
  FROM (VALUES ('identificado', 1), ('contactable', 2), ('contactado', 3), ('interesado', 4), ('lead', 5),
               ('calificado', 6), ('discovery', 7), ('propuesta', 8), ('cliente', 9), ('postventa', 10))
       AS x(etapa, orden)
  LEFT JOIN nexus.contactos c ON c.etapa = x.etapa AND c.anonimizado_en IS NULL
 GROUP BY x.etapa, x.orden
 ORDER BY x.orden;

-- Línea de tiempo de un contacto (GET /contactos/:id/interacciones).
CREATE VIEW v_linea_tiempo AS
SELECT i.id::text AS id, i.contacto_id, i.tipo, i.ocurrida_en AS fecha, i.resumen, i.origen
  FROM nexus.interacciones i
UNION ALL
SELECT m.id::text, m.contacto_id, 'correo_enviado', m.enviado_en, coalesce(m.asunto, 'Correo en el mismo hilo'), 'correo'
  FROM nexus.mensajes m WHERE m.estado = 'enviado'
UNION ALL
SELECT r.id::text, r.contacto_id, 'respuesta', r.recibida_en, left(r.texto, 140), 'correo'
  FROM nexus.respuestas r
UNION ALL
SELECT e.id::text, coalesce(e.contacto_id, m.contacto_id), 'clic', e.ocurrido_en, 'Abrió un enlace del correo', 'correo'
  FROM nexus.eventos_correo e LEFT JOIN nexus.mensajes m ON m.id = e.mensaje_id
 WHERE e.tipo = 'clic' AND NOT e.automatico;

-- Resumen del panel general (GET /panel/resumen). Fechas en hora de Bogotá.
CREATE VIEW v_resumen_panel AS
SELECT
  (SELECT count(*) FROM nexus.v_contactos_elegibilidad WHERE elegible) AS contactables,
  (SELECT count(*) FROM nexus.campanas WHERE estado = 'activa') AS activas,
  (SELECT count(*) FROM nexus.respuestas WHERE estado = 'pendiente') AS pendientes,
  (SELECT count(*) FROM nexus.envios_formulario
    WHERE formulario = 'diagnostico'
      AND date_trunc('month', recibido_en AT TIME ZONE 'America/Bogota')
        = date_trunc('month', now() AT TIME ZONE 'America/Bogota')) AS diagnosticos,
  (SELECT count(*) FROM nexus.interacciones
    WHERE tipo = 'reunion'
      AND date_trunc('month', ocurrida_en AT TIME ZONE 'America/Bogota')
        = date_trunc('month', now() AT TIME ZONE 'America/Bogota')) AS reuniones,
  (SELECT count(*) FROM nexus.mensajes
    WHERE estado = 'enviado' AND tipo = 'secuencia'
      AND (enviado_en AT TIME ZONE 'America/Bogota')::date = (now() AT TIME ZONE 'America/Bogota')::date) AS enviados_hoy;

-- Métricas diarias de correo por campaña (Informes · MetricaDiaria).
CREATE VIEW v_metricas_correo_diarias AS
SELECT (m.enviado_en AT TIME ZONE 'America/Bogota')::date AS fecha,
       i.campana_id, c.segmento_id,
       count(*) AS enviados,
       count(DISTINCT r.id) AS respuestas
  FROM nexus.mensajes m
  JOIN nexus.inscripciones i ON i.id = m.inscripcion_id
  JOIN nexus.campanas c ON c.id = i.campana_id
  LEFT JOIN nexus.respuestas r ON r.mensaje_origen_id = m.id
 WHERE m.estado = 'enviado' AND m.tipo = 'secuencia'
 GROUP BY 1, 2, 3;


-- =====================================================================
-- DATOS INICIALES
-- =====================================================================

INSERT INTO integraciones (id, nombre) VALUES
  ('gmail', 'Gmail · Google Workspace'),
  ('formulario', 'Formulario del sitio web'),
  ('ga4', 'Google Analytics 4'),
  ('search_console', 'Google Search Console'),
  ('verificador', 'Verificador de correos'),
  ('claude', 'API de Claude');

INSERT INTO configuracion (clave, valor) VALUES
  ('marca', jsonb_build_object(
     'nombre', 'NEXUS Innovación',
     'oferta', 'RSE Express → RSE por Retos',
     'tono', 'Cercano, claro y profesional',
     'firma', 'Equipo NEXUS · Candelaria, Valle del Cauca',
     'prohibidas', jsonb_build_array('garantizado', 'gratis para siempre'),
     'politica', 'https://nexus.example/privacidad',   -- reemplazar por la URL real de la política
     'baja', 'Si no desea recibir mensajes, responda BAJA.')),
  ('limites', jsonb_build_object(
     'topeDiarioNuevos', 20,
     'separacionSegundos', jsonb_build_array(60, 180),
     'ventana', jsonb_build_object('dias', jsonb_build_array(1, 2, 3, 4, 5), 'desde', '08:00', 'hasta', '17:00'),
     'topePorDominio', 2,
     'umbralRebotePausa', 5)),
  ('cliente_ideal', jsonb_build_object(
     'criterios', jsonb_build_array(
       'MiPymes de 11 a 200 empleados',
       'Valle del Cauca',
       'Proveedor de empresas grandes')));

-- =====================================================================
-- PERMISOS (opcional, recomendado en producción)
-- La app se conecta con un rol propio, sin permiso para alterar la auditoría.
-- Quite los comentarios y cambie la contraseña antes de ejecutarlo.
-- =====================================================================
-- CREATE ROLE nexus_app LOGIN PASSWORD 'cambiar-esta-clave';
-- GRANT USAGE ON SCHEMA nexus TO nexus_app;
-- GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA nexus TO nexus_app;
-- REVOKE UPDATE, DELETE, TRUNCATE ON nexus.registro_auditoria FROM nexus_app;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA nexus TO nexus_app;
-- ALTER ROLE nexus_app SET search_path = nexus, public;
-- (anonimizar_contacto necesita modificar la auditoría: créela con
--  SECURITY DEFINER y dueño administrador si aplica el REVOKE anterior.)

COMMIT;
