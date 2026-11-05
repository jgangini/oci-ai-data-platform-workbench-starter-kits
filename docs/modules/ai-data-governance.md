# AI Data Governance

[Documentación](../README.md) · [Operación](../operations.md) · [Contratos vigentes](../contracts.md)

AI Data Governance mantiene una única implementación: dos agentes, dieciocho tablas framework y un workflow de cuatro tareas. El agente consultivo responde sobre metadatos con evidencia; el agente documental prepara descripciones. Su ID es `ai_data_governance`; los participantes no reciben administración del módulo.

## Fuentes y publicación

El [manifiesto AIDP](../../apps/backend/app/modules/ai_data_governance/aidp/manifest.json) relaciona cada fuente editable o programa generado con su destino. Las entradas, prompts y requisitos están bajo `aidp/workspaces/default/files/agents/`. Los helpers mantenidos en Python se enumeran como dependencias de ensamblado; no hay otra copia editable del runtime publicado.

El [modelo canónico](../../apps/backend/app/modules/ai_data_governance/aidp/master_catalog/oci_medallion/schemas/oci_artifacts) contiene `schema.json` y un JSON por tabla en `tables/`. `schema.py` los agrega y valida nombres, columnas, descripciones y referencias. El agregado no es un segundo modelo editable. Las tablas usan `oci_medallion.oci_artifacts` y el bucket de artefactos configurado; el nombre del esquema no determina el bucket físico.

Las definiciones de workflow, Spark compartido, AI Compute y agentes están en `aidp/workspaces/default/`. El alias local `default` se vincula al workspace seleccionado en la configuración; no obliga a crear otro workspace.

El [ensamblador](../../apps/backend/app/modules/ai_data_governance/deployment_bundle.py) consume esas definiciones, exige identidades explícitas sin secretos y produce código legible con hashes bajo `/Workspace/.artifact/ai_data_governance/`:

| Destino relativo | Contenido |
| --- | --- |
| `agents/governance/` | Entrada y runtime del agente consultivo. |
| `agents/documentation/` | Entrada y runtime del agente documental. |
| `tasks/` | Inventario, publicación y réplica. |
| `bootstrap18tables.py` | Inicialización aditiva del modelo. |
| `workflow.json`, `manifest.json` | DAG y evidencia de los archivos configurados para el destino. |

## Instalación, permisos y aceptación

En Settings → Application se selecciona un administrador AIDP verificado y se confirma Deploy. El [instalador único](../../apps/backend/app/modules/ai_data_governance/deploy_studio_install.py) conserva operación, recursos, intenciones y recibo para reanudar. GET solo observa; HTTP 202 indica trabajo pendiente. El usuario operador y la credencial OCI compartida `AidpRuntime` deben conservar su identidad; entradas duplicadas, permisos insuficientes o recursos ajenos bloquean la operación.

La preparación de archivos precede al registro de intención de crear cada agente. Una espera de publicación de archivos permite reanudar la misma operación; una respuesta incierta después de enviar el POST mantiene el bloqueo para evitar duplicados. El readback compara código y contenido bruto byte a byte; cuando la API devuelve un objeto JSON ya interpretado, compara su contenido JSON conservando tipos y valores, sin exigir el orden de claves ni los espacios originales.

La verificación del workflow admite únicamente los campos `clusterName` y `newCluster` con valor `null` que AIDP añade a las referencias de compute. Conserva la comparación de claves, número y orden de tareas, dependencias, condiciones de ejecución, reintentos, tiempos y horario. Una configuración de compute adicional no vacía se considera deriva y bloquea la ejecución.

El agente consultivo requiere `deployment_stage=TEST`. La instalación no acredita calidad semántica, permisos de un segundo usuario ni recuperación real. Los accesos de producción y las operaciones de mantenimiento no aceptadas permanecen bloqueados. Cambiar carpetas locales no adopta ni migra una instalación remota.

El worker documental tiene un despliegue de ejecución propio para el workflow; esto no publica el agente consultivo en PROD.

`wf_ai_dg_sync_master_catalog` contiene exactamente cuatro tareas:

| Orden | Tarea | Tipo |
| --- | --- | --- |
| 1 | `sync_inventory` | `PYTHON_TASK` |
| 2 | `document_metadata` | `AGENT_TASK`, con entrega documental inmutable. |
| 3 | `publish_metadata` | `PYTHON_TASK` |
| 4 | `replicate_model` | `PYTHON_TASK`, dependencia declarada `ALL_DONE`. |

Permite una ejecución activa, sin cola y con reintentos acotados. Solo habilita la programación UTC cada treinta minutos después de verificar la primera ejecución del grafo exacto. Fuentes: [workflow](../../apps/backend/app/modules/ai_data_governance/workflow.py), [instalador](../../apps/backend/app/modules/ai_data_governance/deploy_studio_install.py).

Los ámbitos de origen se descubren y vinculan explícitamente al agente; incorporar otro esquema exige regenerar y revisar esa vinculación. El documental rellena solo descripciones vacías, identifica texto generado con `[IA]` y conserva descripciones existentes, incluidas las que ya tienen esa marca. Debe declarar vacíos de evidencia. La consulta cubre inventario, significado, uso en código/tareas, calidad, linaje e impacto hasta donde permitan las fuentes y la aceptación nativa.

Completar la instalación conserva `validation_stage=TEST_PENDING`. El endpoint consultivo para Agent Hub sigue bloqueado hasta aceptación y una publicación explícitamente implementada. Continúan pendientes calidad útil de respuestas y recuperación completa. Un throttle del modelo es una falla de disponibilidad, no una respuesta aceptada. Las escrituras inciertas conservan su identidad y bloquean creación duplicada.

## Verificación

Comprobar inventario, linaje de una tabla inequívoca, protección del esquema de control, rechazo de SQL arbitrario, conservación/relleno documental, ámbitos de origen, respuestas útiles y recuperación de tareas fallidas. HTTP 401/403 exige comprobar sesión y rol; HTTP 202 exige observar la operación original; fallos de credencial exigen comprobar nombre, estado e identidad, sin rotación automática.

Los [tests del estándar](../../apps/backend/tests/modules/ai_data_governance/test_ai_dg_standard_layout.py), [tests de ciclo de vida](../../apps/backend/tests/modules/ai_data_governance/test_governance_lifecycle.py), demás checks del [backend](../../apps/backend/tests) y el [manifiesto](../../apps/backend/app/modules/ai_data_governance/package/lab.json) son contratos ejecutables. Los fixtures locales no certifican permisos, ejecución Spark/agente ni calidad semántica. Usar el [flujo de validación](../development.md) y mantener registros específicos del destino fuera de Git.
