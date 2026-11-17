# Nexus Publicity Manager — frontend y backend integrado

Aplicación React + FastAPI + PostgreSQL 16, con API y frontend en el mismo origen. Incluye el frontend adjunto, el esquema SQL original, migraciones, autenticación Google, datos, campañas, motor de correo, agentes, analítica, trabajador y pruebas.

**Estado de entrega:** implementación integrada para instalación y validación. Los resultados ejecutados están en `VALIDACION.md`. No se certifica como producción: faltan pruebas con las cuentas reales, el recorrido de navegador y los casos pendientes descritos en `DECISIONES.md`. No se enviaron correos reales durante el desarrollo.

## Arranque rápido con Docker

Requisitos: Docker con Compose y Python 3 para generar secretos. Desde esta carpeta:

```bash
python scripts/entorno_desarrollo.py
docker compose up --build -d
docker compose exec app python scripts/demo.py
```

Abra http://localhost:8000 y pulse el acceso de Google. En desarrollo, `AUTH_DEV_BYPASS=true` crea una sesión local del administrador sin llamar a Google. Los datos de ejemplo no tienen correos verificados ni bases legales aprobadas; Gmail empieza desconectado.

`entorno_desarrollo.py` genera claves y contraseñas aleatorias, no sobrescribe un `.env` existente y restringe sus permisos. No publique `.env`. El Compose arranca PostgreSQL, aplica las migraciones con `nexus_owner`, crea `nexus_app` con privilegios limitados y arranca API y trabajador con ese rol. El puerto solo se expone en localhost.

```bash
docker compose logs -f app
docker compose exec app python scripts/encolar.py buscar_duplicados
```

El Dockerfile compila el frontend con `VITE_USE_MOCKS=false`; no define `VITE_API_URL`. El frontend usa `/api`. Si antes utilizó MSW en ese mismo origen, elimine el service worker de demostración desde las herramientas del navegador.

## Instalación sin Docker

Requisitos: Python 3.12, PostgreSQL 16+, Node 24 y npm. Cree una base vacía con un usuario propietario de migraciones.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock
pip install --no-deps -e .
python scripts/entorno_desarrollo.py
```

Edite `DATABASE_URL` en `.env` para apuntar inicialmente al dueño de esa base. Ejecute:

```bash
alembic upgrade head
python scripts/crear_rol.py
```

Cambie después `DATABASE_URL` al usuario `nexus_app` y su `DB_APP_PASSWORD`. Compile el frontend:

```bash
cd frontend
npm ci
VITE_USE_MOCKS=false npm run build
cd ..
python scripts/demo.py
honcho start
```

En Windows puede usar Docker o activar `.venv\Scripts\activate`; para variables de entorno use la sintaxis de PowerShell. La API se sirve en el puerto 8000. `/health` comprueba PostgreSQL; `/api/docs` solo existe en desarrollo.

## Configuración de producción

Defina `APP_ENV=production`, `APP_URL=https://su-dominio`, `AUTH_DEV_BYPASS=false`, los secretos y las credenciales reales. La aplicación rechaza producción con bypass o HTTP. Use HTTPS mediante un proxy, respaldos de PostgreSQL y un gestor de secretos. Ejecute migraciones con el propietario y la aplicación con `nexus_app`. `requirements.lock` registra las versiones de Python utilizadas; `frontend/package-lock.json` fija las dependencias web.

La base no puede modificarse desde las propuestas de un modelo: los agentes entregan datos estructurados y propuestas; solo las rutas de administrador aprueban. Gmail se utiliza exclusivamente desde el motor. El trabajador inicia campañas únicamente si se activaron explícitamente y existe aprobación vigente.

### Google: inicio de sesión

1. Cree un cliente OAuth de tipo aplicación web en Google Cloud y configure la pantalla de consentimiento de Workspace.
2. Registre `APP_URL/api/auth/google/callback` y `APP_URL/api/integraciones/gmail/callback` como URI de redirección exactas.
3. Configure `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_DOMINIO_PERMITIDO` y `ADMIN_CORREO`.
4. El administrador inicial se crea al arrancar. Otros administradores deben existir y estar activos en `nexus.administradores`.

La sesión tiene una duración de 12 horas. La cookie es HttpOnly, SameSite=Lax y Secure en producción. Solo se guarda su hash SHA-256. OAuth usa state, nonce y PKCE; valida email verificado y dominio de Workspace.

### Gmail

Active Gmail API, configure `GMAIL_CUENTA_ENVIO` y use el botón Conectar de Gmail. El flujo pide `gmail.send` y `gmail.readonly`, verifica la cuenta y cifra el refresh token con Fernet. No introduzca tokens directamente en el frontend.

Para enviar una campaña: apruebe bases legales y segmento, revise y apruebe la propuesta, inscriba contactos elegibles y active la campaña. La aprobación por sí sola no inscribe ni envía.

Un envío ambiguo queda en `enviando`, pausa campañas y requiere conciliación. El motor consulta Gmail por Message-ID y encabezado de idempotencia; si encuentra el mensaje, registra el resultado sin reenviarlo. Una ausencia en la búsqueda no se trata como prueba de no entrega. Revise `mensajes.ultimo_error` y la auditoría; no cambie estados manualmente para forzar un reenvío.

### Claude

Configure `ANTHROPIC_API_KEY`, `CLAUDE_MODELO_LIGERO`, `CLAUDE_MODELO_MAYOR` y `CLAUDE_PRECIOS_JSON` con modelos disponibles y sus precios actuales por millón de tokens. Los nombres predeterminados provienen del prompt; su disponibilidad no se comprobó con una cuenta real.

Estructura de precios, reemplazando los valores por cifras actuales:

```json
{"identificador-del-modelo":{"entrada":1.0,"salida":2.0,"cache_lectura":0.1,"cache_escritura":1.25}}
```

Los números anteriores solo ilustran la estructura, no son precios vigentes. Sin precios configurados no se ejecutan agentes. El gasto incluye tokens y búsquedas; se reserva presupuesto antes de llamar, se guarda el uso informado y se detiene la aplicación de resultados que excedan el tope. Las reservas de llamadas fallidas se conservan para revisión conservadora.

Las búsquedas parten de la pantalla Descubrimiento. Las tareas que no tienen botón en el frontend original se encolan por CLI:

```bash
python scripts/encolar.py proponer_segmentos
python scripts/encolar.py proponer_campana --datos '{"segmentoId":"UUID-del-segmento-aprobado"}'
python scripts/encolar.py enriquecer_empresa --datos '{"empresaId":"UUID-de-la-empresa"}'
python scripts/encolar.py informe_semanal
```

No hay herramientas del modelo que envíen o aprueben. Las citas se vuelven a consultar y se comparan por texto normalizado. El lector bloquea IP privadas, destinos de metadatos, protocolos y puertos no permitidos; fija la IP resuelta durante la conexión TLS para evitar DNS rebinding. Respeta robots.txt y limita tamaño, tiempo y frecuencia.

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

## Variables

Todas están enumeradas en `.env.example` y validadas en `app/config.py`. Las obligatorias son APP_URL, DATABASE_URL, CLAVE_CIFRADO, SESION_SECRETO y ADMIN_CORREO; producción exige además OAuth Google. Las credenciales de cada integración se requieren al conectarla, no para utilizar el resto de la aplicación.

`TOPE_USD_POR_TAREA=0.50`, `TOPE_MENSUAL_USD=30`, `TASA_COP_POR_USD=4000` y `UMBRAL_CALIFICADO=70` son valores iniciales a confirmar con NEXUS. Los topes de correo y ventanas se administran desde Configuración. Todas las fechas de negocio usan America/Bogota.

## Pruebas

Las pruebas crean y eliminan una base temporal aleatoria. `TEST_ADMIN_DATABASE_URL` debe apuntar a una instancia de pruebas y un usuario con permiso CREATEDB; no use una instancia de producción.

```bash
TEST_ADMIN_DATABASE_URL=postgresql://usuario:clave@localhost:5432/postgres pytest -q
ruff check app
mypy app
cd frontend
npx tsx scripts/validar_contrato.ts
VITE_API_URL=http://localhost/api VITE_USE_MOCKS=true npm test -- --maxWorkers=2
npm run lint
VITE_USE_MOCKS=false npm run build
```

El `VITE_API_URL` absoluto es solo para los tests del frontend, que construyen Request de Node; no lo use al compilar la aplicación de mismo origen. El script de contratos valida las respuestas reales guardadas por pytest, no datos del mock.

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
- `migraciones`: original SQL, seguridad OAuth y prompts/contadores de agentes.
- `frontend`: aplicación adjunta con los dos cambios funcionales permitidos.
- `pruebas`, `scripts`, `sql`: verificación, instalación y originales.
# Nexus-publicity-manager-bakend
