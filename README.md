# Nexus Publicity Manager — backend (API, trabajador y frontend integrado)

Aplicación React + FastAPI + PostgreSQL 16. Este repositorio contiene la API, el trabajador, el esquema SQL, migraciones, integraciones y pruebas. El frontend vive en su propio repositorio/ZIP; Docker lo compila y lo sirve desde el mismo origen que la API.

**Estado de entrega:** los resultados ejecutados están en `VALIDACION.md` y los límites conocidos en `DECISIONES.md`. No se enviaron correos reales durante el desarrollo.

## Estructura de carpetas esperada

Extraiga los dos ZIP en la **misma carpeta**, sin renombrarlos:

```
mi-carpeta/
  Nexus-publicity-manager-bakend-main/   ← este backend (Nexus-publicity-manager-bakend-main.zip)
  Nexus-publicity-manager-main/          ← frontend (Nexus-publicity-manager-main.zip)
```

También funciona con clones de Git (`Nexus-publicity-manager-bakend` y `Nexus-publicity-manager`) o con el frontend en `frontend/` dentro del backend: `python scripts/entorno_desarrollo.py` busca la carpeta cuyo `package.json` se llama `nexus-publicity-manager` y escribe su ruta en `.env` (`FRONTEND_DIR` y `FRONTEND_DIST`). Si está en otro lugar, edite esas dos variables.

## Arranque rápido con Docker

Requisitos: Docker con Compose v2.17 o superior (BuildKit) y Python 3 para generar el `.env`. Desde `Nexus-publicity-manager-bakend-main/`:

```bash
python scripts/entorno_desarrollo.py      # crea .env con claves y detecta el frontend
docker compose up --build -d
docker compose exec app python scripts/demo.py
```

Abra http://localhost:8000 y pulse el acceso de Google. En desarrollo, `AUTH_DEV_BYPASS=true` crea una sesión local del administrador sin llamar a Google. Los datos de ejemplo no tienen correos verificados ni bases legales aprobadas; Gmail empieza desconectado.

Compose compila el frontend tomando `FRONTEND_DIR` (por defecto `../Nexus-publicity-manager-main`) como contexto adicional de construcción, arranca PostgreSQL, aplica las migraciones con `nexus_owner`, crea `nexus_app` con privilegios limitados y arranca API y trabajador con ese rol. El puerto solo se expone en localhost. `entorno_desarrollo.py` no sobrescribe un `.env` existente (solo actualiza la ruta del frontend) y restringe sus permisos; no publique `.env`.

Sin Compose: `docker build --build-context frontend=../Nexus-publicity-manager-main -t nexus .`

```bash
docker compose logs -f app
docker compose exec app python scripts/encolar.py buscar_duplicados
```

La imagen compila el frontend con `VITE_USE_MOCKS=false` y `VITE_API_URL=/api` (mismo origen, sin CORS). Si antes utilizó la demostración con MSW en ese mismo origen, elimine el service worker desde las herramientas del navegador.

## Instalación sin Docker

Requisitos: Python 3.12, PostgreSQL 16+, Node 22.12+ y npm. Cree una base vacía con un usuario propietario de migraciones. Desde `Nexus-publicity-manager-bakend-main/`:

```bash
python -m venv .venv
source .venv/bin/activate                 # Windows: .venv\Scripts\activate
pip install -r requirements.lock
pip install --no-deps -e .
python scripts/entorno_desarrollo.py      # .env + FRONTEND_DIR/FRONTEND_DIST
```

Edite `DATABASE_URL` en `.env` para apuntar inicialmente al dueño de esa base y ejecute:

```bash
alembic upgrade head
python scripts/crear_rol.py
```

Cambie después `DATABASE_URL` al usuario `nexus_app` y su `DB_APP_PASSWORD`. Compile el frontend **en su propia carpeta**:

```bash
cd ../Nexus-publicity-manager-main
npm ci
VITE_USE_MOCKS=false npm run build        # PowerShell: $env:VITE_USE_MOCKS="false"; npm run build
cd ../Nexus-publicity-manager-bakend-main
python scripts/demo.py
honcho start
```

El backend sirve la carpeta indicada en `FRONTEND_DIST` (relativa a este backend, por ejemplo `../Nexus-publicity-manager-main/dist`); si queda vacía, usa `frontend/dist` dentro del backend (como en Docker). La API se sirve en el puerto 8000. `/health` comprueba PostgreSQL; `/api/docs` solo existe en desarrollo.

Para desarrollar el frontend con Vite contra este backend, defina `APP_URL=http://localhost:5173` y siga el README del frontend (Vite reenvía `/api`, `/baja`, `/r` y `/health`).

### Railway + Vercel

**Migración `0009` y base de bajas (27 de noviembre de 2026):** ver `GUIA_RAILWAY.md` (comandos exactos) y `ENTREGA.md` (causa del fallo de envío y cambios). Pre-deploy command: `alembic upgrade head && python scripts/migrar_bajas.py`. Variables nuevas: `UNSUBSCRIBE_DATABASE_URL` (servicio `Postgres-3qtE`) y `BAJA_URL_PUBLICA` (opcional).

`Dockerfile.railway` publica solo la API y el trabajador (`honcho start`, ver `GUIA_GEMINI.md`).

**Migración `0007` (23 de noviembre de 2026), obligatoria:** añade la clave `descubrimiento` a `configuracion` (meta diaria apagada) y las tablas `eventos_trabajador` (bitácora de la Terminal; se borra sola a los 3 días) y `latidos_trabajador`. Sin ella fallan Descubrimiento y Terminal, y el trabajador no arranca. La aplica `alembic upgrade head` en el *Pre-deploy command*. Se puede bajar con `alembic downgrade 0006`. La meta empieza cada día a la hora `DESCUBRIMIENTO_HORA` (por defecto 8, hora de Colombia).

**Migración `0006` (23 de noviembre de 2026), obligatoria:** añade la clave `cumplimiento` a `configuracion` (amplía su CHECK) con `{"aprobacionLegalAutomatica": false}`. Sin ella, la pantalla Configuración → Cumplimiento falla. La aplica `alembic upgrade head` en el *Pre-deploy command*. Se puede bajar con `alembic downgrade 0005`.

La versión anterior **añadió la migración `0005`**, que es **obligatoria** antes de arrancar el código nuevo:

- añade a `pasos` las columnas `formato` y `cuerpo_html` (con sus CHECK);
- añade `secuencias.archivada_en` y `mensajes.cuerpo_html`;
- duplica las secuencias compartidas por varias campañas;
- crea la propuesta interna de cada campaña;
- activa la versión 3 del prompt del Estratega-redactor.

No debilita ningún CHECK ni disparador. Si `0004` aún no estaba aplicada, `alembic upgrade head` aplica las dos.

En el servicio de Railway, en *Settings → Deploy → Pre-deploy command*, debe estar `alembic upgrade head` (con `DATABASE_URL` del propietario de las migraciones). La dependencia nueva `nh3` (limpieza del HTML de los correos con formato) se instala con el resto. En Railway, `APP_URL` debe ser el dominio público de Vercel de ese entorno. En Vercel, el destino del backend se define por entorno con `NEXUS_BACKEND_URL` (ver README del frontend); ya no hay una dirección fija en el repositorio.

Después de actualizar, el receptor del formulario respeta el estado de la integración «Formulario». Si el secreto está configurado y nadie había tocado esa integración, el primer arranque la deja conectada para no cortar la captación; una desconexión explícita del administrador siempre se respeta.

## Configuración de producción

Defina `APP_ENV=production`, `APP_URL=https://su-dominio`, `AUTH_DEV_BYPASS=false`, los secretos y las credenciales reales. La aplicación rechaza producción con bypass o HTTP. Use HTTPS mediante un proxy, respaldos de PostgreSQL y un gestor de secretos. Ejecute migraciones con el propietario y la aplicación con `nexus_app`. `requirements.lock` registra las versiones de Python utilizadas; el `package-lock.json` del frontend fija las dependencias web.

La base no puede modificarse desde las propuestas de un modelo: los agentes entregan datos estructurados y propuestas; solo las rutas de administrador aprueban. Gmail se utiliza exclusivamente desde el motor. El trabajador inicia campañas únicamente si se activaron explícitamente y existe aprobación vigente.

### Google: inicio de sesión

1. Cree un cliente OAuth de tipo aplicación web en Google Cloud y configure la pantalla de consentimiento de Workspace.
2. Registre `APP_URL/api/auth/google/callback` y `APP_URL/api/integraciones/gmail/callback` como URI de redirección exactas.
3. Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_DOMINIO_PERMITIDO` y `ADMIN_CORREO`.
4. El administrador inicial se crea al arrancar. Otros administradores deben existir y estar activos en `nexus.administradores`.

La sesión tiene una duración de 12 horas. La cookie es HttpOnly, SameSite=Lax y Secure en producción. Solo se guarda su hash SHA-256. OAuth usa state, nonce y PKCE; valida email verificado y dominio de Workspace.

### Gmail

Active Gmail API, configure `GMAIL_CUENTA_ENVIO` y use el botón Conectar de Gmail. El flujo pide `gmail.send` y `gmail.readonly`, verifica la cuenta y cifra el refresh token con Fernet. No introduzca tokens directamente en el frontend.

Para enviar una campaña: apruebe bases legales y la audiencia (segmento o lista), prepare la secuencia (a mano, con plantillas o con el agente) y pulse **Aprobar y programar**: en la misma operación se inscribe a cada destinatario elegible y se programa su primer correo. La pestaña **Envíos** de la secuencia muestra qué se programó, qué aceptó Gmail y qué falta (Gmail, trabajador, cuotas, elegibles). Se puede aprobar sin programar desmarcando la casilla. Los segmentos también se pueden pedir al agente («Pedir propuestas al agente» en Segmentos); el avance y el motivo de un fallo se ven en Propuestas.

Un envío ambiguo queda en `enviando`, pausa campañas y requiere conciliación. El motor consulta Gmail por Message-ID y encabezado de idempotencia; si encuentra el mensaje, registra el resultado sin reenviarlo. Una ausencia en la búsqueda no se trata como prueba de no entrega. Revise `mensajes.ultimo_error` y la auditoría; no cambie estados manualmente para forzar un reenvío.

### Claude

Configure `ANTHROPIC_API_KEY`, `CLAUDE_MODELO_LIGERO`, `CLAUDE_MODELO_MAYOR` y `CLAUDE_PRECIOS_JSON` con modelos disponibles y sus precios actuales por millón de tokens. Los nombres predeterminados provienen del prompt; su disponibilidad no se comprobó con una cuenta real.

Estructura de precios, reemplazando los valores por cifras actuales:

```json
{"identificador-del-modelo":{"entrada":1.0,"salida":2.0,"cache_lectura":0.1,"cache_escritura":1.25}}
```

Ejemplo con los precios publicados por Anthropic el 22 de noviembre de 2026 (USD por millón de tokens; compruébelos antes de usarlos). Para Haiku 5.5 son los de prompts de hasta 100.000 tokens, el caso de estos agentes:

```env
AGENTE_PROVEEDOR=claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODELO_LIGERO=claude-haiku-5-5
CLAUDE_MODELO_MAYOR=claude-sonnet-5-5
CLAUDE_PRECIOS_JSON={"claude-sonnet-5-5":{"entrada":2,"salida":10,"cache_lectura":0.10,"cache_escritura":2.50},"claude-haiku-5-5":{"entrada":0.10,"salida":0.50,"cache_lectura":0.01,"cache_escritura":0.125}}
```

La API de Claude se paga en la Claude Console (platform.claude.com), aparte de cualquier suscripción de claude.ai: compre créditos o, con un plan Max o Team, vincule sus créditos mensuales de API. La búsqueda web (USD 10 por 1.000 búsquedas, `COSTO_BUSQUEDA_WEB_USD=0.01`) debe estar habilitada para la organización en la Console. En Configuración → Integraciones, «Conectar» la API de Claude prueba la clave y también una búsqueda web, e indica el motivo si algo falta (créditos, búsqueda deshabilitada, modelo).

Los números anteriores solo ilustran la estructura, no son precios vigentes. Sin precios configurados no se ejecutan agentes. El gasto incluye tokens y búsquedas; se reserva presupuesto antes de llamar, se guarda el uso informado y se detiene la aplicación de resultados que excedan el tope. Las reservas de llamadas fallidas se conservan para revisión conservadora.

Las búsquedas parten de la pantalla Descubrimiento. Las tareas que no tienen botón en el frontend original se encolan por CLI:

```bash
python scripts/encolar.py proponer_segmentos
python scripts/encolar.py proponer_campana --datos '{"segmentoId":"UUID-del-segmento-aprobado"}'
python scripts/encolar.py enriquecer_empresa --datos '{"empresaId":"UUID-de-la-empresa"}'
python scripts/encolar.py informe_semanal
```

No hay herramientas del modelo que envíen o aprueben. Las citas se vuelven a consultar y se comparan por texto normalizado. El lector bloquea IP privadas, destinos de metadatos, protocolos y puertos no permitidos; fija la IP resuelta durante la conexión TLS para evitar DNS rebinding. Respeta robots.txt y limita tamaño, tiempo y frecuencia.

### Descubrimiento gratuito: Tavily + Gemini

Sin pagar a Google ni a Anthropic: Gemini en el plan gratuito para todos los agentes y Tavily como buscador de Descubrimiento (la búsqueda en Google de Gemini 3 no existe en el plan gratuito).

1. Cree una cuenta gratuita en app.tavily.com (1.000 créditos al mes, sin tarjeta) y copie su clave `tvly-...`.
2. En el backend y el trabajador:
   ```env
   AGENTE_PROVEEDOR=gemini
   GEMINI_API_KEY=su clave del plan gratuito
   TAVILY_API_KEY=tvly-...
   ```
3. En Configuración → Integraciones, «Conectar» la API de IA prueba Gemini y Tavily (usa 1 crédito) y debe decir «Descubrimiento usará Tavily (búsqueda comprobada)».

Cada búsqueda de Descubrimiento hace hasta `TAVILY_CONSULTAS_MAX` consultas (1 crédito cada una; con 4, unas 250 búsquedas al mes), descarta redes sociales y directorios, lee la página de contacto de cada sitio con el lector propio (respeta robots.txt) y el agente extrae nombre, municipio y correo corporativo de esas páginas. Como siempre, cada cita se comprueba en el sitio antes de guardar. Con `BUSCADOR_DESCUBRIMIENTO=proveedor` se vuelve a la búsqueda del modelo aunque haya clave de Tavily. En el plan gratuito de Gemini, Google puede usar lo que se envía para mejorar sus productos (incluido el texto de las respuestas que clasifica el agente); revíselo con su política de datos.

### Hunter

Configure `HUNTER_API_KEY` y conecte el verificador en Configuración. Antes se comprueban sintaxis y DNS. Sin proveedor disponible el correo queda desconocido. Correos de rol y catch-all no se convierten en válidos automáticamente.

### GA4 y Search Console

Configure una cuenta de servicio de solo lectura en `GOOGLE_CUENTA_SERVICIO_JSON` (contenido JSON o ruta a un secreto montado), `GA4_PROPIEDAD_ID` y `SEARCH_CONSOLE_SITIO`. Comparta la propiedad de GA4 y Search Console con el email de esa cuenta, habilite sus APIs y conecte ambas integraciones. El trabajador reimporta cuatro días de GA4 y cinco de Search Console; los datos son agregados.

### Webhook del formulario

Configure `FORMULARIO_WEBHOOK_SECRETO` en ambos servidores. Envíe desde el backend del sitio a `APP_URL/api/webhooks/formulario`, nunca exponga el secreto en el navegador. La firma se calcula sobre los bytes exactos enviados:

```python
import hashlib, hmac, json, os, time
import httpx
payload = {
    "id": "identificador-unico-del-formulario",
    "formulario": "diagnostico",
    "enviadoEn": "2026-11-17T10:00:00-05:00",
    "nombre": "Persona ficticia",
    "correo": "persona@empresa.example",
    "empresa": "Empresa ficticia",
    "municipio": "Palmira",
    "autorizacion": {"aceptada": True, "texto": "Texto exacto aceptado", "versionPolitica": "v1"},
    "respuestas": {}, "utm": {}
}
raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
fecha = str(int(time.time()))
firma = hmac.new(os.environ["FORMULARIO_WEBHOOK_SECRETO"].encode(),
                 fecha.encode() + b"." + raw, hashlib.sha256).hexdigest()
r = httpx.post(os.environ["APP_URL"] + "/api/webhooks/formulario", content=raw,
    headers={"Content-Type":"application/json", "X-Nexus-Fecha":fecha, "X-Nexus-Firma":firma}, timeout=20)
r.raise_for_status()
```

Reintente con el mismo `id` si falla la red: no duplica el registro. La firma vence en cinco minutos. La autorización del formulario siempre queda pendiente de aprobación administrativa.

El receptor solo guarda datos con la integración «Formulario» conectada (Configuración → Integraciones, donde también aparece la dirección exacta del webhook con un botón para copiarla). Desconectada, cualquier envío —aunque esté bien firmado— recibe 503 y no crea contactos; el sitio puede reintentarlo más tarde con el mismo `id`.

## Variables

Todas están enumeradas en `.env.example` y validadas en `app/config.py`. Las obligatorias son APP_URL, DATABASE_URL, CLAVE_CIFRADO, SESION_SECRETO y ADMIN_CORREO; producción exige además OAuth Google. Las credenciales de cada integración se requieren al conectarla, no para utilizar el resto de la aplicación.

`TOPE_USD_POR_TAREA=0.50`, `TOPE_MENSUAL_USD=30`, `TASA_COP_POR_USD=4000` y `UMBRAL_CALIFICADO=70` son valores iniciales a confirmar con NEXUS. Los topes de correo y ventanas se administran desde Configuración. Todas las fechas de negocio usan America/Bogota.

## Pruebas

Las pruebas crean y eliminan una base temporal aleatoria. `TEST_ADMIN_DATABASE_URL` debe apuntar a una instancia de pruebas y un usuario con permiso CREATEDB; no use una instancia de producción.

```bash
TEST_ADMIN_DATABASE_URL=postgresql://usuario:clave@localhost:5432/postgres pytest -q
ruff check app && ruff format --check app
mypy app
cd ../Nexus-publicity-manager-main
npx tsx scripts/validar_contrato.ts ../Nexus-publicity-manager-bakend-main/pruebas/salidas/contratos.json
npm test
npm run lint
VITE_USE_MOCKS=false npm run build
```

`pruebas/test_correcciones.py` cubre los defectos corregidos: importación CSV, conversiones en días sin envíos, costos con dos campañas el mismo día, horario global incompatible, auditoría más allá de 500 acciones, desconexión real con proveedor de prueba, bajas, respuestas solo HTML, recuperación de Gmail tras una desconexión larga, informe semanal por proveedor y rendimiento de listados y puntajes.

Prueba de arranque desde la estructura que recibe el usuario: extrae los dos ZIP en una carpeta temporal, prepara `.env`, valida `docker compose config`, ejecuta las pruebas del backend, instala, prueba y compila el frontend, migra una base temporal, arranca la API y comprueba `/health`, la aplicación y la API.

```bash
TEST_ADMIN_DATABASE_URL=postgresql://usuario:clave@localhost:5432/postgres \
  python scripts/probar_arranque.py ruta/al/Nexus-publicity-manager-bakend-main.zip ruta/al/Nexus-publicity-manager-main.zip
```

Para los 37 casos originales, use una base adicional vacía:

```bash
DATABASE_URL=postgresql://usuario:clave@localhost:5432/nexus_esquema alembic upgrade 0001
DATABASE_URL=postgresql://usuario:clave@localhost:5432/nexus_esquema python scripts/probar_esquema.py
```

Para el recorrido de navegador, instale Playwright (`npm install --no-save playwright`) y Chromium; defina `CHROMIUM_PATH` si no está en `/usr/bin/chromium`, y ejecute `python scripts/prueba_navegador.py` con DATABASE_URL de pruebas. Crea una base independiente, arranca la API real y comprueba las 13 pantallas. No inicia el trabajador ni conecta proveedores.

## Estructura

- `app/api`: rutas, serialización de entidades y transacciones de negocio.
- `app/seguridad`: OAuth, sesión, cifrado.
- `app/dominio`: reglas de segmento, horarios, normalización y puntaje.
- `app/motor`: programación, MIME, envío, conciliación, rastreo y alertas.
- `app/agentes`: modelos estructurados, presupuesto, lector y tareas de agentes.
- `app/integraciones`: Gmail, Hunter, GA4 y Search Console; adaptadores falsos para pruebas.
- `app/publico`: bajas, enlaces de seguimiento y formulario firmado.
- `app/tareas`: cola PostgreSQL, programación y trabajador.
- `migraciones`: original SQL, seguridad OAuth, prompts/contadores de agentes (0004) el prompt v2 del Estratega-redactor para rutas de correo (0005) el editor de secuencias: formato HTML, archivo, una secuencia por campaña, propuesta interna por campaña y prompt v3, (0006) la política de aprobación automática de la base legal y (0007) la meta diaria y la Terminal.
- El frontend está en su propia carpeta (`Nexus-publicity-manager-main`); Docker lo compila desde `FRONTEND_DIR`.
- `pruebas`, `scripts`, `sql`: verificación, instalación y originales.
