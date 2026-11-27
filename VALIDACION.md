# Validación ejecutada — 22 de noviembre de 2026

**Actualización del 27 de noviembre de 2026 (envíos al aprobar, listas, plantillas, importación y bajas; migración 0009 y base de bajas):** pytest 157 aprobadas sobre PostgreSQL 16. `pruebas/test_zzzzz_flujo_aceptacion.py` (4) recorre:
- contacto y empresa en una transacción;
- importación CSV y XLSX con correo repetido, celdas vacías y fórmula;
- segmento Y/O que cambia con un atributo, frente a una lista estática que no cambia;
- exportación .xlsx con teléfonos como texto y fórmulas neutralizadas;
- plantilla → secuencia de 2 pasos → aprobación (doble clic: 1 sola) → 4 envíos con dos trabajadores a la vez y sin duplicar tras reaprobar y «reiniciar»;
- baja desde el enlace con la base secundaria apagada: segundo paso cancelado, reimportar no reactiva y una campaña nueva la excluye;
- base secundaria real migrada con `alembic_bajas.ini`: una sola fila aunque se repita la copia;
- conciliación sin reenvío, supresión justo antes de entregar y diagnóstico sin Gmail, sin elegibles y sin trabajador.

Contrato: 73 + 22 respuestas reales aprobadas por Zod. Ruff y mypy estricto sin errores. Ver ENTREGA.md.

**Actualización del 23 de noviembre de 2026 (errores de Google y Gmail):** pytest 144 aprobadas. `pruebas/test_errores_google.py` (11) comprueba:
- la explicación de cada tipo de error de Google, sin repetir el texto de Google;
- que una API de Gmail deshabilitada se dice en la tarea, con un solo intento por revisión y una sola línea en la Terminal aunque se repita;
- que la pausa con motivo llega al tercer fallo seguido;
- que un error desconocido dice su clase;
- que «Probar conexión» explica el error.

**Actualización del 23 de noviembre de 2026 (meta diaria y Terminal, migración 0007):** pytest 133 aprobadas. `pruebas/test_meta_terminal.py` (6) comprueba:
- la validación y el guardado de la meta, con auditoría;
- una búsqueda a la vez, un tipo y un municipio por búsqueda, rotación y tope del día;
- la parada al cumplir la meta y la pausa tras 3 fallos;
- la aceptación sola solo de lo corroborado con correo genérico del dominio (el correo personal queda pendiente, y con la meta cumplida también), con las líneas de la bitácora sin «@»;
- la Terminal: trabajador, cola, «empieza», «listo en», el aviso de 503 con el próximo intento y `?desde=`;
- la bajada de 0007 a 0006 y la subida.

Contrato: 71 respuestas reales aprobadas por Zod. El recorrido en Chrome con el trabajador real (ver VALIDACION del frontend) también pasó. Ruff y mypy estricto sin errores en `app`.

**Actualización del 23 de noviembre de 2026 (paciencia ante Gemini saturado):** pytest 127 aprobadas. `test_saturacion_espera_mas_y_no_repite_la_busqueda_web` comprueba 9 fallos 503 seguidos con «Intento n de 10» y las esperas de 1, 5, 15, 30 y 60 minutos; que la web se busca y se cobra una sola vez; y que el décimo intento termina y borra las páginas guardadas. `test_otros_errores_conservan_sus_4_intentos` comprueba que un error de otro tipo vuelve al tope de 4 intentos. La prueba de cuota diaria ahora comprueba la espera hasta las 00:05 del Pacífico sin gastar intentos, el aviso con la hora de Colombia y la rendición tras 7 días. Ruff y mypy estricto sin errores en `app`.

**Actualización del 23 de noviembre de 2026 (aprobación automática de la base legal, migración 0006):** pytest 125 aprobadas sobre PostgreSQL 16. `pruebas/test_aprobacion_legal.py` comprueba que la política viene apagada y no aprueba nada, que rechaza un valor no booleano, que al encenderla aprueba los contactos con correo publicado y cita verificada (con tipo, texto, prueba, aprobador, etapa y auditoría), que deja pendiente el contacto sin página, que aprueba tal cual lo que registró el administrador, que verificar un correo nuevo lo aprueba, y que apagada vuelve a esperar al administrador. También comprueba que 0006 baja a 0005 (sin la clave y con el CHECK anterior) y vuelve a subir. Contrato: 69 respuestas reales aprobadas por Zod, incluida `configuracion/cumplimiento`. Ruff y mypy estricto sin errores.

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
