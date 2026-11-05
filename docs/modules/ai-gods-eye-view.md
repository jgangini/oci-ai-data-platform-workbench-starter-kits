# God’s Eye View · Custom layers

[Documentación](../README.md) · [Operación](../operations.md) · [Contratos vigentes](../contracts.md)

God’s Eye View reúne reportes sociales, sensores, evidencia y revisión humana en un mapa compartido. Un marcador, respuesta del modelo o fotografía sintética no acredita una emergencia real. El identificador del módulo es `gods_eye_view`; su integración visual reside en [`modules/ai-gods-eye-view`](../../modules/ai-gods-eye-view).

## Instalación y componentes

La aplicación pública autentica y sirve de proxy al visor privado. Desde **Settings → Application → God’s Eye View · Custom layers**, el administrador selecciona un administrador AIDP y confirma VM privada, computes social/sensores/consulta/agente y costos. La nueva base reserva la red; instalar el módulo no recrea el portal.

El [instalador](../../apps/backend/app/modules/ai_gods_eye_view/runtime/installation.py) verifica el recibo, fuente inmutable y variables del stack Resource Manager original. Solo permite los recursos de su lista de creación y recursos existentes sin cambios, después ejecuta bootstrap AIDP y verificaciones nativas. Guarda recibos e historial de instalación en artefactos, separados de los controles/datos en Gold. No crea otra VM fuera del stack. Ver [operación y recuperación](../operations.md#global-module-lifecycle).

Cerrar el diálogo deja la operación en marcha; reabrir consulta su fase persistida. **Resume deployment** continúa el worker detenido; **Retry deployment** confirma un intento revisado; **Verify installation** observa/verifica sin crear otra instalación. No existe cancelación de instalación en este diálogo. Fuentes: [manager](../../apps/frontend/src/GodsEyeViewModuleManager.tsx), [API del módulo](../../apps/backend/app/modules/ai_gods_eye_view/runtime/module.py).

La imagen se construye a partir del commit/archive upstream y parches verificados de [`upstream.json`](../../modules/ai-gods-eye-view/upstream.json). Bootstrap instala la imagen inmutable de release; no clona una rama mutable ni reconstruye upstream en la VM. La página standalone retenida es un build distinto del visor nativo.

<a id="individual-access-to-the-shared-viewer"></a>
## Acceso individual al visor compartido

Una instalación sirve a todos los lectores. En **Users → Add user** se puede elegir solo este acceso, sin workspace/kit de participante, o combinarlo con kits. No hay selecciones iniciales; la concesión está deshabilitada hasta que el módulo esté listo. **Manage starter kits** permite conceder o revocar a usuarios existentes; no instala el módulo ni concede privilegios OCI/AIDP de administrador.

Los lectores abren `/gods-eye-view/` y usan **Sign in with OCI** en `/viewer/login`. El portal intercambia un código de un solo uso con PKCE, valida el token firmado y verifica cuenta y membresía actuales del grupo lector en cada petición autenticada. Revocar bloquea peticiones posteriores de una sesión abierta. Las contraseñas permanecen en OCI. Administración de fuentes, revisiones y ubicaciones de sensores exige la sesión administradora.

Las claves de firma se obtienen con una petición OCI firmada por el operador existente a `/admin/v1/SigningCert/jwk` del dominio configurado. El portal exige que discovery publique exactamente ese endpoint y mantiene la validación de firma, issuer, audience, nonce y vigencia del token. El dominio puede restringir la lectura anónima de su certificado. Cada callback consume su propia transacción; una respuesta de otra pestaña no elimina la cookie de un inicio de sesión más reciente.

Terraform administra el grupo lector y la aplicación pública de autorización con callback exacto; no tiene client secret ni permisos de aprovisionamiento. Una base antigua necesita incorporar esos recursos/configuración antes del acceso nativo. Actualizar solo la imagen no los crea. Un callback local debe estar registrado explícitamente. Las transacciones de login requieren actualmente un solo worker API.

La tarjeta de acceso presenta nombre/descripción públicos de **Viewer identity**; los datos/configuración siguen protegidos. El formulario administrador vuelve al mismo visor. Fuentes: [login OCI](../../apps/backend/app/api/viewer_auth.py), [membresía](../../apps/backend/app/modules/ai_gods_eye_view/runtime/access.py), [identidad Terraform](../../terraform/i_oci_identity.tf). La aceptación requiere login real autorizado, rechazo de cuenta no aprobada, separación lector/admin y revocación con sesión abierta.

## Fuentes y programación

Social Networks incluye X, Facebook, Instagram y TikTok. Solo X implementa captura `real` con credencial; los demás usan `Synthetic`. Sensores soporta modo sintético para río, lluvia, temperatura, humedad de suelo y viento. X exige acceso/cuota válidos; fallos del proveedor permanecen como errores y se deduplica por ID original.

Un horario controla las cuatro redes y otro los sensores. Se presenta la zona del navegador y se almacena inicio UTC; el intervalo permitido es de 1 a 1440 minutos. Se respetan inicios futuros y se omiten slots perdidos. Guardar no reinicia cursores ni lotes preparados y mantiene pausados/completados los orígenes. Run activa captura; Pause evita capturas nuevas, pero no detiene compute ni elimina checkpoints.

El corpus sintético es finito. Su agotamiento persiste tras Run, cambios de consulta y reinicio. Los diarios de lotes conservan bytes Landing exactos para reintentar sin regeneración; el trabajo posterior puede reanudarse. El intervalo no garantiza latencia completa de publicación. Fuentes: [captura](../../apps/backend/app/modules/ai_gods_eye_view/runtime/capture.py), [sensores](../../apps/backend/app/modules/ai_gods_eye_view/runtime/sensor_capture.py), [X](../../apps/backend/app/modules/ai_gods_eye_view/runtime/x.py).

## Procesamiento, publicación y consulta

Social escribe envelopes CSV UTF-8 con ID y payload JSON; sensores escribe TXT NDJSON UTF-8. Los streams Spark usan volúmenes gobernados y checkpoints separados: no despliegan OCI Streaming. Bronze conserva originales; Silver clasifica/consolida estado actual; Gold guarda publicaciones versionadas. En sensores, timestamp e ID resuelven el dato más reciente y evitan que eventos tardíos lo sustituyan.

El publicador persiste Gold y snapshot Object antes de avanzar el puntero actual. Las revisiones humanas se guardan independientemente y pueden aparecer en una publicación posterior. Nueva publicación no implica backlog vacío. El visor consulta revisiones y obtiene snapshots completados. Fuentes: [pipeline social](../../apps/backend/app/modules/ai_gods_eye_view/runtime/pipeline.py), [pipeline sensores](../../apps/backend/app/modules/ai_gods_eye_view/runtime/sensor_pipeline.py).

El agente consulta las vistas primarias `oci_gold.territorial_incidents`, `territorial_evidence`, `territorial_sensors` y `territorial_event_posts`. Son los nombres actuales del dataset, no aliases de clientes anteriores. Toda consulta queda vinculada a `publication_version`; SQL fijo y parámetros validados acotan el acceso. Referencias y acciones del mapa pertenecen a esa misma versión; las filas anteriores son historial. No se crean copias analíticas en Autonomous. Fuentes: [lectura Gold](../../apps/backend/app/modules/ai_gods_eye_view/runtime/gold_reader.py), [agente](../../apps/backend/app/modules/ai_gods_eye_view/runtime/agent.py).

Los límites actuales incluyen 5.000 registros sociales visibles, 25.000 filas de sensores por microbatch y 100 filas por consulta Gold. Son límites de memoria/respuesta, no una garantía de capacidad de producción ilimitada.

## Controles durables y proyección local

`.control/gods_eye_view/docs/` conserva configuración, revisión, estado y checkpoints con revisiones. Las escrituras usan precondición ETag; editar una revisión vieja devuelve conflicto 409. `posts/nodes/` es un diario inmutable y `posts/head.json` avanza condicionalmente. La VM verifica hashes/ancestros y proyecta páginas en SQLite, reconstruible desde el diario; Spark no necesita SQLite.

Las instalaciones actuales inicializan el runtime Object con prueba de ausencia de estado previo o un recibo propio válido. Los formatos anteriores se rechazan sin migración ni fallback Oracle. Las escrituras de varios objetos no constituyen una transacción SQL. Fuentes: [control store](../../apps/backend/app/modules/ai_gods_eye_view/runtime/control_store.py), [índice](../../apps/backend/app/modules/ai_gods_eye_view/runtime/post_index.py), [contratos](../contracts.md#datos-y-evidencia).

## Workspace y compute

El destino estable es `/Workspace/medallion/gods_eye_view/`:

| Ruta | Contenido |
| --- | --- |
| `10_bronze/social_network.py` | Captura/procesamiento/publicación social autocontenidos. |
| `10_bronze/sensor_stream.py` | Historial/estado actual de sensores. |
| `20_silver/README.md`, `30_gold/README.md` | Etapas y tablas implementadas. |
| `40_report/ai_gods_eye_view.py`, `requirements.txt` | Agente Gold versionado y dependencias. |
| `README.md`, `manifest.json` | Uso e integridad de despliegue. |

Las [fuentes del paquete](../../apps/backend/app/modules/ai_gods_eye_view/package/notebooks) contienen Python legible, sin bundles ocultos ni descubrimiento dinámico de hermanos. Packaging rellena solo `RUNTIME_CONFIG` no secreto. `python scripts/modules/ai_gods_eye_view/render_gods_eye_view_runtime.py --check` comprueba deriva del código/manifiesto sin reescribir. Detener consumidores afectados antes de sustituir una ruta estable.

Los jobs son `wf_ai_gods_eye_view_social_network` y `wf_ai_gods_eye_view_sensor_stream`, tareas `social_network` y `sensor_stream`. Se adoptan IDs reconocidos sin duplicarlos. Los computes `aidp_gods_eye_view_{social,sensor,query,agent}_compute` separan escritores, consulta y agente. Por defecto Spark usa driver 2 OCPU/16 GiB y un worker igual; AI Compute usa 1 OCPU/16 GiB. Son valores iniciales, no tamaños óptimos medidos. Pausar captura no detiene compute; la consulta requiere Query activo. Fuentes: [bootstrap](../../terraform/hooks/gods_eye_view_bootstrap.py), [renderizador de workflows](../../terraform/hooks/gods_eye_sources.py), [renderizador de agente](../../terraform/hooks/gods_eye_agent_source.py).

## Evidencia, revisión y limpieza sintética

La salida nativa identifica workflow, etapa, revisión, lote/conteos y publicación. Compararla con `status_pipeline` y el snapshot actual: una tarea RUNNING no demuestra datos recientes. Confianza, severidad, actividad, diversidad y validación humana son conceptos distintos; copias de una foto o varias redes no prueban corroboración independiente.

El corpus incluye puntos de vista ficticios, contradicciones e imágenes generadas, no prueba documental. La clasificación lee texto, no píxeles; un hash identifica copias, no un mismo incidente. El administrador confirma validación/rechazo, notas y ubicación; una ubicación validada debe desvalidarse antes de editar. Los informes son borradores y no se envían automáticamente.

Eliminar Synthetic social abarca todas las redes y conserva sensores. Eliminar sensores abarca sus cinco familias y conserva social, datos reales y ubicaciones guardadas. Una operación antigua por familia mantiene su alcance. Eliminación nueva/reintento destructivo exige confirmación, UUID y `confirm: true`; el retry conserva ID/alcance. Un ID completado no puede borrar datos nuevos.

Cerrar el diálogo no cancela. Cancelación terminal requiere detener el escritor nativo; conserva progreso parcial, no hace rollback ni se informa como limpieza completa. El ID cancelado no se reintenta; exige nueva confirmación/ID. Las capturas quedan pausadas. La limpieza conserva filas reales en datos mixtos y escribe reemplazos durables antes de borrar originales. No limpiar checkpoints para ocultar fallos. Fuentes: [social](../../apps/backend/app/modules/ai_gods_eye_view/runtime/synthetic_reset.py), [sensores](../../apps/backend/app/modules/ai_gods_eye_view/runtime/sensor_reset.py), [UI](../../apps/frontend/src/SyntheticDataReset.tsx).

<a id="security-and-acceptance"></a>
## Seguridad y aceptación

Visor y administración permanecen detrás del portal autenticado; la VM del visor es privada. Claves/tokens no llegan al navegador ni fuentes publicadas. `AidpRuntime` proporciona OCI; credenciales con base de datos no son fallback. La memoria nativa AIDP es un servicio aparte.

Los checks locales cubren [CAS](../../apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_control_store.py), [índice](../../apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_object_post_index.py), [grounding](../../apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_agent_tools.py), [cancelación](../../apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_reset_cancelled.py) y [fuentes standalone](../../terraform/tests/test_gods_eye_sources.py). No certifican permisos, inferencia ni conversación nativa. Aceptar tareas reales, publicación/evidencia y conversación Gold de la revisión instalada antes de habilitar consumidores. Consultar [Desarrollo](../development.md) y [build/licencias del visor](../../modules/ai-gods-eye-view/README.md).
