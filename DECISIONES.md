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
