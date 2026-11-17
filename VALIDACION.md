# Validación ejecutada — 17 de noviembre de 2026

| Comprobación | Resultado | Alcance |
| --- | --- | --- |
| SQL original, PostgreSQL 16 | 37/37 casos | Reglas, disparadores, auditoría, anonimización y cola sobre migración 0001. |
| pytest, PostgreSQL 16 real | 40 pruebas aprobadas | Base temporal creada con todas las migraciones; proveedores falsos. |
| Contrato frontend/backend | 31 respuestas aprobadas por Zod | JSON obtenido de la API con datos de prueba; listas y fichas. |
| Pruebas originales frontend | 39 pruebas aprobadas, 4 archivos | Vitest/MSW; VITE_API_URL absoluto solo para las pruebas Node. |
| Ruff backend | Aprobado | app/. |
| Mypy estricto | Aprobado, 40 archivos | app/. |
| ESLint frontend | Aprobado | Configuración original. |
| TypeScript + Vite | Compilación aprobada | VITE_USE_MOCKS=false; aplicación de mismo origen. |
| Recorrido navegador | No ejecutado con éxito | Chromium bloqueado al arrancar; no se afirma validación visual. |
| Docker Compose | No ejecutado | Configuración y Dockerfile entregados. |
| Proveedores reales | No ejecutado | Sin credenciales reales ni envíos. |

## Escenarios comprobados

Sesiones y hash, cierre de sesión, CSRF, OAuth inválido, rechazo de bypass en producción; CRUD, importación atómica, aprobación de base legal y supresión; edición que invalida aprobación, nueva aprobación y rechazo de evidencia inferida; respuestas de API con campos opcionales sin null indebido; privilegios que impiden alterar auditoría; reinicio de demo que conserva auditoría.

El escenario de correo usa 50 contactos ficticios elegibles, reloj controlado y Gmail falso: programa 50 mensajes, pausa y reanuda reutilizando identidad; simula aceptación de Gmail antes de guardar confirmación y recuperación concurrente sin segundo envío; verifica separación de los envíos reales aun con trabajador retrasado; registra una respuesta, un rebote duro y una baja; programa 47 seguimientos. Total: 97 llamadas al proveedor falso. GET de baja no suprime; POST repetido es idempotente.

Se probaron además firma e idempotencia del formulario, validación de salida de agentes y presupuesto, bloqueo de destinos privados del lector, normalización de citas, reglas de segmentos, festivos, clasificación de verificación de correo, importación analítica repetida sin duplicación y programación periódica idempotente.

Las pruebas no prueban entrega real de correo, exactitud comercial de un modelo ni cumplimiento exhaustivo del enunciado. Existe un aviso de deprecación del complemento AnyIO/pytest en el entorno; no produjo fallos.

## Evidencia reproducible

`pruebas/salidas/backend_tests.log`, `frontend_tests.log`, `esquema_tests.log`, `contratos.json` y `contrato_zod.log`. Los datos son ficticios. Las instrucciones de ejecución están en README.md; los límites conocidos están en DECISIONES.md.
