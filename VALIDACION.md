# Validación ejecutada — 21 de noviembre de 2026

Entorno: Python 3.12, PostgreSQL 16.15, Node 22.23.5, Chrome for Testing 153 con Playwright 1.65, sobre Linux.

| Comprobación | Resultado | Alcance |
| --- | --- | --- |
| SQL original, PostgreSQL 16 | 37/37 casos | `scripts/probar_esquema.py` sobre la migración 0001. |
| pytest, PostgreSQL 16 real | 94 pruebas aprobadas | 48 existentes (algunas actualizadas al nuevo comportamiento), 18 de regresión en `pruebas/test_correcciones.py`, 3 de propuestas a pedido en `pruebas/test_propuestas_agentes.py` (validaciones, segmento y campaña de punta a punta, fallo visible), 9 de Descubrimiento con Tavily en `pruebas/test_buscador.py` (consultas, filtrado, errores, flujo completo, prueba de conexión) y 16 nuevas de proveedores en `pruebas/test_gemini.py` (Gemini: límite por minuto, cuotas, plan sin búsqueda, reintento sin chocar con el tope, prueba de conexión; Claude: errores clasificados, búsqueda web fallida, prueba de conexión). Base temporal con todas las migraciones; proveedores falsos. |
| Contrato frontend/backend | 34 respuestas aprobadas por Zod | JSON real guardado por pytest, incluidas `auditoria/pagina`, el embudo con fechas y `agentes/solicitudes`. |
| Pruebas del frontend | 57 aprobadas, 7 archivos | Vitest + Testing Library; 18 nuevas, incluidas pruebas en modo API real. |
| Ruff, formato y mypy estricto | Aprobados | `app/` (44 archivos). |
| ESLint, Prettier y TypeScript + Vite | Aprobados | Compilación con `VITE_USE_MOCKS=false`. |
| Recorrido de navegador (13 pantallas) | Aprobado | `scripts/prueba_navegador.py` con la API real, el frontend compilado y Chrome. |
| Recorrido de las correcciones en navegador | Aprobado | Script temporal de Playwright contra la API real (no incluido): sesión caída frente a vencida, importación CSV con cambio de empresa y resumen, integraciones con la dirección real del webhook, costo con fecha, embudo por periodo, auditoría filtrada y exportada, baja inválida. |
| Arranque desde los ZIP | Aprobado | `scripts/probar_arranque.py` con los ZIP entregados: extracción limpia (sin `.env` previo), detección del frontend, `docker compose config`, pytest (94), `npm ci`, `npm test` (57), compilación, migraciones, API y aplicación. |
| Docker | Parcial | Rutas y contextos verificados con `docker compose config` y una construcción BuildKit de las mismas instrucciones COPY. La descarga de imágenes base de Docker Hub estaba bloqueada en este entorno, por lo que no se construyó la imagen completa. |
| Proveedores reales | No ejecutado | Sin credenciales reales ni envíos (Google, Gmail, Claude, Gemini, Tavily, Hunter). Las llamadas a Tavily siguen su documentación oficial, pero no se hicieron contra el servicio real. |

Las pruebas no prueban entrega real de correo ni la exactitud comercial de un modelo. Antes de producción, conecte las integraciones con cuentas de prueba y ejecute `npm run verificar:despliegue` del frontend contra cada entorno.

## Evidencia reproducible

`pruebas/salidas/contratos.json` (generado por pytest). Las instrucciones de ejecución están en README.md; los límites conocidos en DECISIONES.md.
