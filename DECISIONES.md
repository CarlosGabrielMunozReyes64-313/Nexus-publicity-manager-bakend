# Decisiones y alcance pendiente

La entrega implementa la integración principal, pero no equivale al cumplimiento íntegro del prompt ni a una certificación de producción. `REQUISITOS.md` conserva el enunciado para contrastar el alcance.

## Autoridad de datos y contrato

- Se conservan sin editar los dos SQL entregados. La migración 0001 ejecuta el esquema; 0002 añade OAuth y una función de anonimización con privilegios restringidos; 0003 agrega prompts y contadores de consumo.
- Los 37 casos originales se ejecutaron sobre 0001. La prueba original crea sus propios prompts activos de versión 1, incompatibles con la semilla de prompts de 0003; por eso no se presenta esa ejecución como una prueba de toda la aplicación en head. Las 40 pruebas nuevas sí usan head.
- En el frontend solo cambian los archivos de tipos y esquemas para hacer opcional `Respuesta.campanaId`, y el botón de conexión Gmail para navegar al OAuth real. Se añade un script de verificación del contrato. Los textos originales que dicen «simulado» permanecen en algunos controles; con mocks desactivados el control llama a la API real.
- No se importan credenciales ni sesiones de desarrollo al paquete. Las integraciones inician desconectadas.

## Decisiones conservadoras del motor

- Ante un envío ambiguo, se busca el Message-ID estable y el encabezado de idempotencia. Si no se encuentra, se pausa y exige conciliación; no se reenvía a ciegas. Esto se aparta del reintento automático cuando no hay resultado de búsqueda solicitado en el prompt. Una búsqueda vacía no garantiza que Gmail no haya aceptado el correo.
- Un rebote blando pausa la inscripción. **Falta implementar la política solicitada de reintento a los dos días y agotamiento de reintentos**, conciliándola con la unicidad del mensaje por paso y conservando su historial.
- El trabajador reclama una tarea cada vez, no lotes de cinco. Se evitan concesiones que venzan mientras otras tareas del lote esperan. Varios trabajadores pueden reclamar tareas distintas; los envíos se serializan con un bloqueo PostgreSQL y vuelven a comprobar separación, ventana y topes al ejecutarse.
- El enriquecimiento genera propuestas, incluso para campos vacíos con evidencia verificada; no rellena automáticamente esos campos. Es una restricción adicional respecto al prompt.

## Funcionalidades que aún requieren completar o ampliar

| Área | Estado concreto |
| --- | --- |
| Personas descubiertas | La API puede devolver personas existentes, pero el descubrimiento del agente no extrae ni persiste una lista de personas. |
| Catálogo de sectores | No existe una normalización exhaustiva contra un catálogo cerrado de sectores. |
| Calificación configurable | El puntaje usa las tres reglas iniciales (40/30/30) y registra la versión de configuración; cambiar textos arbitrarios en cliente ideal no crea nuevas reglas ejecutables. |
| Informe semanal | La tarea usa métricas acumuladas de campaña; falta restringir su muestra y comparación al período semanal. |
| Mejoras del analista | Las recomendaciones de texto pueden convertirse en propuestas, pero no generan siempre el objeto estructurado objetivo/id/cambios necesario para aprobar una modificación. La API rechaza esas aprobaciones incompletas. |
| Clasificación de respuestas | Baja de alta confianza aplica supresión; otras clasificaciones requieren confirmación administrativa. Fuera de oficina se detecta por encabezados/texto en el rastreador, pero la clasificación del modelo no aplica todas las transiciones protectoras del prompt. |
| Reintentos del modelo | El redactor tiene una corrección de secuencia y el trabajador reintenta tareas; falta uniformar una única corrección estructurada para cada agente según el enunciado. |
| Pruebas de aceptación | No se cubre cada combinación de errores de cada endpoint ni todos los casos de proveedor enumerados en el prompt. Las cantidades y escenarios realmente ejecutados están en VALIDACION.md. |

## Validación externa pendiente

Se necesitan credenciales y propiedades reales para comprobar OAuth Workspace, consentimiento Gmail, envío y lectura de hilos, cuotas y errores de Google, Claude con modelos/precios válidos, Hunter, GA4 y Search Console. No se enviaron correos reales ni se consumieron llamadas de pago durante las pruebas.

Docker Compose está preparado, pero no se ejecutó Docker en este entorno. Chromium no pudo arrancar por las restricciones del entorno; el script del recorrido de las 13 pantallas está incluido y pendiente de ejecución local. Antes de producción, completar los puntos funcionales anteriores y ejecutar estos recorridos con cuentas de prueba.

## Correcciones de noviembre de 2026 (lista de defectos 01-25 y hallazgos adicionales)

Sin migraciones nuevas: el despliegue en Railway no ejecuta `alembic upgrade`, así que ningún cambio depende de columnas o tablas nuevas. Los motivos visibles de pausa se derivan al leer (horario incompatible, Gmail desconectado).

- **Importación CSV (01, 02, 22).** El servidor detecta duplicados con dos conjuntos (existentes y vistos), valida cada fila y responde `creados/omitidos/rechazados`. Un error general (empresa inexistente, base `autorizacion_formulario`) cancela todo el lote.
- **Integraciones (03, 04, 14).** Conectar/desconectar devuelve el estado real (`error` incluido, con su motivo). El receptor del formulario consulta el estado de «Formulario» con `FOR SHARE` antes de guardar: desconectado responde 503 y no crea nada. Compatibilidad: si el secreto existe y nadie tocó nunca esa integración (según la auditoría), el primer arranque la conecta una vez para no cortar la captación; una desconexión explícita siempre se respeta.
- **Informes (05, 06, 12, 13).** Cada evento se cuenta en su propia fecha de Bogotá (envío, respuesta, visita, formulario, cierre). Los cierres sin campaña se atribuyen al origen del contacto. El costo diario aparece una vez por fecha; el frontend agrega por fecha antes de calcular costo por lead y de adquisición. El gasto admite fecha real (no futura) y concepto. `/embudo?desde&hasta` filtra por fecha de creación en Bogotá.
- **Horarios (07).** Guardar un horario global incompatible con campañas aprobadas responde 409 con sus nombres; con `pausarIncompatibles=true` guarda y las pausa. El programador procesa cada inscripción en su propio punto de guardado: una campaña incompatible se pausa sola, sin revertir el lote. Tras un envío ya aceptado por Gmail, un horario incompatible pausa la campaña pero el mensaje queda como enviado. Reanudar conserva la espera en días hábiles del seguimiento pendiente.
- **Auditoría (09).** Filtros en el servidor (actor, acción y entidad por coincidencia parcial; referencia exacta; fechas como días de Bogotá), `GET /auditoria/pagina` y `GET /auditoria/exportar` con todas las coincidencias. `GET /auditoria` conserva su comportamiento.
- **Bajas (15).** Solo se confirma si la baja se registró o ya estaba aplicada; un enlace inválido muestra un mensaje neutro y una vía alternativa (responder BAJA o escribir a la cuenta de envío).
- **Rastreador (16, 17).** Si falta la parte de texto, el HTML se convierte a texto sin la parte citada. Sin cursor válido se recupera desde la última sincronización correcta (o el primer envío) con dos horas de solapamiento, descartando identificadores ya registrados; fuera de los hilos propios solo se leen avisos de rebote. Un mensaje borrado durante la lectura (404) ya no detiene la sincronización; si se agota el tiempo, se guarda lo procesado y se continúa en la siguiente vuelta.
- **Informe semanal (18).** Se programa según el proveedor activo: Gemini por configuración (clave y precios), Claude con su integración conectada. La integración «claude» representa al proveedor activo y su prueba de conexión llama a Claude o a Gemini.
- **Rendimiento (20, 21).** Listados con consultas agrupadas (constantes, sin N+1) y sin evidencias (la ficha las trae completas); tamaño de segmentos con una sola carga mínima de datos. Paginación opcional (`limite`, `desplazamiento`, cabecera `X-Total-Count`). El puntaje diario es una única tarea por día que recorre empresas por páginas con punto de control y no repite filas idénticas.
- **Arranque (08).** Docker usa la carpeta del backend como contexto y el frontend como contexto adicional (`FRONTEND_DIR`, por defecto `../Nexus-publicity-manager-main`). `entorno_desarrollo.py` detecta el frontend y escribe `FRONTEND_DIR`/`FRONTEND_DIST`; el backend sirve `FRONTEND_DIST` sin Docker. Se eliminaron `Dockerfile.bak` y `docker-compose.yml.bak`.
- **Hallazgos adicionales.** OAuth: el administrador configurado siempre puede entrar y otras cuentas solo del dominio de Workspace permitido; Gmail ya no exige que la cuenta de envío sea la del administrador, detecta permisos incompletos y vuelve a Configuración con un mensaje en lugar de JSON. Responder exige Gmail conectado. La fusión de empresas descarta pares obsoletos, reapunta referencias y permite marcar «no es duplicado». Una oportunidad creada desde una respuesta conserva contacto y campaña. Cambiar una oportunidad a la misma etapa ya no reinicia su fecha de cierre. `/r` y `/baja` responden `no-store` (Vercel ahora honra cabeceras de caché en reescrituras externas). Se corrigió `test_mvp_50_contactos` (mezclaba reloj falso y real) y el orden frágil de las pruebas de contrato. La suite ya no depende del `.env` local ni de un frontend compilado: `conftest.py` fija valores de prueba (una variable del entorno sigue teniendo prioridad) y la prueba del frontend usa un `index.html` mínimo temporal.

## Descubrimiento con Gemini (22 de noviembre de 2026)

- **Reintentos que nunca podían funcionar.** Un fallo del proveedor conservaba la reserva completa (unos USD 0,33 de los 0,50 por tarea), así que el reintento siempre fallaba con «Tope por tarea alcanzado». Ahora `FalloProveedor` lleva el consumo conocido: si el proveedor respondió con un error o una salida no válida, la ejecución queda en `error` con su costo real y se libera la reserva. Sin respuesta (red o tiempo agotado) se conserva, como antes.
- **429 de Gemini clasificado.** Límite por minuto: espera lo que indica Google (hasta 90 s por ejecución) y luego reintenta la cola. Cuota diaria, mensual, límite 0 o plan sin búsqueda en Google (Gemini 3 en el plan gratuito): falla de inmediato con un mensaje que dice qué hacer. Lo mismo para clave inválida (400), permisos (401/403) y modelo inexistente (404). Los mensajes son textos fijos, sin el cuerpo de la respuesta ni la clave.
- **Motivo visible.** La búsqueda muestra «Intento N de M sin éxito: motivo. Nuevo intento a las HH:MM» mientras espera y «No se pudo completar la búsqueda. motivo.» si falla; los errores definitivos de agentes ya no gastan reintentos.
- **Prueba de conexión.** Conectar la integración de IA con Gemini prueba también la búsqueda en Google y avisa si Descubrimiento no funcionará.
- **Claude con el mismo cuidado.** Los errores de Anthropic se clasifican (sin créditos, límite de gasto, búsqueda web deshabilitada, clave, modelo, límite, saturación) con textos fijos. Como la API responde 200 aunque la búsqueda web falle, un error dentro del contenido o una respuesta sin búsquedas ya no termina en «0 resultados» silencioso: la búsqueda falla con el motivo. «Conectar» la API de Claude prueba también una búsqueda web.

## Propuestas a pedido (22 de noviembre de 2026)

- **Faltaba el disparador.** Los agentes Segmentador (`proponer_segmentos`) y Estratega-redactor (`proponer_campana`) existían, pero nada los encolaba; como las campañas solo nacen de esa propuesta, en producción no se podía crear ninguna campaña nueva. Ahora: `POST /segmentos/proponer`, `POST /segmentos/:id/proponer-campana` y `GET /agentes/solicitudes`, siguiendo el «a pedido» de la sección 18 de REQUISITOS (no se programan solos).
- **Validar antes de gastar.** Antes de encolar se comprueba el proveedor de IA (clave y precios), que haya empresas, que el segmento esté aprobado y tenga contactos elegibles, y que no haya otro pedido igual en curso; cada caso responde 409 con el motivo.
- **Motivo visible.** Un fallo del agente queda en la lista de pedidos con un texto seguro (contactos no elegibles, segmento sin aprobar, reglas o secuencia no válidas, errores del proveedor). Esos fallos no gastan reintentos.
- **Pruebas.** Antes no había pruebas de estos agentes; ahora una prueba recorre segmento propuesto → campaña propuesta → aprobación con un proveedor falso.

## Descubrimiento con Tavily (22 de noviembre de 2026)

- **Por qué.** La búsqueda en Google de Gemini 3 no existe en el plan gratuito y la de Claude se paga. Con `TAVILY_API_KEY` (plan gratuito de 1.000 créditos al mes), Descubrimiento usa Tavily como buscador y el agente solo extrae de páginas ya leídas, algo que el plan gratuito de Gemini sí permite.
- **Flujo.** Consultas repartidas entre tipos y municipios (hasta `TAVILY_CONSULTAS_MAX`, 1 crédito cada una), exclusión de redes, directorios, portales de empleo y comercio, un resultado por dominio, página de contacto leída con el lector propio (robots.txt, sin direcciones privadas, en paralelo y con tiempo acotado) y extracción con el esquema `DescubrimientoEnPaginas`, cuyas instrucciones van fuera de los datos no confiables. La verificación de citas y el guardado son los mismos que con la búsqueda del proveedor.
- **Sin páginas no hay gasto.** Si Tavily no devuelve sitios utilizables, la búsqueda termina sin resultados y no se llama al modelo.
- **Errores.** Clave inválida (401) y créditos o límite agotados (432/433) fallan de inmediato con el motivo; 429 y 5xx se reintentan. «Conectar» la API de IA prueba Tavily en lugar de la búsqueda del proveedor.
- **Costo.** `TAVILY_COSTO_CREDITO_USD=0` (plan gratuito); con pago por uso, el gasto se registra en costos.

## Rutas de correo en Propuestas (22 de noviembre de 2026)

- **El agente solo propone.** `estratega_redactor` crea la secuencia, la campaña `en_revision` y la propuesta, o una versión nueva al revisar un rechazo (ahora con `autor: agente`; antes quedaba como `admin` por error). Ninguna ruta nueva aprueba, activa ni envía; aprobar sigue sin crear mensajes.
- **Rutas nuevas** (`app/api/rutas_correo.py`, registradas antes de las genéricas de propuestas): `POST /propuestas/rutas-correo`, `GET /propuestas/solicitudes`, `POST /propuestas/solicitudes/:id/reintentar`, `PUT /propuestas/:id` con `contenido`, `POST /propuestas/:id/previsualizar`, `GET /propuestas/:id/revision`, `POST /propuestas/:id/restaurar` y `pedirNuevaVersion` en rechazar. Se añadieron dos lecturas que el frontend necesita sin calcular elegibilidad por su cuenta: `GET /propuestas/rutas-correo/segmentos` (segmentos aprobados con su número de elegibles) y `GET /propuestas/:id/elegibles` (hasta 50 contactos para la vista previa).
- **Se conservan las rutas anteriores.** `POST /segmentos/:id/proponer-campana` usa la misma lógica con 3 correos y el objetivo «Que completen el diagnóstico RSE Express», y sigue respondiendo `tareaId`. `GET /agentes/solicitudes` no cambia.
- **Estados de las solicitudes.** `pendiente → en_cola`, `en_proceso → trabajando`, `completada → lista`, `fallida` y `cancelada → fallida` (una cancelada muestra «Solicitud cancelada»). Al terminar, la tarea guarda `propuestaId` en `tareas.datos`.
- **Una sola lógica para revisar y aprobar.** `revision()` calcula bloqueos (versión decidida, propuesta aprobada, segmento sin aprobar, ventana fuera del horario global, correo inferido), errores (`validar_secuencia` con las palabras prohibidas) y advertencias; `aprobar()` llama a la misma función y responde 400 con los bloqueos y errores unidos por «. ». La aprobación esencial no cambió: copia el contenido de la versión a la secuencia, congela en `aprobaciones_campana` (con su huella) y deja la campaña `aprobada`.
- **Advertencias.** Más de 120 palabras en un correo sin contar `{{firma}}` ni `{{baja}}`, asunto de más de 60 caracteres, mayúsculas sostenidas o signos repetidos, más de un `{{enlace}}`, ruta de más de 15 días hábiles, Gmail desconectado y, por variable de datos usada, «X de N contactos usarán el texto de respaldo». Ese conteo sigue la regla de `render()` (dato con evidencia verificada del contacto o su empresa, y para `{{senal}}` la primera señal) con una sola consulta SQL, sin renderizar contacto por contacto.
- **Sin datos personales para el agente.** La entrada lleva reglas del segmento (una selección manual de contactos se resume como cantidad, sin identificadores), justificación y versión, número de elegibles, cifras por sector, municipio y tamaño, los tipos de señal más frecuentes con su número de contactos, cuántos elegibles tienen cada variable verificada, la marca (oferta, tono, firma, prohibidas), los criterios del cliente ideal, la ventana global y la solicitud. Al revisar un rechazo se añaden la versión rechazada, su comentario y los comentarios anteriores. Nunca nombres, correos, teléfonos ni ids de contactos.
- **Correcciones y fallos definitivos.** La salida se valida con `validar_secuencia` y, en una solicitud nueva, con el número de correos pedido; se corrige hasta dos veces enviando la lista de errores. Si sigue sin cumplir, la tarea falla sin reintentos con «La ruta propuesta no cumple: …». Un segmento ya no aprobado o sin elegibles también falla sin reintentos. Los errores temporales (429, 5xx, tiempo agotado) se siguen reintentando.
- **Notas del agente.** `CampanaPropuesta` incluye `notas` (`general` y `pasos[{orden, proposito, porQue}]`) y la versión guarda la secuencia completa más `notasAgente`. Al editar, se conservan las notas y los correos cambiados se marcan con `editadoPorAdmin: true`. Todas las comparaciones usan `Secuencia.model_validate(...).model_dump(by_alias=True, exclude_none=True)`.
- **`revisar_pendientes` corregido.** Una propuesta pendiente (o rechazada) solo recibe una versión nueva desde la secuencia en uso si esa secuencia cambió después de la última versión (`secuencias.actualizado_en > propuesta_versiones.creada_en`); si no, gana la versión del administrador o del agente. Antes, cualquier cambio de marca o de segmento creaba una versión con la secuencia vieja y borraba en la práctica las ediciones del administrador. Las propuestas ya aprobadas conservan el comportamiento anterior (versión nueva con la secuencia vigente, que es la aprobada), ahora también con sus notas.
- **Rechazar.** Rechazar una versión ya decidida responde 400 en vez de chocar con el disparador `version_inmutable`. `pedirNuevaVersion` es verdadero por defecto; solo entonces, y solo para rutas del Estratega-redactor, se encola `revisar_campana`.
- **Migración 0004 (solo datos).** Desactiva la versión 1 del prompt (por el índice único de un prompt activo por agente) e inserta la versión 2 con el mismo texto de seguridad de 0003. La bajada reactiva la 1 y borra la 2 si ninguna ejecución la usó; si se usó, la conserva inactiva para no perder la auditoría, y una subida posterior la reactiva. No se debilitó ningún CHECK, disparador ni función: los 37 casos de `sql/nexus_publicity_manager_pruebas.sql` siguen pasando.
- **Contrato aditivo.** Campos nuevos en camelCase (`autor`, `decision`, `comentario`, `decididaEn` en las versiones; `ruta` en la propuesta de tipo campana); los opcionales vacíos se omiten. `PUT /propuestas/:id` y `POST /propuestas/:id/restaurar` responden `{ok: true, version}`. El frontend anterior sigue funcionando con este backend.
- **Despliegue.** Primero el backend (compatible con el frontend actual) y después el frontend. Railway debe ejecutar `alembic upgrade head` como comando previo al despliegue; sin 0004 todo funciona con el prompt anterior.

## Secuencias de correo: editor con borrador, prueba y aprobación (22 de noviembre de 2026)

Reemplaza la pantalla de Propuestas para las rutas de correo. Todo vive en «Secuencias de correo»: la IA propone la secuencia completa, el administrador la edita, la prueba y la aprueba. Aprobar no envía nada.

- **Tres reglas que no cambian.**
  1. Solo se envía lo aprobado: el motor lee `aprobaciones_campana.contenido_congelado`, como antes.
  2. Guardar un borrador no cambia lo que se está enviando.
  3. La IA propone y escribe, pero nunca aprueba, activa ni envía.
- **Modelo.**
  - Cada secuencia pertenece a una campaña, y esa campaña tiene una propuesta interna de tipo `campana`. Sus `propuesta_versiones` son el historial, y la última versión es el borrador.
  - `secuencias` y `pasos` guardan lo último aprobado y solo cambian al aprobar.
  - Guardar crea una versión (`autor: admin`), aunque tenga errores; esos errores impiden aprobar, no guardar.
  - Un borrador idéntico al último y sin decidir no crea versión.
  - Las comparaciones usan `Secuencia.model_validate(...).model_dump(by_alias=True, exclude_none=True)`.
- **Cambio de comportamiento.** `PUT /propuestas/:id` con `contenido` y `PUT /secuencias/:id` ya no revocan la aprobación: crean un borrador. Antes, editar una secuencia activa la pausaba al instante. Ahora la campaña sigue enviando la versión aprobada hasta que se aprueban los cambios.
- **Aprobar una campaña activa.**
  - Aprobar sigue pausando sus envíos (es lo que hacen los disparadores al cambiar `pasos`).
  - `POST /secuencias/:id/aprobar-y-reanudar` aprueba y reanuda en dos transacciones. Si reanudar falla (por ejemplo, con Gmail desconectado), la aprobación queda guardada y se responde 400 con el motivo.
  - Una versión ya decidida no se vuelve a decidir: se aprueba una copia.
- **`revisar_pendientes`.**
  - Ya no crea versiones a partir de la secuencia en uso.
  - Si la última versión estaba decidida (por ejemplo, aprobada y luego revocada por un cambio de marca o de segmento), crea una copia para aprobarla de nuevo.
  - Nunca tapa un borrador más nuevo.
- **Migración 0005** (la 0004 puede estar aplicada en producción):
  - `pasos.formato` (`texto` o `html`, con CHECK);
  - `pasos.cuerpo_html` con CHECK: nulo en texto; en html, entre 1 y 20.000 caracteres y con `{{baja}}` y `{{firma}}`;
  - `secuencias.archivada_en` y `mensajes.cuerpo_html`;
  - duplica las secuencias compartidas por varias campañas (la campaña más antigua conserva la original);
  - crea la propuesta `campana` que falte (versión 1 = la secuencia en uso);
  - prompt v3 del `estratega_redactor`: desactiva el anterior antes de insertar.
  - La bajada reactiva v2, borra v3 si nadie la usó y quita las columnas.
  - No se debilitó ningún CHECK, disparador ni función: los 37 casos originales pasan.
- **Lecturas.**
  - `GET /secuencias` añade, sin quitar nada: `campanaId`, `campanaEstado`, `segmentoId`, `estadoEditor`, `versionBorrador`, `versionAprobada`, `actualizadaEn` y `archivada`.
  - `estadoEditor` toma el primer caso que se cumpla, en este orden: archivada → borrador (nunca aprobada) → `cambios_sin_aprobar` (el borrador difiere de lo congelado, o se revocó la aprobación) → el estado de la campaña.
- **Revisión.** `GET /secuencias/:id/revision` devuelve `{puedeAprobar, bloqueos, errores, advertencias, elegibles, gmailConectado, duracionDiasHabiles, hayCambiosSinAprobar}` y algunos datos de apoyo. Aprobar usa exactamente esa lógica. Los errores se nombran «Correo N:», como en el editor.
- **Envío de prueba** (`POST /secuencias/:id/prueba`):
  - va solo al correo del administrador, con el asunto «[Prueba] …» y la cabecera `X-Nexus-Prueba`;
  - el enlace de baja es «#» y no hay enlaces de seguimiento ni filas en `mensajes`;
  - requiere Gmail conectada y admite máximo 20 al día (días de Bogotá), contados en `registro_auditoria` (`envio_prueba`), así que el límite sobrevive a reinicios.
- **Texto con formato.**
  - El HTML se limpia en el servidor con nh3 y una lista blanca: p, br, strong, em, ul, ol, li y a, con href solo `https:` o `{{enlace}}`.
  - El texto simple se genera a partir del HTML limpio y se guarda en `pasos.cuerpo`, que conserva todas sus reglas.
  - El motor guarda el HTML personalizado en `mensajes.cuerpo_html` y envía `multipart/alternative` (texto primero) con una plantilla mínima, sin imágenes ni píxel de apertura.
  - Las variables se escapan siempre. `href="{{enlace}}"` y `{{enlace}}` suelto llevan el mismo enlace de seguimiento que el texto, y `{{baja}}` muestra el texto de baja con su enlace.
  - La IA siempre escribe texto simple.
- **«Mejorar con IA».**
  - `POST /secuencias/:id/correos/:orden/mejorar` encola `reescribir_correo` con el texto del correo, el resto de la secuencia (sin notas) y la instrucción (3 a 300 caracteres). 409 si ya hay una en curso para ese correo.
  - El agente recibe además cifras agregadas del segmento; nunca datos personales.
  - La salida `{asunto?, cuerpo, porQue}` pasa las reglas de un correo: firma, baja, longitud, palabras prohibidas y variables conocidas. Se corrige hasta dos veces; si sigue sin cumplir, falla sin reintentos con «La sugerencia no cumple: …».
  - La sugerencia queda en `tareas.datos.resultado` y no toca el borrador.
  - `GET /secuencias/:id/sugerencias` la lista; `DELETE /secuencias/:id/sugerencias/:tareaId` la oculta y, si aún no empezó, la cancela.
  - Se añadió DELETE al enrutador. Como toda mutación, exige `Content-Type: application/json` y el mismo origen.
- **Archivar.** Una campaña activa no se archiva (409 «Pause la campaña antes de archivar la secuencia»). Una secuencia archivada no admite borradores ni aprobación.
- **Despliegue.** Primero el backend y después el frontend. El comando previo al despliegue en Railway sigue siendo `alembic upgrade head`, que aplica 0005. La dependencia nueva `nh3` está en `pyproject.toml` y `requirements.lock`.

## Verificar correos pendientes (23 de noviembre de 2026)

- **El problema.** Un contacto solo es elegible con un correo `valido`, y ese estado solo lo pone el verificador (DNS + Hunter). Antes, la verificación se encolaba únicamente al crear el contacto o al cambiar su correo. Los contactos guardados sin Hunter conectado quedaban «desconocido» para siempre y no había forma de verificarlos de nuevo.
- **`POST /contactos/:id/verificar-correo`.** Encola `verificar_correo` para ese contacto y responde `{ok:true}`. Responde 400 si el contacto no tiene correo y 409 si está excluido o anonimizado. Queda en la auditoría como `verificar_correo`. El estado nunca se marca a mano.
- **Al conectar el verificador.** Se encolan todos los correos en estado `desconocido` de contactos no excluidos ni anonimizados, y el detalle de la integración dice cuántos.
- **Mensaje.** «El segmento no tiene contactos elegibles» ahora dice también que hace falta un correo verificado, no solo la base legal. El trabajador usa la misma constante para tratarlo como fallo definitivo.

## Aprobación automática de la base legal (23 de noviembre de 2026)

- **Pedido.** El administrador pidió que la evidencia legal se llene y se apruebe sola, conservando los botones manuales para cambiarla.
- **Política, apagada por defecto.** Migración 0006: clave `cumplimiento` en `configuracion` con `{"aprobacionLegalAutomatica": false}`. El CHECK de claves se amplía en una migración reversible; no se debilita ningún CHECK ni disparador. `PUT /configuracion/cumplimiento` exige un booleano y queda en la auditoría (`aprobacion_automatica_encendida` / `_apagada`).
- **Qué se aprueba.** Solo contactos con correo, no excluidos ni anonimizados, sin base aprobada y que no sean de formulario. Si el administrador ya registró tipo, texto y prueba, se aprueba tal cual. Si no, hace falta una evidencia verificada con página (`url`) cuyo valor sea ese mismo correo (`correo` del contacto o `correoCorporativo` de la empresa). Con ella se registra `corporativo_generico`, un texto que dice que la aprobó la política sin revisión manual, y la prueba «url — «cita» — fecha». Sin esa evidencia, el contacto sigue esperando al administrador.
- **Quién aprueba.** `aprobada_por` es el administrador que encendió la política (lo exige el CHECK de `bases_legales`). Cada aprobación queda en la auditoría como `aprobacion_legal_automatica`.
- **Cuándo.** Al encender la política (sobre los pendientes; la respuesta dice cuántos) y cada vez que el verificador revisa un correo, antes de calcular la elegibilidad. Apagarla no deshace lo aprobado.
- **Límites que se mantienen.** La elegibilidad sigue exigiendo correo `valido`, y las bajas y supresiones prevalecen. La IA no interviene: es una regla del backend, sin datos de contacto enviados a ningún modelo.
- **Aviso.** Que la base legal sea suficiente depende del caso (Ley 1581 de 2012: el correo corporativo de un empleado se considera dato personal). La política es una decisión del administrador.

## Más paciencia ante Gemini saturado y cuota diaria (23 de noviembre de 2026)

- **Problema.** Una búsqueda con Gemini saturado (503) se rendía tras 4 intentos en unos 36 minutos (esperas de 1, 5 y 30 minutos), y cada reintento repetía la búsqueda en la web y volvía a pagar Tavily. Con cientos de búsquedas en cola, una caída de Google de una hora dejaba muchas en error.
- **Saturación o límite por minuto** (Gemini 500/502/503/504 y 429 por minuto o capacidad; Claude transitorios y 429): las tareas de IA pasan a 10 intentos con esperas de 1, 5, 15 y 30 minutos y luego cada hora (unas 6 horas en total). Lo hace el trabajador después de `terminar_tarea`; no cambia el esquema ni hay migración. Si después llega un error de otro tipo, la tarea vuelve a su tope normal de 4 intentos, para no pagar 10 veces una salida mala.
- **Cuota diaria agotada** (antes fallaba al instante): la tarea espera a las 00:05 del Pacífico, la hora en que Google reinicia las cuotas diarias según su documentación de límites. No gasta intentos. La búsqueda dice cuándo seguirá, en hora de Colombia. Tras 7 días seguidos sin cuota se rinde con el motivo. Los errores que no se arreglan esperando (sin cuota en el plan, clave inválida, búsqueda que requiere pago, cuota mensual) siguen fallando de inmediato.
- **Búsqueda en la web una sola vez:** las páginas encontradas por Tavily se guardan en `tareas.datos._paginas` (solo texto público de sitios web, que de todos modos va al modelo) y los reintentos las reutilizan, sin volver a cobrar. Se borran al terminar bien la búsqueda.
- **La cola no se bloquea:** una tarea reprogramada espera su hora mientras el trabajador sigue con las demás.

## Meta diaria de contactos y Terminal (23 de noviembre de 2026)

- **Pedido.** El administrador pidió una «meta de contactos diarios» en Descubrimiento y una «terminal» para ver lo que pasa en segundo plano. Eligió que la meta **busque sola** hasta cumplirse y que los resultados comprobados **se acepten solos**.
- **Configuración.** Migración 0007: clave `descubrimiento` en `configuracion` con `{activa, metaDiaria, maxBusquedas, tipos, municipios, sectores, tamano}`. El CHECK de claves se amplía en una migración reversible. `PUT /descubrimiento/meta` valida tipos y rangos, y queda en la auditoría.
- **Cómo busca.** `programar_meta` corre en cada vuelta del programador desde `DESCUBRIMIENTO_HORA`, con un bloqueo consultivo propio. Lanza una búsqueda a la vez, cada una con **un tipo y un municipio**: así cada búsqueda lee páginas de ese caso, porque una búsqueda solo lee 8 páginas. Recorre las combinaciones en orden, día tras día. Se detiene al llegar a la meta, al tope de búsquedas del día o tras 3 búsquedas fallidas seguidas (pausa con motivo; se reanuda al volver a guardar). Necesita el proveedor de IA listo, igual que el descubrimiento automático. Una búsqueda que espera un reintento cuenta como en curso: durante una caída de Gemini no se apilan búsquedas.
- **Qué cuenta.** Contactos nuevos de Descubrimiento creados hoy (hora de Colombia), aceptados a mano o solos.
- **Aceptación sola.** Solo si el nombre, el país (Colombia) y un correo genérico del propio dominio están corroborados (la cita contiene el valor) y la meta no está cumplida. Usa `importar` sin el tope de `DESCUBRIMIENTO_LIMITE_DIARIO`, que sigue rigiendo solo para el descubrimiento automático por variables de entorno. Lo demás queda pendiente de revisión. Los contactos creados pasan por la verificación del correo y, si está encendida, por la aprobación automática de la base legal.
- **Lista de búsquedas.** `GET /descubrimiento/busquedas` devuelve las 50 más recientes (`?limite=` hasta 500), porque la meta puede lanzar decenas al día. Las de la meta traen `meta: true`.
- **Terminal.** Tabla `eventos_trabajador` (nivel, mensaje de hasta 500 caracteres, tarea) con limpieza a los 3 días, y `latidos_trabajador`, que el trabajador actualiza en cada vuelta. El trabajador anota el inicio, el fin con su duración, los reintentos (motivo, intento y hora), los fallos y la espera de cuota. Descubrimiento anota la búsqueda web, las páginas reutilizadas, cuántas empresas devolvió el modelo, las aceptadas solas y el resumen. La meta anota cada búsqueda que lanza y cuándo se cumple. Las tareas de cada minuto (programar envíos, Gmail, liberar tareas) solo se anotan si fallan. Los mensajes los arma el backend con textos fijos y nombres de empresas o búsquedas: nunca correos, teléfonos, claves ni contenido de páginas. El trabajador cuenta como activo con una señal en los últimos 90 s o con una tarea tomada en los últimos 11 minutos (las tareas largas no laten).
- **Pendiente conocido.** El verificador marca como «riesgoso» los correos genéricos info@, ventas@, contacto@, admin@, soporte@ y comercial@. Por eso muchos contactos que crea la meta no serán elegibles para envío aunque tengan la base legal aprobada. Cambiarlo es una decisión del administrador.

