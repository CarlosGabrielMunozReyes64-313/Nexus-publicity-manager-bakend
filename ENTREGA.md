# Entrega — 27 de noviembre de 2026

## Por qué aprobar no enviaba correos

**Defecto reproducible en el código.** «Aprobar» solo congelaba la versión y dejaba la campaña en `aprobada`. No inscribía a nadie y no activaba la campaña. El programador (`app/motor/envios.programar`) solo trabaja inscripciones `activa` de campañas `activa`. Así que, tras aprobar, no había nada que programar. Inscribir requería ir contacto por contacto y luego pulsar «Activar» en Campañas, y la interfaz no lo decía.

**Condiciones de Railway** (vistas con acceso de solo lectura a la configuración y los registros):
- El servicio arranca con `honcho start` desde `Dockerfile.railway`, y los registros muestran `api.1` y `trabajador.1` en marcha. El trabajador sí existía.
- Desde el 25-nov, cada 2 minutos falla «Revisar respuestas en Gmail» con «la API de Gmail no está habilitada en su proyecto de Google Cloud». Con 3 fallos seguidos el trabajador pausa todas las campañas activas, y cada envío fallaría con 403. Aun corregido el código, nada saldrá hasta habilitar la Gmail API (ver GUIA_RAILWAY.md §4).
- Los registros de ese despliegue terminan el 25-nov a las 06:38 UTC. No pude confirmar si hoy el trabajador da señales; la pestaña Envíos y la Terminal lo muestran.
- `Postgres-3qtE` figura hoy en línea (desplegado el 27-nov 09:00 UTC) con su volumen, y aún no está conectado al backend.

## Qué cambió

**Envíos (backend `app/motor/audiencia.py`, `app/motor/envios.py`, `app/api/envios_estado.py`; frontend pestaña «Envíos»).**
- Aprobar programa por omisión, en la misma transacción: activa la campaña e inscribe a cada destinatario elegible con su primer paso.
- Si falta Gmail, no hay elegibles o el horario no cuadra, la aprobación se guarda y la respuesta dice qué falta. «Programar envíos» reintenta.
- Los pasos siguientes respetan esperas, ventana, festivos, cuotas, separación, pausas y respuestas, como antes. Todo vive en la base, así que sobrevive a reinicios.
- Contra duplicados:
  - El candado de la secuencia hace que un doble clic dé una sola aprobación.
  - `UNIQUE(campaña, contacto)` y el índice único por paso impiden inscribir o enviar dos veces.
  - Los candados de envío evitan que dos trabajadores manden el mismo mensaje.
  - Repetir «programar» no crea nada nuevo.
- Si Gmail pudo aceptar un correo y el proceso cayó antes de guardar, el mensaje queda «Por conciliar». Nunca se reenvía solo: la tarea `conciliar_envios` lo busca de nuevo en Gmail, y en la interfaz se decide «Sí salió / No salió». Ya no pausa todas las campañas.
- Justo antes de entregar a Gmail se vuelve a comprobar la supresión.
- Estados visibles por destinatario: Programado, Entregándose a Gmail, Aceptado por Gmail (solo con el id de Gmail), Por conciliar, Fallido (con el motivo real de Google), Pausado, Finalizado.
- La pestaña Envíos también diagnostica:
  - cero elegibles, con los motivos;
  - cuota agotada;
  - trabajador sin latido;
  - Gmail desconectado o pausado;
  - próximo envío.

**Contactos, segmentos y listas.**
- Nuevo contacto con búsqueda o creación de empresa en el mismo formulario. Se guarda en una sola transacción y detecta duplicados por NIT, dominio o nombre parecido. Ante un error conserva lo escrito.
- Segmentos con grupos Y/O anidados.
  - Atributos nuevos: cargo, dominio, estado del correo, estado de la cuenta y lista.
  - Comportamientos basados en eventos: hizo clic (sin escáneres), respondió y recibió correo.
  - La vista previa con conteo y muestra la calcula el servidor con la misma función que elige destinatarios.
- Listas de envío (migración 0009):
  - Estáticas: guardan a sus miembros, por ejemplo una copia del segmento.
  - Vinculadas: siguen al segmento.
  - Se pueden renombrar, archivar, añadir y quitar miembros, y elegir como audiencia de una secuencia.
  - La opción «Incorporar nuevos miembros» decide si quien entra después de aprobar recibe la secuencia, y se muestra en Envíos. Por omisión no.
  - Una baja retira al contacto de las listas.

**Importar y exportar.**
- .csv y .xlsx, leídos en el servidor.
- Mapeo de columnas, vista previa por fila con su motivo y confirmación.
- Lotes de 500 filas. Límites: 4 MB, 20.000 filas y 60 columnas.
- Normaliza tildes y correos con espacios o mayúsculas, ignora filas vacías y detecta duplicados dentro del archivo y contra la base.
- Ante un correo existente se elige omitir o actualizar. Al actualizar, una celda vacía conserva el dato salvo que se elija «borrar vacíos».
- Puede asignar a una lista.
- Un correo dado de baja nunca se importa ni se reactiva. Un disparador lo garantiza también fuera de la importación.
- Exportar a .xlsx: toda la base, una lista, un segmento o el filtro de la pantalla, con todas las filas.
  - Todas las celdas van como texto, así que los teléfonos conservan el «+».
  - Lo que parece una fórmula se exporta con «'».

**Plantillas.**
- Crear, editar, duplicar, archivar y restaurar, con vista previa (HTML y texto) con datos de ejemplo o de un contacto.
- Avisa de variables no admitidas.
- Se pueden copiar en un paso desde el editor. La versión aprobada conserva su copia.
- Revisión bloquea la aprobación si un paso usa una variable que no existe.

**Baja global.**
- Cada correo lleva «Rechazar / Darme de baja» en texto y HTML, aunque falte `{{baja}}`. Usa `BAJA_URL_PUBLICA` o `APP_URL`.
- El GET muestra una confirmación y la baja ocurre al confirmar. Es idempotente.
- En la misma transacción de la base principal:
  - se crea la supresión, se excluye al contacto y sale de las listas;
  - se finalizan sus inscripciones y se cancelan sus envíos pendientes;
  - se registra la tarea de copia en `sincronizacion_bajas`.
- El trabajador copia a `bajas.usuarios_baja` en `UNSUBSCRIBE_DATABASE_URL`:
  - con `ON CONFLICT`, así que nunca duplica;
  - reintenta con espera creciente hasta 1 hora;
  - tiene su propia migración (`alembic_bajas.ini`).
- La administración distingue «efectiva · sincronización pendiente» de «efectiva · sincronizada».

## Pruebas ejecutadas (todas pasaron)

| Qué | Resultado |
| --- | --- |
| pytest backend (PostgreSQL 16 real) | 157 aprobadas (153 existentes + 4 nuevas de aceptación) |
| `pruebas/test_zzzzz_flujo_aceptacion.py` | Recorrido de aceptación completo de los 5 pasos, más conciliación sin reenvío, supresión en el último instante y diagnóstico sin Gmail / sin elegibles / sin trabajador |
| Contrato frontend/backend | 73 + 22 respuestas reales validadas con los esquemas Zod del frontend |
| Ruff, formato, mypy estricto | Sin errores (`app/`) |
| Migración 0009 | Subida, triggers y bajada a 0008 conservando la audiencia de campañas con lista |
| Frontend | Vitest 126 aprobadas (16 archivos), `tsc --noEmit`, ESLint, Prettier y `vite build` |
| Chrome real con la API real | Listas, plantilla, contacto con empresa, segmento Y/O, pestaña Envíos y configuración sin errores de consola |

Gmail es `GmailFalso` en todas las pruebas. Los destinatarios son ficticios (`*.example`).

## Verificación externa pendiente (no tengo credenciales de Gmail ni permiso de escritura en Railway)

1. Habilitar la Gmail API en el proyecto de Google Cloud y comprobar «Probar conexión».
2. Definir `UNSUBSCRIBE_DATABASE_URL` (y, si quiere, `BAJA_URL_PUBLICA`) y el Pre-deploy command de la guía.
3. Desplegar ambos proyectos. Migrar la base principal (0009) y la de bajas (`scripts/migrar_bajas.py --estricto`).
4. Ejecutar `scripts/prueba_buzon_control.py` con un buzón propio y ver «Aceptado por Gmail» en la pestaña Envíos.
5. Confirmar la baja desde ese correo y ver la fila en `bajas.usuarios_baja`.

No modifiqué nada en Railway: solo leí la configuración y los registros.

## Límites conocidos

- «Mejorar con IA» y «Pedir otra versión» requieren una audiencia de segmento: la IA trabaja con cifras agregadas del segmento.
- En la demostración (MSW), la importación solo lee .csv, la exportación no genera archivos y no se programan envíos.
- Una fila sin correo se rechaza al importar: sin correo no se pueden evitar duplicados ni escribirle.
- La pestaña Envíos lista hasta 500 destinatarios por secuencia. Los totales cuentan todos.
