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
