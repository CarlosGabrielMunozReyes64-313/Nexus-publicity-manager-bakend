# Búsqueda automática de empresas de Colombia con Gemini

Actualización del backend `Nexus-publicity-manager-bakend`. El frontend, el inicio de sesión, los datos existentes y las funciones de Claude se conservan. No requiere borrar tablas ni recrear PostgreSQL. No añade migraciones.

## Qué incorpora

- Gemini con búsqueda en Google y extracción JSON validada.
- Búsqueda diaria opcional, a partir de las 08:00 de Colombia. Si el trabajador arranca después, programa los lotes pendientes del día.
- Por defecto, cuatro lotes de hasta cinco empresas: máximo 20 altas automáticas diarias. Rota entre los 32 departamentos y Bogotá, sin restringir sector. Es cobertura progresiva; no un censo completo de todas las empresas ni una garantía de 20 resultados.
- Guarda empresas nuevas en Empresas, resultados y evidencias en Descubrimiento, y buzones corporativos genéricos publicados en Contactos. Cada valor usado automáticamente debe aparecer en una cita consultada en el sitio de la empresa; también exige evidencia de Colombia.
- No inventa correos ni nombres de personas. Acepta buzones como ventas@ o contacto@ del mismo dominio; otros quedan fuera de la incorporación automática. Si no encuentra correo admisible, puede guardar solo la empresa.
- Evita duplicados por dominio, nombre/municipio y correo; respeta supresiones. No sobrescribe empresas existentes.
- Los contactos ingresan como identificados, con base legal pendiente. Esta búsqueda no aprueba contactos, campañas ni envíos.
- Mantiene el presupuesto por tarea y mensual. Registra Gemini en Costos como «otro», con concepto «Gemini: investigador», para conservar el esquema actual. Las reservas y el consumo detallado quedan en ejecuciones de agente.

## 1. Incorporar los archivos

Este paquete NO se ha publicado en GitHub: la conexión devolvió `403 Resource not accessible by integration` al intentar crear una rama.

El ZIP incluye únicamente los archivos nuevos/modificados, una guía y un parche. Si usas Git local, desde la carpeta del backend y con tus cambios guardados:

```bash
git switch -c gemini-descubrimiento
git apply --check /ruta/nexus-gemini.patch
git apply /ruta/nexus-gemini.patch
git add app pruebas/test_gemini.py .env.example GUIA_GEMINI.md
git commit -m "Añadir Gemini y descubrimiento automático de empresas de Colombia"
git push -u origin gemini-descubrimiento
```

Abre un pull request hacia `main` y revisa los cambios. Si `git apply --check` indica un conflicto, no fuerces la aplicación: hay cambios posteriores que deben conservarse.

Alternativa desde GitHub: descomprime el ZIP y utiliza **Add file → Upload files** en la raíz del repositorio backend. Sube las carpetas `app` y `pruebas` y los archivos `.env.example` y `GUIA_GEMINI.md` conservando sus rutas. No subas el ZIP ni el parche como si fueran el código. Revisa el diff antes de confirmar. El archivo `app/seguridad/auth.py` no forma parte del cambio.

## 2. Variables en Railway (backend)

Conserva las variables que ya tienes. Añade estas en **Variables** del backend:

| Nombre | Valor |
| --- | --- |
| `AGENTE_PROVEEDOR` | `gemini` |
| `GEMINI_API_KEY` | Tu clave de Google AI Studio |
| `GEMINI_MODELO` | `gemini-3.5-flash-lite` |
| `DESCUBRIMIENTO_AUTOMATICO` | `true` |
| `DESCUBRIMIENTO_LIMITE_DIARIO` | `20` |
| `DESCUBRIMIENTO_HORA` | `8` |

La clave va solamente en Railway; no en Vercel, GitHub ni una variable `VITE_`. Obtenerla: https://aistudio.google.com/apikey

El proyecto de Google necesita acceso a Gemini y a Google Search grounding; verifica cuota y facturación. Tener una clave no garantiza que esa función esté habilitada.

Se incluyen precios configurables de referencia (consultados el 19 de noviembre de 2026): entrada USD 0.30 y salida USD 2.50 por millón de tokens; búsqueda USD 0.014 por consulta. No se descuenta ningún cupo gratuito en la estimación conservadora. Si Google cambia tarifas o cambias modelo, actualiza `GEMINI_PRECIOS_JSON` y `GEMINI_COSTO_BUSQUEDA_USD`.

```text
GEMINI_PRECIOS_JSON={"gemini-3.5-flash-lite":{"entrada":0.30,"salida":2.50}}
GEMINI_COSTO_BUSQUEDA_USD=0.014
TOPE_USD_POR_TAREA=0.50
TOPE_MENSUAL_USD=30
```

Estos topes son controles internos estimados, no un tope de facturación garantizado por Google. Se reserva por adelantado un margen de 20 consultas por ejecución de descubrimiento, y se concilia con el número de consultas reportadas y los tokens, incluido pensamiento. Una llamada puede superar la reserva; si excede el tope se detiene la aplicación del resultado y queda para revisión. Los fallos conservan la reserva para evitar reintentos ilimitados. Configura también controles de gasto/cuota en Google.

## 3. Activar el trabajador

En tu captura, el comando de inicio ejecutaba solo Uvicorn. Eso atiende la web, pero no procesa búsquedas en segundo plano.

La imagen `Dockerfile.railway` existente ya incluye Honcho y copia `Procfile.railway` como `Procfile`. En **Settings → Deploy → Custom Start Command**, cambia el comando por:

```text
honcho start
```

Mantén `PORT=8000` y el healthcheck `/health`. Honcho arrancará la API y el trabajador existentes juntos. También procesará las demás tareas ya autorizadas de la aplicación; las reglas de aprobación de campañas permanecen intactas. No actives otro trabajador separado si ya tienes uno funcionando y prefieres conservar esa instalación.

Alternativa si ya separas procesos: conserva Uvicorn en la API y ejecuta `python -m app.tareas.trabajador` en un servicio trabajador con el mismo código, base de datos y variables; sin dominio público ni healthcheck HTTP en ese servicio.

Aplica los cambios y despliega la versión nueva. No necesita cambios en Vercel.

## 4. Comprobar funcionamiento

1. Comprueba que `/health` continúe respondiendo `{"estado":"ok"}`.
2. En Railway → Deploy Logs deben arrancar `api` y `trabajador` si elegiste Honcho.
3. Después de la hora configurada, entra en Descubrimiento. Deben aparecer búsquedas de departamentos de Colombia. Se crean una sola vez por día, incluso al reiniciar.
4. Las empresas corroboradas aparecen en Empresas; los correos genéricos admisibles, en Contactos. Los demás resultados quedan pendientes o se omiten por falta de evidencia.
5. Si la búsqueda termina sin altas, revisa resultados/evidencias. No se fuerza a Gemini a inventar datos para llenar el cupo.
6. Si falla, revisa clave, cuota, acceso a búsqueda, modelo, presupuesto y disponibilidad de los sitios. Los errores de proveedor no incluyen la clave.

Para parar nuevas búsquedas, cambia `DESCUBRIMIENTO_AUTOMATICO=false` y despliega. Las tareas que ya estaban en cola pueden terminar. Para volver a Claude, usa `AGENTE_PROVEEDOR=claude` con sus claves y precios existentes.

## Validación y límites

Resultados locales (19 de noviembre de 2026):

- 21 pruebas específicas de Gemini, agentes y lector web: correctas.
- Ruff y mypy sobre `app`: correctos.
- Regresión del backend: 46 correctas y un fallo en `test_mvp_50_contactos`. La versión original, sin este cambio, presenta el mismo fallo (38 correctas y ese mismo fallo). No se modificó el motor de envíos.
- Se excluyó `test_frontend_real` de la comparación porque este repositorio backend no incluye `frontend/dist`; el frontend de producción está separado en Vercel.
- Base del parche: commit `1e11383` del repositorio backend.


Se prueban peticiones HTTP simuladas de Gemini, rechazo de salidas truncadas/sin fuentes, cálculo de consumo, idempotencia diaria, incorporación en PostgreSQL, duplicados, tope diario y base legal pendiente. No se ha utilizado una clave real de Gemini ni se ha probado una búsqueda de pago de extremo a extremo. Hace falta esa comprobación tras configurar Railway.

La búsqueda solo utiliza sitios públicos accesibles; respeta las restricciones y protecciones del lector web existente. Los sitios que bloquean acceso, no ofrecen citas verificables o solo renderizan datos mediante JavaScript pueden no producir resultados.

Fuentes técnicas:
- https://ai.google.dev/api/generate-content
- https://ai.google.dev/gemini-api/docs/google-search
- https://ai.google.dev/gemini-api/docs/structured-output
- https://ai.google.dev/gemini-api/docs/pricing
- https://ai.google.dev/gemini-api/docs/deprecations
