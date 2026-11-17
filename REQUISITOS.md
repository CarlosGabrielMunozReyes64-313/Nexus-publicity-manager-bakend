# Prompt · Backend de Nexus-publicity-manager (Python + FastAPI + PostgreSQL)

## 0. Archivos adjuntos y cuál manda

Con este prompt recibes cuatro archivos. Léelos completos antes de escribir código.

| Archivo | Qué es | Para qué lo usas |
|---|---|---|
| `.zip` del frontend (`Nexus-publicity-manager-main`) | El frontend ya construido en Vite + React + TypeScript. | Es el **contrato del API**. Lo más importante: `src/api/schemas.ts` (forma exacta de cada JSON), `src/types/index.ts`, `src/mocks/handlers.ts` (comportamiento de cada ruta), `src/lib/reglas.ts` (reglas de elegibilidad, segmentos y secuencias), `src/api/client.ts` y `API.md`. |
| Documentación del proyecto (PDF o documento) | Explica qué hace cada parte del frontend. | Contexto de pantallas y flujos. |
| `nexus_publicity_manager_schema.sql` | El esquema completo de PostgreSQL: 40 tablas, 8 vistas, disparadores y funciones. | Es **la base de datos**. No la rediseñes. |
| `nexus_publicity_manager_pruebas.sql` | 37 pruebas de las reglas del esquema. | Deben seguir pasando siempre. |

Si dos fuentes se contradicen, manda este orden:

1. **El esquema SQL** para todo lo que es dato y regla de negocio. Nunca debilites una regla, un `CHECK` o un disparador.
2. **`src/api/schemas.ts`** para la forma de cada respuesta JSON.
3. **Este prompt** para el comportamiento del backend. Donde este prompt corrige al mock (`handlers.ts`), gana este prompt: el mock simplifica cosas a propósito.
4. `src/mocks/handlers.ts` para todo comportamiento que este prompt no defina.
5. La documentación.

Si algo sigue sin estar definido, elige la opción más simple y segura, y anótala en `DECISIONES.md`.

## 1. Tu rol

Actúa como un ingeniero backend senior en Python, FastAPI, PostgreSQL, la Gmail API y la API de Claude. Vas a construir el backend completo de **Nexus-publicity-manager**, listo para producción, que conecte con el frontend adjunto sin cambiarle nada salvo lo indicado en la sección 22.

Trabaja con estas prioridades, en este orden: **no enviar un correo que no debía salir**, no perder ni inventar datos, cumplir el contrato del frontend, y después todo lo demás.

## 2. Contexto del negocio

- **Empresa:** NEXUS Innovación, consultora de sostenibilidad y RSE en Candelaria, Valle del Cauca (Colombia).
- **Cliente ideal:** MiPymes de 11 a 200 empleados del Valle del Cauca, proveedoras de empresas grandes que empiezan a pedirles información de sostenibilidad.
- **Oferta:** el diagnóstico gratuito **RSE Express** es la puerta de entrada; **RSE por Retos** es la primera oferta de pago.
- **Usuario:** una sola persona, el administrador de publicidad y correos.
- **Volumen:** de 200 a 500 contactos el primer trimestre y menos de 50 correos nuevos al día. Diseña para esto: un monolito sencillo, sin Redis, sin microservicios, sin Kubernetes.
- **Idioma y hora:** español de Colombia. Zona horaria `America/Bogota` para ventanas, días hábiles y "hoy".
- **Marco legal:** Ley 1581 de 2012 (protección de datos personales).

## 3. Qué hace el backend

La aplicación centraliza los contactos B2B de NEXUS, deja que agentes de IA investiguen y propongan campañas, y **solo envía por Gmail lo que el administrador aprueba**. Mide cada paso en un embudo comercial.

Tres principios que el backend hace cumplir, no solo la interfaz:

1. **Humano en el circuito.** Los agentes leen datos y escriben propuestas. Ningún agente tiene una herramienta para enviar correos, aprobar o cambiar una campaña aprobada. El único camino a Gmail es el motor de secuencias, un proceso sin IA con reglas fijas.
2. **Cada dato con procedencia.** Todo dato nuevo lleva fuente, fecha y estado (`verificado`, `inferido`, `pendiente`). Si no hay evidencia, el campo queda vacío y pendiente. Nada se completa por intuición.
3. **Cumplimiento.** Solo se envía a contactos elegibles: no excluidos, no suprimidos, con base legal aprobada por el administrador, con correo válido y cuya empresa no sea cliente. Las bajas se aplican al instante y sin aprobación.

El esquema SQL ya garantiza estas reglas con disparadores. El backend las respeta, las repite en sus validaciones para dar buenos mensajes de error, y **nunca** intenta saltárselas.

## 4. Alcance y entregables

Entrega un repositorio `nexus-publicity-manager-backend` con:

- El backend completo (API, trabajador y programador de tareas).
- Migraciones con Alembic. La migración `0001` aplica el esquema SQL adjunto **tal cual** (ver sección 7).
- Integraciones: Google (inicio de sesión), Gmail API, API de Claude, verificador de correo (Hunter) con consulta DNS, GA4 Data API, Search Console API y webhook del formulario del sitio.
- Pruebas automáticas (sección 20) y un archivo `VALIDACION.md` con los resultados reales de ejecutarlas.
- `Dockerfile` de varias etapas que construye el frontend adjunto y lo sirve desde el mismo contenedor, y `docker-compose.yml` para desarrollo con PostgreSQL 16.
- `README.md` (instalación, variables, cómo conectar cada integración, cómo desplegar), `.env.example` y `DECISIONES.md`.
- Un script de datos ficticios de demostración, solo para desarrollo.

## 5. Stack y librerías

| Parte | Elección |
|---|---|
| Lenguaje | Python 3.12 |
| API | FastAPI + Uvicorn, Pydantic v2 |
| Acceso a datos | psycopg 3 con SQL explícito y parametrizado (puedes usar SQLAlchemy 2 Core). Gran parte de la lógica vive en disparadores: no la dupliques en un ORM que la esconda. |
| Migraciones | Alembic |
| Base de datos | PostgreSQL 16 o superior, con la extensión `citext` |
| Cola y programación | La tabla `nexus.tareas` con las funciones del esquema. Nada de Celery ni Redis. |
| Google | `google-auth`, `google-auth-oauthlib`, `google-api-python-client` |
| Claude | SDK oficial `anthropic` para Python |
| HTTP y lectura web | `httpx`, `beautifulsoup4` (o `trafilatura`) para extraer texto |
| Correo | `email` de la biblioteca estándar para armar MIME, `dnspython` para registros MX |
| Similitud de textos | `rapidfuzz` |
| Días hábiles | `holidays` con los festivos de Colombia |
| Tokens firmados y cifrado | `itsdangerous` para enlaces de baja, `cryptography` (AES-GCM o Fernet) para credenciales |
| Registros | `structlog` en JSON |
| Calidad | `pytest`, `ruff`, `mypy` en modo estricto en el código propio |

## 6. Arquitectura y estructura del proyecto

Un solo contenedor con dos procesos y el frontend estático:

- **`api`**: Uvicorn con FastAPI. Sirve la API en `/api`, las rutas públicas `/r/...` y `/baja/...`, y el frontend compilado (`dist/`) con respaldo a `index.html` para las rutas del SPA. Mismo origen para frontend y API: así `VITE_API_URL` queda en su valor por defecto `/api` y no hace falta CORS.
- **`trabajador`**: un proceso que (a) cada minuto encola las tareas periódicas y (b) toma tareas con `nexus.tomar_tareas()` y las ejecuta. Arráncalo con `honcho` y un `Procfile`, o como dos servicios en `docker-compose`.

```
app/
  main.py                 # FastAPI, montaje del SPA, manejadores de error
  config.py               # variables de entorno (pydantic-settings)
  db.py                   # pool, transacción con actor (set_config)
  seguridad/              # sesión, OAuth de Google, cifrado, CSRF
  api/                    # un módulo por recurso del frontend
  esquemas/               # modelos Pydantic de entrada y salida (camelCase)
  dominio/                # reglas: elegibilidad, segmentos, secuencias, etapas, puntaje
  motor/                  # programador de envíos, render de plantillas, envío
  integraciones/          # gmail.py, claude.py, hunter.py, dns.py, ga4.py, search_console.py
  agentes/                # investigador, segmentador, estratega_redactor, analista, verificador_citas, lector_web
  tareas/                 # trabajador, programador, un manejador por tipo de tarea
  publico/                # webhook del formulario, redirector de clics, página de baja
migraciones/
pruebas/
scripts/                  # datos de demostración, validación de contrato
```

Cada integración externa tiene una **interfaz** y dos implementaciones: la real y una falsa para pruebas (`GmailFalso`, `ClaudeFalso`, `HunterFalso`...). El reloj también se inyecta (`Reloj`), para poder probar ventanas y días hábiles sin esperar.

## 7. Base de datos

- La migración `0001` ejecuta `nexus_publicity_manager_schema.sql` completo. Alembic ya abre una transacción, así que quita solo las líneas `BEGIN;` y `COMMIT;` del archivo al ejecutarlo. No cambies nada más del archivo.
- Todo cambio posterior va en migraciones nuevas, numeradas y reversibles, con su motivo en `DECISIONES.md`. Puedes **agregar** columnas, índices o tablas si de verdad hacen falta. No puedes quitar ni relajar reglas.
- `search_path = nexus, public` en cada conexión.
- La app se conecta con un rol propio sin permiso para modificar `registro_auditoria`. El bloque comentado al final del SQL muestra cómo crearlo. `anonimizar_contacto` debe quedar como `SECURITY DEFINER` con dueño administrador si aplicas ese `REVOKE`.

### Quién actúa en cada transacción

Cada transacción que escribe empieza indicando quién actúa. La auditoría y el historial de etapas lo leen de ahí. Usa `set_config(..., true)`, que acepta parámetros, en lugar de `SET LOCAL`:

```sql
SELECT set_config('nexus.actor', %s, true);            -- 'admin' | 'agente:investigador' | 'proceso:motor_secuencias' ...
SELECT set_config('nexus.administrador_id', %s, true); -- uuid del administrador, cuando actúa él
SELECT set_config('nexus.accion', %s, true);           -- 'aprobar_propuesta', 'inscribir', ...
SELECT set_config('nexus.evento', %s, true);           -- motivo de un cambio de etapa: 'primer_envio', 'respuesta_positiva'...
```

Actores: `admin`, `agente:investigador`, `agente:segmentador`, `agente:estratega_redactor`, `agente:analista`, `proceso:motor_secuencias`, `proceso:rastreador_gmail`, `proceso:webhook_formulario`, `proceso:sincronizador_analitica`, `proceso:verificador`.

### Lo que ya hace la base (no lo reimplementes)

| Ya lo hace la base | Cómo |
|---|---|
| Elegibilidad para inscribir y enviar | Disparadores `guardian_inscripcion` y `guardian_mensaje`; función `motivo_no_elegible(uuid)`. |
| Respuesta pausa la secuencia | Disparador al insertar en `respuestas`. |
| Supresión excluye y detiene | Disparador al insertar en `supresiones` (por correo o dominio). |
| Rebote duro, baja o queja crean la supresión | Disparador al insertar en `eventos_correo`. |
| Editar paso, secuencia, reglas de segmento o marca revoca aprobaciones | Disparadores de versión + `revocar_aprobaciones(uuid[], text)`. |
| Oportunidad ganada vuelve cliente a la empresa y la saca de secuencias | Disparadores en `oportunidades` y `empresas`. |
| Copia aprobada congelada y su huella SHA-256 | Disparador `aprobacion_congelada`. |
| Historial de etapas | Disparador en `contactos` → `cambios_etapa`. |
| Auditoría de 22 tablas, inmutable | Disparadores `auditar` y `auditoria_inmutable`. |
| Cola de tareas | `tomar_tareas`, `terminar_tarea`, `liberar_tareas_colgadas`. |
| Derecho de supresión | `anonimizar_contacto(uuid, text)`. |

El backend **sí** debe: validar antes de escribir para dar mensajes claros, crear las propuestas de revisión cuando una campaña vuelve a revisión (sección 9.6), mover etapas según la sección 14 y registrar en `registro_auditoria` las acciones que no pasan por tablas auditadas (inicio y cierre de sesión, envíos, ejecuciones de agentes con su `costo_usd`).

### Errores de la base hacia el API

Los disparadores lanzan mensajes en español (por ejemplo, `No se puede inscribir: Base legal sin aprobar`). Tradúcelos así:

- `RAISE` de un disparador o `check_violation` con mensaje propio → **400** con ese mensaje.
- `check_violation` de una restricción sin mensaje propio → **400** con un mensaje en español escrito por ti para cada restricción relevante (tabla en `app/db.py`).
- `unique_violation` → **409** con mensaje por índice (`contactos_correo_uq` → "Ya existe un contacto con ese correo"; `empresas_dominio_uq` → "Ya existe una empresa con ese dominio"...).
- Nunca devuelvas el texto crudo de PostgreSQL ni trazas.

## 8. Contrato con el frontend

Lee `src/api/client.ts`: el frontend valida **cada** respuesta con Zod y, si no coincide, muestra "La API devolvió datos con un formato inesperado". Por eso:

1. **Nombres en `camelCase`** exactamente como en `schemas.ts`. La base usa `snake_case`.
2. **Campos opcionales: omítelos cuando no hay valor. Nunca envíes `null`** en un campo marcado `.optional()`: Zod lo rechaza. Solo tres campos admiten `null` y **deben estar presentes**: `evidencia.valor`, `metrica.costoUSD` y `puntaje.criterios[].cumple`. Implementa un serializador que quite los `None` salvo esos tres.
3. **Fechas** en ISO 8601 con zona (`2026-11-16T14:05:00-05:00` o en UTC con `Z`).
4. **Identificadores** como texto. El `id` numérico de `registro_auditoria` se envía como texto.
5. **Colecciones** como arreglo JSON (`[]` si no hay nada, nunca `null`). Sin paginación: el volumen es pequeño. Excepción: `/auditoria` devuelve las 500 filas más recientes si no se filtra.
6. **Mutaciones** (POST, PUT, PATCH) responden `{"ok": true}`, salvo las de previsualización, que devuelven su objeto.
7. **Errores** con estado 4xx o 5xx y cuerpo `{"message": "Texto en español"}`. Reemplaza el manejador de validación de FastAPI: un cuerpo inválido responde **400** con `{"message": ...}`, no 422 con `detail`.
8. **Campos que el frontend envía pero no controla.** En los POST de creación el frontend envía `id: "nuevo"`, `creadoEn`, `actualizadoEn`, `version`, `estado`, `tamano` (del segmento) y `baseLegal.aprobadaPorAdmin`. Acéptalos e **ignóralos**: el servidor asigna ids, fechas, versiones y estados, y una base legal nunca llega aprobada desde un formulario.
9. **Sesión** por cookie: el frontend usa `credentials: "include"`.

### 8.1 Cómo se arma cada tipo del frontend

| Tipo (`schemas.ts`) | Origen y reglas de armado |
|---|---|
| `Empresa` | `empresas` sin las fusionadas (`fusionada_en_id IS NULL`). `senales` de `senales` (`fecha` = `detectada_en`). `puntaje` de `v_puntaje_actual` (omítelo si no hay). `evidencias` de `evidencias` con `empresa_id`. |
| `Contacto` | `contactos`. `baseLegal` de `v_base_legal_vigente`; si no hay fila vigente, `{tipo: "pendiente", aprobadaPorAdmin: false}`. `fecha` = `obtenida_en`. `evidencias` de `evidencias` con `contacto_id`. `motivoExclusion`, `creadoEn`, `actualizadoEn`. |
| `Evidencia` | `campo`, `valor` (puede ser `null`), `fuente`, `url`, `cita`, `obtenidoEn`, `estado`. |
| `Segmento` | `segmentos`. `tamano` = número de contactos que cumplen sus reglas **hoy**, calculado con la misma lógica de `coincide()` de `src/lib/reglas.ts`. |
| `Secuencia` | `secuencias` + `pasos` ordenados. `ventanaEnvio = {dias: ventana_dias, desde: "HH:MM", hasta: "HH:MM"}`. `variables` de `pasos.variables`. |
| `Campana` | `campanas` + `v_metricas_campana`. `metricas.entregados` = `entregados_estimados`; `metricas.aperturas` = `aperturas_poco_fiables`. `versionAprobada` = `propuesta_versiones.version` de la aprobación vigente; `huellaAprobada` = su `huella`. Omite ambos si no hay aprobación vigente. `creadaEn` = `creado_en`. |
| `PropuestaAgente` | `propuestas`. `version` = `version_actual`. `versiones` = sus `propuesta_versiones` (`version`, `contenido`, `fecha` = `creada_en`). `fuentes` = `evidencias` con `propuesta_id`. `objetivoId` = el `objetivo_*` que no sea nulo. `decision` = la decisión de la versión actual (`fecha` = `decidida_en`, `comentario`). |
| `Respuesta` | `respuestas`. `fecha` = `recibida_en`. `hilo` = todos los `mensajes` enviados y `respuestas` del mismo `gmail_thread_id`, ordenados por fecha: `de: "nexus"` para mensajes, `de: "contacto"` para respuestas, `texto` sin la parte citada. Si aún no hay clasificación: `clasificacion: "necesita_revision"`, `confianza: 0`. |
| `Oportunidad` | `oportunidades`. `creadaEn`, `cerradaEn`. |
| `BusquedaDescubrimiento` | `busquedas_descubrimiento` con `criterios = {consulta, municipios, sectores, tamano}`. `resultados` de `resultados_descubrimiento`: `empresa {razonSocial, sector, municipio, sitioWeb}` (texto vacío si falta sector o municipio), `contactoCorporativo {correo, telefono, whatsapp, formularioUrl}`, `personas` de `personas_descubiertas` (`requiereBaseLegal: true`), `evidencias` con `resultado_id`, `posibleDuplicadoDe`, `estado`. |
| `Supresion` | `supresiones`. `fecha` = `creado_en`. |
| `Inscripcion` | `inscripciones`. |
| `Interaccion` | `v_linea_tiempo`. |
| `Auditoria` | `registro_auditoria`. `entidadId`, `costoUSD` = `costo_usd`. |
| `Integracion` | `integraciones`. **Nunca** devuelvas credenciales, permisos ni cursores. |
| `Marca`, `LimitesEnvio`, `ClienteIdeal` | `configuracion.valor` de las claves `marca`, `limites`, `cliente_ideal`. |
| `Duplicado` | `posibles_duplicados` en estado `pendiente`. |
| `Recomendacion` | `recomendaciones`. |
| Resumen del panel | `v_resumen_panel` (`enviados_hoy` → `enviadosHoy`). |

## 9. Endpoints

Todas las rutas van bajo `/api` y exigen sesión de administrador, salvo las marcadas como públicas. Cada mutación corre en **una sola transacción** con el actor `admin`: si algo falla, no queda nada a medias (igual que el mock, que revierte todo).

### 9.1 Autenticación

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/auth/google` | Redirige a Google (OpenID Connect con `state`, `nonce` y PKCE). Parámetro `hd` con el dominio de Workspace. Pide solo `openid email profile`. |
| GET | `/auth/google/callback` | Valida el token, que el correo esté verificado, que `hd` sea el dominio permitido y que el correo exista en `administradores` con `activo = true`. Crea la sesión y redirige a `APP_URL + "/"`. Si falla, redirige a `/login?error=...`. |
| GET | `/auth/session` | `{ok: true}` con sesión válida; si no, 401 con `{message}`. |
| GET | `/auth/logout` | Revoca la sesión, borra la cookie y redirige a `APP_URL + "/login"`. |

### 9.2 Panel, embudo e informes

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/panel/resumen` | Desde `v_resumen_panel`. |
| GET | `/embudo?campana=&segmento=&fuente=&desde=&hasta=` | `[{etapa, conteo}]` con las 10 etapas del embudo en orden (identificado … postventa), igual que el mock: filtra por campaña (contactos inscritos en ella), segmento (contactos que cumplen sus reglas), `fuente` = `origen`, y `creado_en` entre las fechas. Excluye contactos anonimizados. |
| GET | `/informes/metricas?agrupacion=&desde=&hasta=` | Arreglo de `MetricaDiaria` (ver 9.13). El frontend agrupa por su cuenta: devuelve filas diarias. |
| GET | `/informes/recomendaciones` | `recomendaciones`. |
| POST | `/informes/costos` | `{costoUSD}` ≥ 0. Inserta en `costos` con fecha de hoy en Bogotá, `categoria: "otro"`, `concepto: "Registro manual"`, `moneda: "USD"`. |
| POST | `/informes/recomendaciones/:id/aprobar` | Marca `aprobada` y crea una propuesta `mejora` del agente `analista` (versión 1, contenido = el texto y el sustento) enlazada en `recomendaciones.propuesta_id`. |
| POST | `/informes/recomendaciones/:id/descartar` | Marca `descartada`. |

### 9.3 Empresas, contactos y duplicados

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/empresas`, `/empresas/:id` | Sin fusionadas. 404 si no existe. |
| POST | `/empresas` | Normaliza (sección 15) y crea. Si el NIT o el dominio ya existen, 409. Guarda las `evidencias` recibidas como `pendiente` con `fuente: manual`. Encola `enriquecer_empresa` si tiene sitio web. |
| PATCH | `/empresas/:id` | Actualización parcial. Cada campo cambiado a mano genera su evidencia `manual`. |
| GET | `/contactos`, `/contactos/:id` | |
| POST | `/contactos` | Crea el contacto y una base legal **no aprobada** con lo recibido. Si el correo ya existe, 409. Encola `verificar_correo`. |
| POST | `/contactos/importar` | `{contactos: Contacto[]}`. Omite los que ya existen por correo (sin error), exige que la empresa exista, ignora cualquier aprobación legal entrante. Encola `verificar_correo` por cada nuevo. |
| PATCH | `/contactos/:id` | Parcial. Si trae `baseLegal`: en la misma transacción marca la vigente como no vigente (`revocada_en`, motivo "Reemplazada") e inserta la nueva **sin aprobar**. Si cambia `correo`: `estado_correo` vuelve a `desconocido` y se encola su verificación. |
| POST | `/contactos/:id/excluir` | `{motivo}` de 3 o más caracteres. Si tiene correo, inserta una supresión `origen: manual` con ese motivo (el disparador excluye y detiene). Si no tiene correo, marca `excluido` con motivo y etapa `excluido`. |
| POST | `/contactos/:id/base-legal/aprobar` | Aprueba la base vigente (`aprobada_por`, `aprobada_en`). Si falta tipo, texto o prueba, 400 con "Registre tipo, texto y prueba de la base legal antes de aprobar". Recalcula la etapa (sección 14). |
| GET | `/contactos/:id/interacciones` | Desde `v_linea_tiempo`, de la más reciente a la más antigua. |
| GET | `/duplicados` | Pendientes. |
| POST | `/duplicados/:id/resolver` | Cuerpo: `{razonSocial|sector|municipio|tamano|sitioWeb|dominio: "principal" | "duplicado"}`. Copia a la principal los campos elegidos del duplicado, mueve a la principal los contactos, oportunidades, señales y evidencias, marca el duplicado con `fusionada_en_id` (no lo borres) y deja el par en `fusionado` con `campos_elegidos`. Todo en una transacción. |

### 9.4 Descubrimiento

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/descubrimiento/busquedas`, `/descubrimiento/busquedas/:id` | |
| POST | `/descubrimiento/busquedas` | `{consulta, municipios[], sectores[], tamano?}` (un `tamano` vacío se guarda como nulo). Crea la búsqueda `en_cola` y encola `descubrir_empresas`. |
| POST | `/descubrimiento/resultados/:id/aceptar` | Solo si está `pendiente`. Si tiene `posible_duplicado_de`: 400 "Posible duplicado: revise la empresa existente antes de aceptar". Crea la empresa (`tamano: desconocido`, `prospecto`), copia las evidencias, y si hay correo corporativo crea un contacto "Contacto corporativo" (`origen: descubrimiento`, etapa `identificado`, base legal `corporativo_generico` **sin aprobar**). Encola `verificar_correo` y `enriquecer_empresa`. Las personas descubiertas **no** se convierten en contactos solas. |
| POST | `/descubrimiento/resultados/:id/descartar` | Solo si está `pendiente`. |

### 9.5 Segmentos

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/segmentos` | Con `tamano` calculado. |
| POST | `/segmentos` | Crea con `estado: propuesto`, `propuesto_por: admin`. Valida cada regla (campos admitidos: `municipio`, `sector`, `tamano`, `puntaje`, `etapa`, `origen`, `baseLegal`, `contactoIds`). |
| PATCH | `/segmentos/:id` | Si el cuerpo es `{estado: "archivado"}`, archiva. Si no, guarda `nombre`, `reglas` y `justificacion`; el disparador sube la versión, lo devuelve a `propuesto` y revoca aprobaciones; luego aplica 9.6. Ignora `estado`, `version` y `tamano` enviados. |
| POST | `/segmentos/:id/aprobar` | `estado: aprobado` con `aprobado_por` y `aprobado_en`. |
| POST | `/segmentos/previsualizar` | Cuerpo: `ReglaSegmento[]`. Devuelve `{conteo, muestra}` con hasta 8 contactos. |

La evaluación de reglas debe dar **exactamente** el mismo resultado que `coincide()` de `src/lib/reglas.ts` (incluidos `contiene` sin distinguir mayúsculas, `mayor_que` y `menor_que` numéricos, `puntaje` = total actual o 0, `baseLegal` = `"aprobada"` o `"pendiente"`, y `contactoIds` como lista separada por comas). Porta los casos de `src/lib/reglas.test.ts` a pytest.

### 9.6 Propuestas, secuencias y aprobación de campañas

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/propuestas`, `/propuestas/:id` | Pendientes primero, luego por fecha. |
| PUT | `/propuestas/:id` | `{resumen}` de 5 o más caracteres. Crea una versión nueva (`autor: admin`) con el contenido de la versión anterior y el resumen nuevo, sube `version_actual`, deja la propuesta `pendiente`. Si es de tipo `campana`, la campaña vuelve a `en_revision` con `revocar_aprobaciones`. |
| POST | `/propuestas/:id/rechazar` | `{comentario}` de 3 o más caracteres. Registra la decisión en la versión actual y deja la propuesta `rechazada`. Si es del agente estratega-redactor, encola `revisar_campana` para que proponga una versión nueva que atienda el comentario. |
| POST | `/propuestas/:id/aprobar` | Ver abajo. |
| GET | `/secuencias`, `/secuencias/:id` | |
| PUT | `/secuencias/:id` | Secuencia completa. Valida con la misma lógica de `validarSecuencia()` de `src/lib/reglas.ts` y las palabras prohibidas de la marca; si hay errores, 400 con todos unidos por ". ". Reemplaza los pasos en una transacción (la restricción de orden es diferible). Los disparadores suben la versión y revocan; luego aplica la **revisión de campañas**. |
| POST | `/secuencias/:id/previsualizar` | `{contactoId}` → `{pasos, respaldo}` con el render real del motor (sección 11.4), sin enviar nada. `respaldo` lista las variables que usaron su texto de respaldo. |

**Aprobar una propuesta**, según su tipo:

- `correo_inferido`: siempre 400, "Un correo inferido requiere verificación; no se aprueba para envíos".
- `campana`: valida la secuencia (como en `PUT`) y que la ventana de la secuencia se cruce con la ventana global de `limites`. Crea una fila en `aprobaciones_campana` con `contenido_congelado` = `{secuencia con sus pasos, reglas y versión del segmento, firma, texto de baja y versión de la marca}`, las tres versiones y `propuesta_version_id` de la versión actual. Pone `campanas.aprobacion_vigente_id` y `estado: aprobada`. Registra la decisión en la versión.
- `segmento`: aprueba el segmento objetivo.
- `enriquecimiento`: aplica cada campo propuesto a la empresa o contacto objetivo y guarda su evidencia con el estado que traiga (verificado solo si la cita fue comprobada).
- `mejora`: el contenido trae cambios estructurados (`{objetivo: "campana" | "secuencia" | "segmento", id, cambios: {...}}`). Aplícalos con las mismas validaciones que las rutas de edición; los disparadores devuelven a revisión lo afectado. **Nunca** se aplica una mejora a una campaña sin pasar otra vez por aprobación.
- Siempre: la propuesta queda `aprobada`, la versión con `decision: aprobada`, `decidida_por`, `decidida_en`. Si ya estaba aprobada, 400 "Propuesta ya aprobada".

**Revisión de campañas.** Cada vez que una edición revoca la aprobación de una campaña (paso, secuencia, segmento, marca o mejora), en la misma transacción busca la propuesta `campana` de esa campaña (o créala, agente `estratega_redactor`) y agrégale una versión nueva (`autor: admin`) con la secuencia completa actual. La propuesta queda `pendiente`. Así el administrador siempre puede volver a aprobar. Es lo que hace `reviseCampaign()` en el mock.

### 9.7 Campañas

Las campañas no se crean desde el frontend: nacen de una propuesta del agente estratega-redactor (sección 16), en estado `en_revision`.

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/campanas`, `/campanas/:id` | |
| GET | `/campanas/:id/inscripciones` | |
| POST | `/campanas/:id/inscribir` | Cuerpo: arreglo de ids de contacto. La campaña debe estar `aprobada`, `activa` o `pausada` (si no, 400 "La campaña requiere aprobación"). Cada contacto debe ser elegible, no ser de una empresa cliente y cumplir las reglas del segmento; si uno falla, 400 "Contacto no elegible o fuera del segmento" y no se inscribe ninguno. Los ya inscritos se omiten. Inscripciones nuevas en `pausada`. |
| POST | `/campanas/:id/reanudar` | Exige aprobación vigente ("La campaña requiere aprobación vigente") y Gmail `conectada` ("Gmail está desconectado"). Pasa la campaña a `activa` y después activa sus inscripciones sin `motivo_salida` cuyos contactos siguen siendo candidatos, con `proximo_envio` = ahora (el programador respeta la ventana). |
| POST | `/campanas/:id/pausar` | Campaña e inscripciones `pausada`, sin `proximo_envio`. Cancela sus mensajes `programado`. |
| POST | `/campanas/:id/detener` | Campaña `finalizada`; inscripciones `finalizada` con `motivo_salida: manual`. Cancela lo programado. |

### 9.8 Respuestas

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/respuestas`, `/respuestas/:id` | Pendientes primero. |
| PATCH | `/respuestas/:id` | `{clasificacion}`. La guarda con `clasificacion_confirmada = true`. Si es `baja`: supresión del correo (`origen: respuesta`) y respuesta `atendida`. Si es `no_interesado`: finaliza la inscripción (`respondio`) y el contacto pasa a `descartado`. Si es `interesado`, `pide_informacion` o `quiere_reunion`: el contacto avanza a `interesado`. Las demás dejan la secuencia en pausa. |
| POST | `/respuestas/:id/enviar-respuesta` | `{texto}` de 3 o más caracteres. Si el contacto está suprimido o excluido: 400 "El contacto está suprimido". Crea un mensaje `respuesta_manual` en el mismo hilo (`en_respuesta_a`) y encola su envío; la respuesta queda `atendida`. |
| POST | `/respuestas/:id/atender` | Marca `atendida`. |
| POST | `/respuestas/:id/reunion` | Finaliza las inscripciones del contacto con `motivo_salida: reunion` (`detener_envios(..., 'reunion', true)`), crea la interacción `reunion` y mueve el contacto a `discovery`. |

### 9.9 Oportunidades

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/oportunidades` | |
| POST | `/oportunidades` | Crea con lo recibido (ignora `id` y `creadaEn`). Si llega desde una respuesta, guarda `contacto_id`, `campana_origen_id` y `respuesta_origen_id` cuando se puedan deducir. |
| PATCH | `/oportunidades/:id` | `{etapa}` y demás campos. `cliente` o `perdido` fijan `cerrada_en`. Al pasar a `cliente`: el disparador vuelve cliente a la empresa y detiene envíos; además, los contactos no excluidos de esa empresa pasan a etapa `cliente` (evento `oportunidad_ganada`). Al pasar a `propuesta`: el contacto de la oportunidad, si hay, pasa a `propuesta`. |

### 9.10 Configuración

| Método | Ruta | Comportamiento |
|---|---|---|
| GET/PUT | `/configuracion/marca` | Objeto completo. Al guardarla, el disparador revoca las aprobaciones vigentes; aplica la revisión de campañas (9.6) a las `activa`, `aprobada` y `pausada`. |
| GET/PUT | `/configuracion/limites` | Valida `desde < hasta` y `separacionSegundos[0] <= separacionSegundos[1]` ("Revise el orden de horas y separación"). |
| GET/PUT | `/configuracion/cliente-ideal` | Clave `cliente_ideal` en la base. Al guardarla, encola el recálculo de puntajes. |

### 9.11 Integraciones y supresiones

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/integraciones` | Sin credenciales. |
| POST | `/integraciones/:id/conectar` | Prueba la conexión real y deja `conectada` o `error` con un `detalle` claro. `gmail`: exige credenciales guardadas (ver abajo) y prueba `users.getProfile`. `claude`: una llamada mínima con el modelo ligero. `verificador`: consulta la cuenta de Hunter. `ga4` y `search_console`: una consulta de un día con la cuenta de servicio. `formulario`: confirma que existe el secreto del webhook. |
| POST | `/integraciones/:id/desconectar` | Deja `desconectada`. `gmail`: revoca el token en Google, borra las credenciales y pausa todas las campañas activas y sus inscripciones (como el mock). |
| GET | `/integraciones/gmail/autorizar` | Inicia el OAuth de Gmail para la cuenta de envío (`gmail.send` y `gmail.readonly`, `access_type=offline`). |
| GET | `/integraciones/gmail/callback` | Guarda el token de actualización **cifrado** en `integraciones.credenciales_cifradas`, verifica que la cuenta sea `GMAIL_CUENTA_ENVIO`, marca `conectada` y redirige a `/configuracion`. |
| GET | `/supresiones` | |
| POST | `/supresiones` | `{correo}` o `{dominio}`, más `motivo`. Si no trae ninguno: 400 "Indique un correo o dominio". El disparador hace el resto. |

### 9.12 Auditoría y desarrollo

| Método | Ruta | Comportamiento |
|---|---|---|
| GET | `/auditoria?entidad=&entidadId=&actor=&desde=&hasta=` | Más reciente primero. Sin filtros, las últimas 500 filas. |
| POST | `/dev/reset` | Solo con `APP_ENV=development`: vacía los datos de negocio y carga los datos de demostración. En cualquier otro entorno, 404. |

### 9.13 Métricas diarias (`/informes/metricas`)

Devuelve filas `MetricaDiaria` para cada día entre `desde` y `hasta` (hora de Bogotá):

- **Una fila por día y campaña con envíos**: `fuente: "correo"`, `campanaId`, `segmentoId`, `enviados` y `respuestas` (de `v_metricas_correo_diarias`), `visitas` = sesiones de GA4 cuya `campana_utm` coincide con el `utm_campaign` de esa campaña, `leads` = envíos del formulario que llegaron por un enlace de esa campaña, `clientes` = oportunidades ganadas ese día con `campana_origen_id` de esa campaña.
- **Una fila por día y canal web** (todos menos `correo`): `fuente` = canal, `visitas` = sesiones, `leads` = conversiones `generate_lead` de ese canal, `enviados` y `respuestas` en 0.
- **`costoUSD`**: el total de `costos` del día, convertido a USD con `TASA_COP_POR_USD` cuando el monto está en pesos, **solo en la primera fila de cada día** y `null` en las demás, para que la suma del frontend no lo duplique. Si un día tiene costos pero ninguna fila, agrega una fila `fuente: "directo_desconocido"` en ceros con ese costo.

## 10. Autenticación y seguridad

- **Sesión:** token aleatorio de 32 bytes en una cookie `nexus_sesion` con `HttpOnly`, `Secure`, `SameSite=Lax` y `Path=/`. En la base solo se guarda su SHA-256 (`sesiones.token_hash`). Expira a las 12 horas; revocable al cerrar sesión. Actualiza `administradores.ultimo_acceso`.
- **Administrador inicial:** se crea desde `ADMIN_CORREO` en el primer arranque, si no existe.
- **CSRF:** las mutaciones exigen `Content-Type: application/json` y un encabezado `Origin` igual a `APP_URL`.
- **Credenciales:** cifradas con AES-GCM (o Fernet) usando `CLAVE_CIFRADO`, que vive en el gestor de secretos y nunca en la base ni en el repositorio. Nunca aparecen en respuestas ni en registros.
- **Lector web seguro (contra SSRF):** solo `http` y `https`; resuelve el DNS y bloquea direcciones privadas, de loopback, link-local y de metadatos; vuelve a comprobar en cada redirección (máximo 3); tamaño máximo de 2 MB; solo `text/html` o `text/plain`; tiempo máximo de 15 s; respeta `robots.txt`; agente de usuario propio con URL de contacto; máximo una petición cada 2 s por dominio.
- **Contenido externo:** el texto de sitios web y de correos recibidos es dato no confiable. Va a los agentes delimitado como dato, con la instrucción de no seguir órdenes que contenga. Los agentes no tienen herramientas que escriban, envíen o aprueben.
- **Rutas públicas** (`/api/webhooks/formulario`, `/r/...`, `/baja/...`): límite de peticiones por IP, sin datos personales en la URL, sin revelar si un registro existe.
- **Registros:** sin tokens, sin cuerpos de correo y con los correos enmascarados (`la***@dominio.co`).
- **Documentación del API** (`/api/docs`) solo en desarrollo.
- **Acceso de desarrollo:** con `APP_ENV=development` y `AUTH_DEV_BYPASS=true` (ambos), `/auth/google` inicia sesión directamente como el administrador inicial. Esa ruta no debe existir en otro entorno, y una prueba lo comprueba.

## 11. Motor de secuencias

Es un proceso sin IA. Toda su lógica está en `app/motor/` y se prueba con un reloj falso.

### 11.1 Programador (cada minuto, tarea `programar_envios`)

1. Toma las inscripciones `activa` con `proximo_envio <= ahora`, de campañas `activa`.
2. Para cada una, verifica en este orden y, si algo falla, reprograma o detiene con su motivo:
   - Dentro de la **ventana** de la secuencia **y** de la ventana global de `limites` (días según `getDay`, 0 = domingo; horas en Bogotá). Si no, `proximo_envio` = inicio de la siguiente ventana válida.
   - **Topes del día** (hoy en Bogotá): `limites.topeDiarioNuevos` (envíos de paso 1 en todas las campañas), `campanas.tope_diario` (paso 1 de esa campaña) y `limites.topePorDominio` (envíos al mismo dominio). Si se alcanzó, pasa a la siguiente ventana.
   - Que el hilo no tenga una respuesta sin registrar: para pasos en el mismo hilo, consulta el hilo en Gmail justo antes de programar. Si el contacto respondió, registra la respuesta y no envía.
3. Crea el mensaje `programado` con `clave_idempotencia = "<inscripcion_id>:<paso>:<aprobacion_id>"`, el contenido renderizado **desde `contenido_congelado` de la aprobación vigente** (nunca desde la secuencia en edición) y `programado_para` = ahora + una separación al azar entre `separacionSegundos[0]` y `[1]` después del último envío programado.
4. Encola `enviar_mensaje` con `clave_unica = "enviar:<mensaje_id>"`.

### 11.2 Envío (tarea `enviar_mensaje`)

1. Si el paso va en el mismo hilo, vuelve a consultar el hilo en Gmail: si el contacto respondió desde que se programó, registra la respuesta y no envía. Después, en una transacción, pasa el mensaje de `programado` a `enviando`. El disparador vuelve a revisar elegibilidad, inscripción, campaña y aprobación; si falla, el mensaje queda `cancelado` y la tarea termina sin error.
2. Fuera de la transacción: envía con `users.messages.send`.
3. En otra transacción: `enviado`, `enviado_en`, `gmail_message_id`, `gmail_thread_id`. Avanza la inscripción: `paso_actual + 1` y `proximo_envio` según la espera del siguiente paso, o `finalizada` con `motivo_salida: completada` si era el último. Primer envío al contacto → etapa `contactado`. Registra el envío en la auditoría.
4. **Si el proceso se cae entre 2 y 3**, el mensaje queda en `enviando`. Antes de reintentarlo, busca en Gmail con `rfc822msgid:` el `Message-ID` del mensaje y, si no aparece, revisa los enviados de las últimas 24 horas buscando el encabezado `X-Nexus-Idempotencia`. Si existe, solo registra el envío. Si no, lo envía.
5. Errores: temporales (429, 5xx) → reintento a 1, 5 y 30 minutos (`terminar_tarea` ya lo hace) y luego pausa con alerta; permanentes de destinatario → mensaje `fallido` y correo `invalido`; token de Gmail vencido o revocado (`invalid_grant`, 401) → integración `error` y pausa de **todas** las campañas activas.

### 11.3 Formato del correo

- Texto plano UTF-8, sin imágenes ni píxel de apertura.
- `From` con nombre visible de la marca y la cuenta `GMAIL_CUENTA_ENVIO`.
- `Message-ID` propio y estable derivado de la clave (`<hash-de-la-clave@dominio-de-envio>`), y el encabezado `X-Nexus-Idempotencia: <clave>`.
- Pasos con `mismo_hilo`: mismo asunto (con "Re: "), `In-Reply-To` y `References` del mensaje anterior, y `threadId` en la llamada.
- `List-Unsubscribe: <https://APP_URL/baja/<token>>, <mailto:GMAIL_CUENTA_ENVIO?subject=BAJA>` y `List-Unsubscribe-Post: List-Unsubscribe=One-Click`.

### 11.4 Variables y plantillas

- Variables del sistema: `{{firma}}` (marca), `{{baja}}` (texto de baja de la marca + enlace a `/baja/<token>`) y `{{enlace}}` (llamada a la acción: enlace de seguimiento a `DIAGNOSTICO_URL`).
- Variables de datos: `{{nombre}}`, `{{empresa}}`, `{{cargo}}`, `{{municipio}}`, `{{senal}}`. **Solo se usan si su evidencia está `verificado`**; si no, se usa el `respaldo` definido en `pasos.variables`, y si no hay, los respaldos del mock ("equipo", "su empresa", "trabaja con empresas grandes de la región").
- El render de la previsualización y el del envío son **la misma función**.

### 11.5 Días hábiles y ventanas

- `espera_dias_habiles` cuenta de lunes a viernes, sin festivos de Colombia (`holidays`).
- La siguiente fecha de envío es el primer momento válido después de la espera: día permitido, dentro de las horas y no festivo.

### 11.6 Alertas automáticas

- Si los rebotes duros del día superan `limites.umbralRebotePausa` % de los envíos del día (con al menos 10 envíos), o llega una queja de spam: pausa todas las campañas activas, registra la causa en la auditoría y deja la integración `gmail` con `detalle` explicativo.

## 12. Rastreador de Gmail

Tarea `sincronizar_gmail` cada 2 minutos, actor `proceso:rastreador_gmail`.

- Usa `users.history.list` desde el `historyId` guardado en `integraciones.cursor_sincronizacion`. Si Google responde que el cursor es muy viejo, resincroniza los últimos 7 días con `messages.list`.
- Cada mensaje nuevo se clasifica, en este orden:
  1. **Rebote**: remitente `mailer-daemon` o `postmaster`, o `multipart/report; report-type=delivery-status`. Encuentra el mensaje original por `X-Nexus-Idempotencia` en las cabeceras adjuntas o por el hilo. Estado `5.x.x` → `eventos_correo` `rebote_duro`. `4.x.x` → `rebote_suave`: reintenta el mismo paso hasta dos veces en días distintos y, si persiste, pausa la inscripción.
  2. **Respuesta automática**: `Auto-Submitted` distinto de `no`, `X-Autoreply`, `X-Autorespond`, `Precedence: auto_reply`, o asunto típico de fuera de oficina. Registra `fuera_oficina`; no cuenta como respuesta; mueve el siguiente paso a la fecha de regreso si se puede leer, o a 3 días hábiles.
  3. **Respuesta del contacto**: mensaje en un hilo con un `gmail_thread_id` de `mensajes`, de un remitente que no es la cuenta de envío. Inserta en `respuestas` con el texto sin la parte citada (el disparador pausa al instante) y encola `clasificar_respuesta`. Si el texto limpio es exactamente "baja" (sin distinguir mayúsculas ni tildes), crea la supresión de inmediato, sin esperar al agente.
- Todo es idempotente por `gmail_message_id` (único en la base).

## 13. Rutas públicas: formulario, clics y bajas

### 13.1 Webhook del formulario (`POST /api/webhooks/formulario`)

El backend del sitio web (otro repositorio) llamará a esta ruta. Documenta en el `README` cómo hacerlo, con un ejemplo de código.

- Autenticación: encabezados `X-Nexus-Fecha` (epoch en segundos, máximo 5 minutos de diferencia) y `X-Nexus-Firma` = HMAC-SHA256 en hexadecimal de `fecha + "." + cuerpo_crudo` con `FORMULARIO_WEBHOOK_SECRETO`. Firma inválida → 401.
- Cuerpo:

```json
{
  "id": "id-unico-del-envio",
  "formulario": "diagnostico",
  "enviadoEn": "2026-11-16T14:05:00-05:00",
  "nombre": "Andrés Cárdenas",
  "correo": "andres@empaquessaman.co",
  "telefono": "+57 300 000 0000",
  "cargo": "Gerente",
  "empresa": "Empaques Samán S.A.S.",
  "nit": "900123456",
  "municipio": "Palmira",
  "autorizacion": { "aceptada": true, "texto": "Texto exacto que aceptó", "versionPolitica": "2026-11" },
  "utm": { "source": "nexus-pm", "medium": "email", "campaign": "cam-1a2b3c4d", "content": "paso-1", "term": null },
  "gclid": null,
  "fbclid": null,
  "ref": "referencia-opaca-del-enlace",
  "pagina": "https://nexusinnovacion.com/diagnostico",
  "referrer": "https://www.google.com/",
  "respuestas": { "...": "respuestas completas del diagnóstico" }
}
```

- Proceso, en una transacción con actor `proceso:webhook_formulario`:
  1. Inserta en `envios_formulario`. Si `id_externo` ya existe, responde `{ok: true}` sin repetir nada.
  2. Busca o crea la empresa (por NIT, luego por dominio del correo si no es un dominio de correo gratuito como gmail.com u hotmail.com, luego por nombre normalizado y municipio; un parecido no exacto crea un posible duplicado, no una fusión).
  3. Busca o crea el contacto por correo, con evidencias `fuente: formulario`, `estado: verificado`.
  4. Si aceptó la autorización: base legal `autorizacion_formulario` con el texto exacto, `envio_formulario_id` y `prueba` = "Formulario <tipo> <id> del <fecha>". **Queda pendiente de aprobación del administrador.**
  5. `origen`: `correo` si `ref` o `utm.source = nexus-pm`; `pagado` si hay `gclid`, `fbclid` o un `utm.medium` de pago; `organico_redes` si el medio es social; `organico_busqueda` si el `referrer` es un buscador; si no, `registro`.
  6. Si `ref` corresponde a un enlace de seguimiento: une el envío con el mensaje y la inscripción, finaliza esa inscripción (`motivo_salida: completada`) y registra el evento en la auditoría como `diagnostico_completado`.
  7. Interacción `formulario` y etapa `lead` (solo avanza, nunca retrocede).
  8. Encola `verificar_correo` y `enriquecer_empresa`.

### 13.2 Redirector de clics (`GET /r/<referencia>`)

Busca `enlaces_seguimiento`, registra `eventos_correo` tipo `clic` con `automatico = true` si llega antes de 60 segundos del envío, por `HEAD`, o con un agente de usuario de escáner conocido. Redirige con 302 a `url_destino` con los UTM y `ref=<referencia>`. Un clic humano a la llamada a la acción mueve al contacto de `contactado` a `interesado`. Referencia desconocida → redirige a `DIAGNOSTICO_URL` sin error visible.

Cada enlace usa `utm_source=nexus-pm`, `utm_medium=email`, `utm_campaign=cam-<primeros 8 caracteres del id de campaña>`, `utm_content=paso-<n>` y una referencia aleatoria de al menos 22 caracteres.

### 13.3 Baja (`/baja/<token>`)

- El token firma con `itsdangerous` el id del mensaje. No contiene datos personales.
- `GET`: página HTML sencilla, en español, con un botón "Confirmar baja". No da de baja por sí sola, porque los escáneres abren enlaces.
- `POST` (botón o `List-Unsubscribe=One-Click`): inserta `eventos_correo` tipo `baja` (el disparador suprime y detiene) y muestra la confirmación. Idempotente.

## 14. Etapas del embudo

Cada cambio de etapa lo dispara un evento, con `nexus.evento` fijado. Las etapas solo avanzan, salvo las salidas (`excluido`, `descartado`, `perdido`) y los cambios manuales del administrador por `PATCH /contactos/:id`.

| De | A | Evento |
|---|---|---|
| identificado | contactable | Correo `valido` y base legal aprobada (`contactable`). |
| contactable | contactado | Primer correo enviado (`primer_envio`). |
| contactado | interesado | Respuesta positiva confirmada o clic humano a la llamada a la acción (`respuesta_positiva`, `clic_cta`). |
| cualquiera anterior | lead | Envío del formulario (`formulario`). |
| lead | calificado | Puntaje no incompleto y mayor o igual a `UMBRAL_CALIFICADO` (`calificado_por_puntaje`). |
| cualquiera anterior | discovery | Reunión registrada (`reunion`). |
| discovery | propuesta | Oportunidad en `propuesta` (`propuesta_enviada`). |
| cualquiera | cliente | Oportunidad ganada (`oportunidad_ganada`). |
| — | excluido | Supresión, rebote duro o exclusión (lo hace la base). |
| — | descartado | Respuesta `no_interesado` confirmada (`no_interesado`). |

## 15. Calidad de datos

- **Normalización:** correo en minúsculas y sin espacios; dominio a partir del sitio web (sin `www.`, sin ruta); razón social sin "S.A.S.", "SAS", "Ltda.", tildes ni signos para comparar (la original se guarda tal cual); teléfonos a formato internacional con `+57` por defecto.
- **Duplicados exactos:** mismo NIT o mismo dominio = misma empresa; mismo correo = mismo contacto. En importaciones y webhooks se actualiza el existente conservando todas las evidencias.
- **Posibles duplicados** (tarea `buscar_duplicados`, diaria y al crear una empresa): nombre normalizado con similitud ≥ 0,88 (`rapidfuzz`) en el mismo municipio → fila en `posibles_duplicados`. Nunca se fusionan solos.
- **Verificación de correo** (tarea `verificar_correo`): sintaxis; registros MX con `dnspython` (sin MX ni A → `invalido`); luego Hunter. Correos de rol (`info@`, `ventas@`, `contacto@`...) y `catch_all` quedan como `riesgoso` o `catch_all`, nunca `valido`. Guarda cada resultado en `verificaciones_correo` (el disparador actualiza al contacto). Si Hunter no está conectado o se agotó su plan, el resultado es `desconocido`. La traducción de los estados de Hunter vive en una sola función con pruebas.
- **Puntaje** (tarea `calcular_puntaje`): reglas transparentes en código, no IA. Un catálogo de criterios con id, peso y regla (por ejemplo: tamaño entre 11 y 200, municipio del Valle del Cauca, señal de proveedor de empresa grande, sector prioritario), relacionados con los textos de `configuracion.cliente_ideal`. Un criterio sin datos tiene `cumple: null` y marca el puntaje como `incompleto`. Guarda cada cálculo en `puntajes` con `version_reglas` = versión de la configuración.

## 16. Agentes de IA

### 16.1 Ejecutor común

- SDK `anthropic`. Modelos por variable de entorno: `CLAUDE_MODELO_LIGERO` (por defecto `claude-haiku-5-5`) para clasificar, y `CLAUDE_MODELO_MAYOR` (por defecto `claude-sonnet-5-5`) para investigar y redactar.
- **Salidas estructuradas** con `client.messages.parse(..., output_format=ModeloPydantic)`: la respuesta llega validada en `parsed_output`. Ten en cuenta que el esquema JSON no admite restricciones de longitud ni numéricas; el SDK las mueve a la descripción y las valida localmente. Vuelve a validar con Pydantic antes de guardar.
- Cada agente lee su instrucción activa de `prompts` (versionada). Escribe tú las versiones 1 de los cuatro prompts, en español, y cárgalas en una migración de datos.
- Cada ejecución queda en `ejecuciones_agente`: prompt, modelo, entrada, salida, estado, tokens (`input_tokens`, `output_tokens` y los de caché del objeto `usage`), búsquedas web (`usage.server_tool_use.web_search_requests`), duración y `costo_usd`. El costo se calcula con precios por millón de tokens de `CLAUDE_PRECIOS_JSON`, que el administrador llena con los precios vigentes; no los escribas fijos en el código. Cada búsqueda web suma `COSTO_BUSQUEDA_WEB_USD` (por defecto 0,01, es decir, 10 USD por cada 1.000).
- **Topes:** `TOPE_USD_POR_TAREA` corta una ejecución y la deja `requiere_revision`; `TOPE_MENSUAL_USD` detiene las tareas de agentes del mes y deja la integración `claude` en `error` con el motivo.
- **Fallos:** un reintento; si vuelve a fallar, `requiere_revision`.
- **Los agentes no escriben directo en tablas de negocio.** Escriben propuestas (`propuestas` + `propuesta_versiones` + `evidencias`) o, en los casos permitidos abajo, campos vacíos con evidencia verificada.

### 16.2 Verificador de citas (sin IA)

Toda cita que un agente entregue se comprueba así: el lector web seguro vuelve a abrir la URL, extrae el texto, normaliza espacios y Unicode (NFC) en ambos lados, y busca la cita como subcadena exacta. Si aparece: `estado: verificado` y `cita_verificada_en`. Si no: `inferido`, o se descarta si el campo exige verificación. La base no deja marcar como verificado un dato de agente sin esa fecha.

### 16.3 Investigador

- **Enriquecimiento** (`enriquecer_empresa`): el lector web seguro trae como máximo 10 páginas **del dominio de la empresa** (inicio, nosotros, servicios, clientes, contacto, sitemap). El modelo mayor extrae campos y señales; cada dato con URL y cita textual exacta. El verificador de citas comprueba. Los datos verificados se guardan sin aprobación **solo si el campo estaba vacío**; cualquier cambio sobre un dato ya existente es una propuesta `enriquecimiento`. Un correo personal deducido es una propuesta `correo_inferido` (nunca aprobable). Sitio caído: dos reintentos y luego "sin fuente". Fuentes contradictorias: no elige, crea una propuesta para revisión.
- **Descubrimiento** (`descubrir_empresas`): el modelo mayor usa la herramienta de búsqueda web del API (`web_search_20250305`, `max_uses` de 10, `user_location` en Colombia, `blocked_domains` con linkedin.com, facebook.com, instagram.com y tiktok.com, porque sus términos no permiten este uso). Una segunda llamada estructurada convierte lo hallado en una lista de empresas con su sitio y sus citas. Después, sin IA: lector web y verificador de citas sobre el sitio de cada empresa, deduplicación contra `empresas` (`posible_duplicado_de`), y escritura de `resultados_descubrimiento`, `personas_descubiertas` (solo de páginas públicas de la propia empresa) y `evidencias`. Actualiza `progreso` de la búsqueda.

### 16.4 Segmentador

Lleva el sector en texto libre a la lista estándar, redacta la justificación del puntaje (el número lo calcula la regla) y propone segmentos: crea el segmento en estado `propuesto` con `propuesto_por: agente` y una propuesta `segmento` que lo apunta. Sector ambiguo → "Otro" y nota para el administrador.

### 16.5 Estratega-redactor

- **Proponer campaña** (`proponer_campana`, sobre un segmento aprobado): crea la secuencia con sus pasos, la campaña en `en_revision` y la propuesta `campana` con la versión 1 = secuencia completa. Debe pasar `validarSecuencia` antes de guardarse; si no pasa, el agente corrige una vez y si sigue fallando la propuesta se guarda con los errores para revisión. Usa solo variables con datos verificados disponibles y define el respaldo de cada una. Si el segmento no tiene suficientes contactos con base legal aprobada, no propone envío automático y lo explica.
- **Revisar campaña** (`revisar_campana`): tras un rechazo, crea una versión nueva que atiende el comentario.

### 16.6 Analista

- **Respuestas** (`clasificar_respuesta`, modelo ligero): clasificación, confianza de 0 a 1, nombres y fechas mencionados y un borrador de respuesta. La guarda sin confirmar. Solo aplica acciones protectoras automáticas con confianza ≥ 0,85: `baja` → supresión; `fuera_oficina` → reprogramación. Todo lo demás espera la confirmación del administrador. **Nunca** envía una respuesta.
- **Informe semanal** (`informe_semanal`, lunes 7:00 en Bogotá): compara campañas, segmentos y pasos con las cifras de la base y propone recomendaciones con su `sustento`. Con muestras pequeñas (menos de 30 envíos) lo dice y no recomienda cambios.

## 17. Analítica

- **GA4** (`sincronizar_ga4`, diaria a las 6:10 en Bogotá): Data API con una cuenta de servicio de solo lectura. Dimensiones `date`, `sessionDefaultChannelGroup`, `sessionSource`, `sessionMedium`, `sessionCampaignName`; métricas de sesiones, usuarios y los eventos `generate_lead` y `whatsapp_click`. Canal → `canal`: Organic Search → `organico_busqueda`; Organic Social → `organico_redes`; Paid Search, Paid Social, Display y Cross-network → `pagado`; Email o `source = nexus-pm` → `correo`; Referral → `referencia`; el resto → `directo_desconocido`. Reimporta los últimos 4 días con upsert, porque GA4 ajusta los datos recientes.
- **Search Console** (`sincronizar_search_console`, diaria): `searchanalytics.query` por fecha, consulta y página; reimporta los últimos 5 días.
- Ambos son datos agregados, nunca por persona.

## 18. Cola de tareas y procesos programados

| Tarea | Cuándo | Prioridad |
|---|---|---|
| `enviar_mensaje` | Al programar un mensaje | 2 |
| `sincronizar_gmail` | Cada 2 min | 2 |
| `programar_envios` | Cada minuto | 3 |
| `procesar_formulario` (si el webhook lo difiere) | Al recibirlo | 3 |
| `clasificar_respuesta` | Al registrar una respuesta | 3 |
| `verificar_correo` | Contacto nuevo o correo cambiado | 5 |
| `enriquecer_empresa`, `calcular_puntaje`, `buscar_duplicados` | Empresa nueva o cambiada; diaria | 6 |
| `descubrir_empresas`, `proponer_segmentos`, `proponer_campana`, `revisar_campana` | A pedido | 6 |
| `sincronizar_ga4`, `sincronizar_search_console` | Diaria | 7 |
| `informe_semanal` | Lunes 7:00 | 8 |
| `liberar_tareas_colgadas` | Cada 5 min (función de la base) | 1 |

- Las tareas periódicas se encolan con `clave_unica` por intervalo (por ejemplo `sincronizar_gmail:2026-11-16T14:32`), así dos programadores nunca duplican una tarea.
- El trabajador toma tareas con `tomar_tareas(nombre_del_trabajador, 5)` y termina cada una con `terminar_tarea`. Un manejador por tipo, con tiempo máximo por tipo.
- Todo manejador es idempotente: si se ejecuta dos veces, el resultado es el mismo.

## 19. Variables de entorno

Documenta todas en `.env.example` y valídalas al arrancar con `pydantic-settings` (si falta una obligatoria, el proceso no arranca).

```
APP_ENV=production                 # development | production
APP_URL=https://app.nexusinnovacion.com
DATABASE_URL=postgresql://...
CLAVE_CIFRADO=                     # 32 bytes en base64, desde el gestor de secretos
SESION_SECRETO=                    # para firmar tokens de baja y estado de OAuth
ADMIN_CORREO=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
GOOGLE_DOMINIO_PERMITIDO=nexusinnovacion.com
GMAIL_CUENTA_ENVIO=
AUTH_DEV_BYPASS=false
ANTHROPIC_API_KEY=
CLAUDE_MODELO_LIGERO=claude-haiku-5-5
CLAUDE_MODELO_MAYOR=claude-sonnet-5-5
CLAUDE_PRECIOS_JSON=               # {"modelo": {"entrada": USD/M, "salida": USD/M, ...}}
COSTO_BUSQUEDA_WEB_USD=0.01
TOPE_USD_POR_TAREA=0.50
TOPE_MENSUAL_USD=30
HUNTER_API_KEY=
GOOGLE_CUENTA_SERVICIO_JSON=       # ruta o contenido, para GA4 y Search Console
GA4_PROPIEDAD_ID=
SEARCH_CONSOLE_SITIO=
FORMULARIO_WEBHOOK_SECRETO=
DIAGNOSTICO_URL=https://nexusinnovacion.com/diagnostico
TASA_COP_POR_USD=4000
UMBRAL_CALIFICADO=70
```

Los valores de topes, tasa y umbral son iniciales: déjalos así y anótalos en `DECISIONES.md` como valores a confirmar por NEXUS.

## 20. Pruebas

Las pruebas corren contra **PostgreSQL 16 real** (contenedor de Docker), nunca SQLite. Ninguna prueba llama a servicios reales: usa las implementaciones falsas.

1. **Esquema:** aplica las migraciones a una base nueva y corre `nexus_publicity_manager_pruebas.sql`; deben pasar los 37 casos.
2. **Contrato:** un script en `scripts/validar_contrato.ts` que se ejecuta dentro del frontend adjunto (`npx tsx`) valida contra `src/api/schemas.ts` las respuestas JSON reales de **cada** ruta GET del backend, guardadas por las pruebas. Ninguna puede fallar.
3. **Rutas:** una prueba por ruta, con el caso feliz y cada error documentado en la sección 9.
4. **Reglas portadas:** los casos de `src/lib/reglas.test.ts` (`coincide`, `motivoInscripcion`, `validarSecuencia`) dan el mismo resultado en Python.
5. **Motor**, con reloj falso: ventanas, festivos de Colombia, topes diarios y por dominio, separación al azar, render con y sin evidencia verificada, avance de pasos, fin de secuencia.
6. **Sin duplicados:** dos trabajadores a la vez, caída entre el envío y el registro, reintento después de la caída. En todos los casos sale un solo correo por paso.
7. **Rastreador:** rebote duro y suave, fuera de oficina, respuesta en el hilo, respuesta "BAJA", cursor vencido.
8. **Rutas públicas:** firma del webhook válida, inválida y vencida; envío repetido; clic humano y de escáner; baja por GET (no da de baja) y por POST.
9. **Agentes**, con Claude falso: salida válida, salida inválida, cita que no aparece en la página, tope de costo superado, correo inferido.
10. **Seguridad:** rutas sin sesión → 401; correo fuera del dominio o no registrado → rechazado; `AUTH_DEV_BYPASS` no funciona en producción; las credenciales nunca aparecen en ninguna respuesta; el lector web rechaza IP privadas.
11. **Escenario de aceptación del MVP:** una campaña de 50 contactos ficticios corre de principio a fin con Gmail falso y reloj falso. Se verifica: cero envíos duplicados, ningún seguimiento después de una respuesta, rebotes y bajas aplicados al instante, topes y ventanas respetados, y métricas de la campaña que cuadran con lo enviado.

Además: `ruff` y `mypy` sin errores.

## 21. Plan de trabajo por etapas

Entrega por etapas. Cada etapa debe quedar funcionando, con sus pruebas pasando, antes de empezar la siguiente. Al final de cada una, informa qué archivos cambiaron, qué pruebas corriste y su resultado real.

| Etapa | Contenido | Termina cuando |
|---|---|---|
| 1. Base | Proyecto, configuración, migración `0001`, conexión con actor, manejo de errores, serializador camelCase sin nulos, autenticación y sesión, Docker. | Pasan las 37 pruebas del esquema y el frontend real inicia sesión (con el acceso de desarrollo). |
| 2. Datos | Empresas, contactos, importación, base legal, supresiones, duplicados exactos, verificación de correo, auditoría, configuración, integraciones (sin Gmail). | El frontend sin mocks navega Contactos, Empresas, Configuración y Auditoría sin errores de formato. |
| 3. Campañas | Segmentos, secuencias, previsualización, propuestas, aprobación con contenido congelado, revisión de campañas, inscripción, pausar, reanudar y detener. | Validación de contrato en verde para esas rutas. |
| 4. Envío | OAuth de Gmail, motor, envío, rastreador, enlaces, bajas, webhook del formulario, etapas del embudo, respuestas y oportunidades. | Pasa el escenario de aceptación del MVP. |
| 5. Agentes | Ejecutor común, lector web seguro, verificador de citas, Investigador y Estratega-redactor; después Analista y Segmentador. | Pasan las pruebas de agentes con Claude falso y una prueba manual documentada con Claude real. |
| 6. Medición | GA4, Search Console, costos, métricas diarias, panel, embudo, informe semanal, duplicados por similitud, puntaje. | Informes y Panel muestran cifras que cuadran con la base. |
| 7. Cierre | `README`, `DECISIONES.md`, `VALIDACION.md`, revisión de seguridad. | Todo lo de la sección 20 en verde. |

## 22. Cambios permitidos en el frontend

No modifiques el frontend salvo estos dos cambios, y documéntalos en `DECISIONES.md`:

1. En `src/api/schemas.ts` y `src/types/index.ts`, `Respuesta.campanaId` pasa a opcional: una respuesta a un correo escrito a mano no pertenece a ninguna campaña. Ajusta los usos que lo necesiten.
2. En la pantalla de Configuración, el botón "Conectar" de Gmail navega a `${API_URL}/integraciones/gmail/autorizar` en lugar de hacer el POST, porque el permiso de Gmail exige pasar por Google.

Para usar el backend real, el frontend se compila con `VITE_USE_MOCKS=false` y sin `VITE_API_URL` (mismo origen, `/api`).

## 23. Lo que no debes hacer

- No envíes ningún correo sin pasar por el motor y por el disparador `guardian_mensaje`.
- No des a ningún agente una herramienta que envíe correos, apruebe propuestas o modifique campañas.
- No completes datos sin evidencia ni marques como verificado algo cuya cita no se comprobó.
- No apruebes una base legal sin una acción explícita del administrador.
- No extraigas datos de LinkedIn, Facebook, Instagram ni TikTok.
- No pongas datos personales en URLs, registros ni nombres de archivo.
- No borres ni edites filas de `registro_auditoria`, y no desactives disparadores para que algo funcione.
- No cambies la forma de ninguna respuesta del contrato ni envíes `null` en campos opcionales.
- No uses Redis, Celery, microservicios ni Kubernetes.
- No dejes credenciales, claves o precios fijos en el código.
- No digas que una prueba pasa si no la ejecutaste. Si tu entorno no permite ejecutar algo, dilo y entrega los comandos exactos para correrlo.

## 24. Cómo entregar cada respuesta

1. Antes de escribir código, un resumen de una página: cómo entendiste el sistema, las dudas que resolviste con supuestos y el plan de la etapa.
2. El código completo de la etapa (archivos completos, no fragmentos), o un `.zip` del repositorio si tu entorno lo permite.
3. Los comandos para correrlo y los resultados reales de las pruebas.
4. Lo que queda pendiente para la siguiente etapa.