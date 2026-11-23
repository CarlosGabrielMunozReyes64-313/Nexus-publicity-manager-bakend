# Validación ejecutada — 22 de noviembre de 2026

Entorno: Python 3.12, PostgreSQL 16, Node 22.23.5, Chrome for Testing 153 con Playwright, sobre Linux.

| Comprobación | Resultado | Alcance |
| --- | --- | --- |
| SQL original, PostgreSQL 16 | 37/37 casos | `scripts/probar_esquema.py` sobre la migración 0001, en una base aparte. |
| Migraciones 0004 y 0005 | Aprobadas | En una base limpia: `upgrade head`, `downgrade 0004`, `downgrade 0003` y otra vez `upgrade head`, sin errores. Al final queda un solo prompt activo de `estratega_redactor` (v3). `pruebas/test_secuencias_editor.py` comprueba además que 0005 duplica las secuencias compartidas, crea las propuestas que faltan y baja bien. |
| pytest, PostgreSQL 16 real | 122 aprobadas | 13 en `pruebas/test_secuencias_editor.py`, una por cada regla del editor (ver la nota bajo la tabla). Las anteriores se actualizaron a la regla nueva: guardar no revoca y el prompt v3 está en head. |
| Contrato frontend/backend | 68 respuestas aprobadas por Zod | JSON real guardado por pytest. Incluye 28 respuestas de las rutas del editor: lista (también archivadas), solicitudes, borrador, versiones, revisión, elegibles, sugerencias y vista previa. |
| Compatibilidad con el frontend anterior | Aprobada | Los esquemas Zod del frontend entregado antes aceptan las 42 respuestas de este backend que ese frontend usa; las otras 26 son de rutas nuevas. Por eso el backend se puede desplegar primero. |
| Recorrido de 9 pasos en Chrome con la API real | Aprobado | Base nueva migrada a head, API real y trabajador real. Solo la IA y Gmail son simulados (script temporal, no incluido). Detalle de los pasos bajo la tabla. |
| Recorrido de navegador (13 pantallas) | Aprobado | `scripts/prueba_navegador.py` con la API real; ahora comprueba también que `/propuestas` lleva a `/secuencias`. |
| Pruebas del frontend | 95 aprobadas, 10 archivos | Ver VALIDACION del frontend. |
| Ruff, formato y mypy estricto | Aprobados | `app/` (50 archivos) y las pruebas nuevas. |
| Arranque desde los ZIP | Aprobado | `scripts/probar_arranque.py` con los ZIP entregados: extracción limpia, `docker compose config`, pytest (122), `npm ci`, `npm test`, compilación, migraciones, API y aplicación. |
| Docker | Parcial | `docker compose config` sí; las imágenes base de Docker Hub no se pueden descargar en este entorno. |
| Proveedores reales | No ejecutado | Sin credenciales reales ni envíos (Google, Gmail, Claude, Gemini, Tavily, Hunter). |

**Qué cubren las 13 pruebas de `pruebas/test_secuencias_editor.py`:**

1. Un borrador de una secuencia activa no cambia los envíos.
2. Un borrador con errores se guarda, pero no se puede aprobar.
3. Aprobar copia la versión, congela el contenido y no crea mensajes.
4. Aprobar y reanudar funciona con y sin Gmail.
5. Los cambios de marca y de segmento no tapan un borrador.
6. Vista previa sin guardar y envío de prueba (solo al administrador, sin mensajes, con el límite diario).
7. Crear, duplicar, archivar y restaurar.
8. Pedir otra versión a la IA.
9. Migración 0005.
10. Una mejora crea un borrador, y aprobar un segmento aprueba su propuesta.
11. Solicitudes a la IA desde Secuencias.
12. Texto con formato: limpieza, texto derivado, restricciones de la base y envío `multipart/alternative` por el motor, con el mismo enlace de seguimiento en las dos partes.
13. «Mejorar con IA»: sin datos personales, sin tocar el borrador, asunto solo cuando corresponde, descartar, fallo sin reintentos y reintento.

**Pasos del recorrido en Chrome:**

1. Pedir la secuencia a la IA.
2. Abrir el borrador.
3. Editar el correo 2 y guardar con Ctrl+S. Pedir «Mejorar con IA» y descartar la sugerencia.
4. Vista previa con un contacto real.
5. Enviar una prueba.
6. Aprobar, comprobando el texto «Aprobar no envía ningún correo…».
7. Inscribir y activar la campaña.
8. Editar otra vez (con el correo 3 en formato): la API sigue sirviendo el texto aprobado y las inscripciones siguen activas.
9. Aprobar y reanudar: contenido nuevo, campaña activa y HTML limpio.

Al final se comprobó el Historial y el editor y la lista a 390 px, sin desborde horizontal. No hubo errores en la consola ni respuestas 5xx.

Las pruebas no prueban la entrega real de correo ni la exactitud comercial de un modelo. Antes de producción, conecte las integraciones con cuentas de prueba y ejecute `npm run verificar:despliegue` del frontend contra cada entorno.

## Evidencia reproducible

`pruebas/salidas/contratos.json` (generado por pytest). Las instrucciones de ejecución están en README.md y los límites conocidos en DECISIONES.md.
