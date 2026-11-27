# Guía de Railway: migraciones, API, trabajador, bajas y buzón de control

Proyecto Railway: **Nexus-publicity-manager-BAKEND** (`9464edf0-09ed-46ae-af91-0667b65e22b5`), entorno `production`.

| Servicio | Papel | Variable que lo usa |
| --- | --- | --- |
| `Nexus-publicity-manager-bakend` | API + trabajador (un contenedor, `honcho start`) | — |
| `Postgres` | Base principal (todo NEXUS) | `DATABASE_URL` del backend |
| `Postgres-3qtE` (con volumen `postgres-volume-eVor`) | Base de bajas | `UNSUBSCRIBE_DATABASE_URL` del backend |

No borre ni recree ninguno de los dos Postgres ni sus volúmenes: los pasos de abajo solo añaden tablas.

## 0. Requisitos

Si su versión de la CLI no acepta `railway ssh --service … -- comando`, abra la consola del servicio desde el panel de Railway y ejecute el mismo comando ahí.

```bash
npm i -g @railway/cli        # o brew install railway
railway login
railway link --project 9464edf0-09ed-46ae-af91-0667b65e22b5 --environment production
```

## 1. Variables del servicio del backend

```bash
# Base de bajas: referencia a la variable del servicio Postgres-3qtE (red privada).
railway variables --service Nexus-publicity-manager-bakend \
  --set 'UNSUBSCRIBE_DATABASE_URL=${{Postgres-3qtE.DATABASE_URL}}'

# Origen público de los enlaces «Rechazar / Darme de baja». Opcional: vacío usa APP_URL.
# Con APP_URL en Vercel funciona porque vercel.json reescribe /baja/* al backend; si
# prefiere no depender de Vercel para las bajas, use el dominio de Railway:
railway variables --service Nexus-publicity-manager-bakend \
  --set 'BAJA_URL_PUBLICA=https://nexus-publicity-manager-bakend-production.up.railway.app'
```

Compruebe además (ya existen en el servicio): `DATABASE_URL` apunta al servicio `Postgres` (no a `Postgres-3qtE`), `RAILWAY_DOCKERFILE_PATH=Dockerfile.railway`, `APP_URL` es el dominio público del frontend, y `GMAIL_CUENTA_ENVIO` es la cuenta que envía.

## 2. Migraciones (cada base por separado)

**Base principal** (crea la 0009: listas, plantillas, bandeja de bajas, conciliación):

```bash
# Automático en cada despliegue: Settings → Deploy → Pre-deploy command
alembic upgrade head && python scripts/migrar_bajas.py
# Manual, dentro del contenedor:
railway ssh --service Nexus-publicity-manager-bakend -- alembic upgrade head
railway ssh --service Nexus-publicity-manager-bakend -- alembic current     # debe decir 0009 (head)
```

**Base de bajas** (crea `bajas.usuarios_baja`; su propia tabla de versiones `alembic_version_bajas`):

```bash
railway ssh --service Nexus-publicity-manager-bakend -- python scripts/migrar_bajas.py --estricto
# equivalente directo:
railway ssh --service Nexus-publicity-manager-bakend -- alembic -c alembic_bajas.ini upgrade head
```

`scripts/migrar_bajas.py` sin `--estricto` nunca bloquea un despliegue: si `Postgres-3qtE` está apagado, avisa y sigue; las bajas siguen surtiendo efecto en la base principal y quedan «sincronización pendiente» hasta que la secundaria responda y esté migrada.

Bajada (solo si hace falta volver atrás): `alembic downgrade 0008` (conserva la audiencia de las campañas que usaban una lista creando un segmento con sus miembros) y `alembic -c alembic_bajas.ini downgrade base`.

## 3. Arranque de la API y del trabajador

El servicio usa `Dockerfile.railway`, cuyo `Procfile` (copia de `Procfile.railway`) tiene dos procesos:

```
api: sh -c 'exec uvicorn app.main:crear_app --factory --host 0.0.0.0 --port ${PORT:-8000} --no-access-log'
trabajador: python -m app.tareas.trabajador
```

Start command (Settings → Deploy): **`honcho start`** — arranca los dos. Si alguna vez se cambia por un `uvicorn …` solo, la API responde pero nadie programa ni envía (la pestaña Envíos lo dice: «El trabajador no da señales»).

Alternativa con dos servicios (más aislamiento): duplique el servicio, ponga en uno `Start command = sh -c 'exec uvicorn app.main:crear_app --factory --host 0.0.0.0 --port $PORT --no-access-log'` y en el otro `python -m app.tareas.trabajador` (sin dominio público), con las mismas variables.

Comprobar por separado:

```bash
curl -fsS https://nexus-publicity-manager-bakend-production.up.railway.app/health     # API: {"estado":"ok"}
railway logs --service Nexus-publicity-manager-bakend | grep -E "api.1|trabajador.1" | tail
```

En la aplicación: **Terminal** muestra el último latido del trabajador; cada secuencia tiene la pestaña **Envíos** con «Trabajador: activo / sin señales».

## 4. Gmail

Los registros de Railway del 25-nov muestran, cada 2 minutos, `sincronizar_gmail` fallando con «la API no está habilitada en su proyecto de Google Cloud». Con ese error el trabajador pausa todas las campañas al tercer fallo seguido y los envíos fallan con 403. Antes de probar:

1. Google Cloud Console (el mismo proyecto del cliente OAuth) → APIs y servicios → Biblioteca → **Gmail API** → Habilitar. Espere unos minutos.
2. En NEXUS: Configuración → Integraciones → Gmail → **Probar conexión** (o Conectar de nuevo).
3. En la secuencia: pestaña Envíos → **Programar envíos** (o «Reanudar y programar» si quedó pausada).

## 5. URL pública de baja

Cada correo lleva «Rechazar / Darme de baja: https://…/baja/<token>» en texto y HTML (aunque la plantilla omita el pie) y la cabecera `List-Unsubscribe`. Compruebe desde fuera, sin sesión:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "https://<BAJA_URL_PUBLICA o APP_URL>/baja/token-invalido"   # 400 con página «No pudimos procesar este enlace»
```

Un GET solo muestra «Confirmar baja» (un escáner no da de baja a nadie); la baja ocurre con el botón (POST). Repetirlo es idempotente.

## 6. Verificar un envío a un buzón de control

Use solo un buzón propio y autorizado (nunca un cliente):

```bash
railway ssh --service Nexus-publicity-manager-bakend -- \
  python scripts/prueba_buzon_control.py --correo control@nexusinnovacion.com --confirmo-buzon-de-control
```

El script crea una lista con ese único contacto, una secuencia de 1 correo con la ventana del horario global y la aprueba y programa como el botón Aprobar. Imprime el enlace de la secuencia. En la pestaña **Envíos**: «Programado» → «Entregándose a Gmail» → «Aceptado por Gmail» (solo con el id que devuelve Gmail). El correo sale en la siguiente ventana hábil, respetando separación y cuotas.

Para probar la baja con ese correo: abra el enlace del pie, confirme, y compruebe:

```bash
railway connect Postgres-3qtE        # abre psql en la base de bajas
SELECT correo, origen, dado_de_baja_en, sincronizado_en FROM bajas.usuarios_baja;
```

En NEXUS: Configuración → Cumplimiento → Lista de supresión muestra «efectiva · sincronización pendiente» o «efectiva · sincronizada». Para reproducir la prueba con la secundaria apagada: detenga `Postgres-3qtE` (en Railway, retire su despliegue activo desde la pestaña Deployments; el volumen se conserva), confirme la baja, vea «pendiente», vuelva a desplegarlo y en uno o dos minutos pasa a «sincronizada» con una sola fila. La misma prueba, automatizada y sin Gmail real, está en `pruebas/test_zzzzz_flujo_aceptacion.py`.
