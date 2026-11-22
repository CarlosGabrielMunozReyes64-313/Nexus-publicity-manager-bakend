# Validación ejecutada — 22 de noviembre de 2026

Entorno: Python 3.12, PostgreSQL 16.15, Node 22.23.5, Chrome for Testing 153 con Playwright 1.65, sobre Linux.

| Comprobación | Resultado | Alcance |
| --- | --- | --- |
| SQL original, PostgreSQL 16 | 37/37 casos | `scripts/probar_esquema.py` sobre la migración 0001. |
| Migración 0004 | Aprobada | Base nueva: `alembic upgrade head` deja un solo prompt activo de `estratega_redactor` (versión 2); `alembic downgrade 0003` reactiva la versión 1 y `upgrade head` vuelve a la 2 (también en `pruebas/test_rutas_correo.py`). |
| pytest, PostgreSQL 16 real | 109 pruebas aprobadas | 15 nuevas de rutas de correo en `pruebas/test_rutas_correo.py` (pedir con validaciones; tarea con Claude falso que crea secuencia, campaña `en_revision` y propuesta con notas; salida inválida con dos correcciones y fallo sin reintentos; 429 reintentado; segmento revocado sin gasto; entrada del agente sin nombres, correos, teléfonos ni ids; `PUT` con contenido; ediciones que sobreviven a cambios de marca y segmento; vista previa por versión sin mensajes; revisión y aprobar coinciden; aprobar una versión editada sin mensajes; rechazar con y sin versión nueva, restaurar y reintentar; migración 0004; ruta anterior de segmentos; contrato sin nulos). Las 94 anteriores siguen pasando: 48 existentes (algunas actualizadas al nuevo comportamiento), 18 de regresión en `pruebas/test_correcciones.py`, 3 de propuestas a pedido en `pruebas/test_propuestas_agentes.py` (validaciones, segmento y campaña de punta a punta, fallo visible), 9 de Descubrimiento con Tavily en `pruebas/test_buscador.py` (consultas, filtrado, errores, flujo completo, prueba de conexión) y 16 nuevas de proveedores en `pruebas/test_gemini.py` (Gemini: límite por minuto, cuotas, plan sin búsqueda, reintento sin chocar con el tope, prueba de conexión; Claude: errores clasificados, búsqueda web fallida, prueba de conexión). Base temporal con todas las migraciones; proveedores falsos. |
| Contrato frontend/backend | 42 respuestas aprobadas por Zod | JSON real guardado por pytest, incluidas `propuestas/solicitudes`, `propuestas/rutas-correo/segmentos`, `propuestas/:id/revision` y `propuestas/:id/elegibles`; además, las 54 versiones reales de rutas cumplen `rutaCorreoSchema`. |
| Compatibilidad con el frontend anterior | Aprobada | Los esquemas Zod del frontend entregado antes aceptan las 34 respuestas de este backend que ese frontend usa (las 8 restantes son rutas nuevas). Por eso el backend se puede desplegar primero. |
| Pruebas del frontend | 76 aprobadas, 8 archivos | Vitest + Testing Library; 19 nuevas de rutas de correo (ver VALIDACION del frontend). |
| Escenario completo de rutas (9 pasos) | Aprobado | Chrome con Playwright sobre la compilación de demostración: pedir, verla lista, vista previa, editar el correo 2, comparar 1 con 2, rechazar, recibir la versión 3 del agente, aprobar y comprobar la campaña aprobada sin ninguna llamada de inscripción, activación ni envío. |
| Ruff, formato y mypy estricto | Aprobados | `app/` (48 archivos), la migración 0004 y las pruebas nuevas. |
| ESLint, Prettier y TypeScript + Vite | Aprobados | Compilación con `VITE_USE_MOCKS=false`. |
| Recorrido de navegador (13 pantallas) | Aprobado | `scripts/prueba_navegador.py` con la API real, el frontend compilado y Chrome. |
| Recorrido de las correcciones en navegador | Aprobado | Script temporal de Playwright contra la API real (no incluido): sesión caída frente a vencida, importación CSV con cambio de empresa y resumen, integraciones con la dirección real del webhook, costo con fecha, embudo por periodo, auditoría filtrada y exportada, baja inválida. |
| Arranque desde los ZIP | Aprobado | `scripts/probar_arranque.py` con los ZIP entregados: extracción limpia (sin `.env` previo), detección del frontend, `docker compose config`, pytest (94), `npm ci`, `npm test` (57), compilación, migraciones, API y aplicación. |
| Docker | Parcial | Rutas y contextos verificados con `docker compose config` y una construcción BuildKit de las mismas instrucciones COPY. La descarga de imágenes base de Docker Hub estaba bloqueada en este entorno, por lo que no se construyó la imagen completa. |
| Proveedores reales | No ejecutado | Sin credenciales reales ni envíos (Google, Gmail, Claude, Gemini, Tavily, Hunter). Las llamadas a Tavily siguen su documentación oficial, pero no se hicieron contra el servicio real. |

Las pruebas no prueban entrega real de correo ni la exactitud comercial de un modelo. Antes de producción, conecte las integraciones con cuentas de prueba y ejecute `npm run verificar:despliegue` del frontend contra cada entorno.

## Evidencia reproducible

`pruebas/salidas/contratos.json` (generado por pytest). Las instrucciones de ejecución están en README.md; los límites conocidos en DECISIONES.md.
