# Desarrollo y validación

[Documentación](README.md) · [Operaciones](operations.md) · [Contratos vigentes](contracts.md)

<a id="prerequisites-and-profiles"></a>
## Requisitos y perfiles

Use Git y Docker con Compose para ejecución local. CI usa Python 3.12 y Node 22 para el portal, Terraform 1.5.7 y 1.15.7 y el proveedor OCI fijado en [`a_versions.tf`](../terraform/a_versions.tf). El visor usa Node 24.14.0 en su [Dockerfile](../modules/ai-gods-eye-view/Dockerfile); su [guía de integración](../modules/ai-gods-eye-view/README.md) define la compilación upstream fijada.

| Perfil | Configuración | Alcance |
| --- | --- | --- |
| Datos locales de prueba | `docker/docker-compose.dev.yml`, `.env.dev` privado | Portal, API, integración del visor y comportamiento determinista |
| Aplicación local conectada a OCI | `docker/docker-compose.oci-local.yml`, montajes protegidos | Actúa sobre una instalación real existente; no es una copia aislada |
| VM desplegadas en OCI | Terraform e imágenes de versión inmutables | Infraestructura y aplicación reales; cada módulo requiere aceptación separada |

<a id="start-local-fixtures"></a>
## Iniciar las pruebas locales

Desde la raíz, copie el ejemplo únicamente si no existe su archivo privado:

```powershell
if (-not (Test-Path .env.dev)) { Copy-Item .env.example .env.dev }
$env:APP_RELEASE_SHA = git rev-parse HEAD
python scripts/release/write_package_versions.py
docker compose -f docker/docker-compose.dev.yml up --build -d
docker compose -f docker/docker-compose.dev.yml ps
```

Abra `http://localhost:18081`. El perfil escucha en loopback y usa HTTP sin instalar certificados. Los hashes de ejemplo de [`.env.example`](../.env.example) son demostrativos; configure sus valores locales con [`hash_secret`](../apps/backend/app/security.py). Mantenga `.env.dev`, cuentas y estado generado fuera de Git.

El generador de fechas usa el último cambio Git del manifiesto y verifica el hash del paquete. Compose pasa `APP_RELEASE_SHA` a la compilación para vincular los metadatos al commit seleccionado. La imagen sigue siendo `development` y puede incluir cambios locales de aplicación. Un paquete modificado sin commit carece de fecha verificada y muestra **Not available**: no sustituya esa ausencia por la hora de compilación o del sistema de archivos.

Los perfiles Compose activos y recibos locales pueden vivir en `.local/`, ignorado por Git. Los archivos de bienvenida usan un volumen Docker del proyecto. Los datos locales de identidad guardan ese archivo sin enviar correo, aunque el diálogo no muestre detalles internos de entrega. Los estados de instalación que requieren acción se conservan; Governance y Market Intelligence habilitan su enlace validado a Agent Hub únicamente con despliegue verificado del módulo y una URL confiable.

Para detener el perfil:

```powershell
docker compose -f docker/docker-compose.dev.yml down
```

No añada `--volumes` salvo que quiera eliminar datos persistentes. Este perfil no valida Identity Domains, IAM, Spark, modelos ni memoria de agentes AIDP reales.

<a id="connect-a-local-app-to-an-existing-oci-deployment"></a>
## Conectar la aplicación local a OCI

El [asistente de configuración](../scripts/development/bootstrap_local_oci_env.py) usa un perfil y clave de operador y un correo de acceso de un despliegue correcto. Proteja el correo y el `.env` resultante.

```powershell
python scripts/development/bootstrap_local_oci_env.py --config <oci-config> --key <oci-key.pem> --access-email <deployment-email.html>
docker compose --env-file .env -f docker/docker-compose.oci-local.yml up --build --detach
```

La URL predeterminada es `http://127.0.0.1:18082`. Compose monta la clave seleccionada en modo de solo lectura. Para un runtime Object actual de God's Eye View, añada `--gods-eye-control-bucket <gold-bucket-name>`; use `medallion_bucket_names.gold` del despliegue. El asistente verifica el bucket y escribe `GODS_EYE_CONTROL_BUCKET` explícitamente, sin inferirlo del bucket Landing. Verifique los [contratos de controles](contracts.md#datos-y-evidencia) antes de conectar consumidores.

```powershell
docker compose --env-file .env -f docker/docker-compose.oci-local.yml down
```

El controlador externo de vistas previas de Deploy Studio no pertenece a este repositorio; sus rutas y puertos particulares no forman parte del contrato del proyecto.

<a id="test-native-module-installation-locally"></a>
### Probar la instalación nativa de módulos

Con una instalación AIDP real configurada, localhost usa los controles nativos de producción. En **Settings → Application**, seleccione un administrador AIDP y revise los recursos antes de **Deploy**: ese botón inicia la operación real y puede crear recursos facturables. Abrir el diálogo solo consulta configuración. El monitor muestra fases del servidor y progreso indeterminado. God's Eye View recupera su operación durable tras navegar o recargar; Governance conserva el identificador de operación del espacio y requiere **Resume** para continuar tras salir de Settings. El runtime explícito `local_fixture` usa datos aislados y no aprovisiona OCI.

<a id="repository-and-aidp-resources"></a>
## Organización del backend y recursos AIDP

La [composición de la aplicación](../apps/backend/app/main.py) conserva el montaje explícito de rutas y factories. La distribución actual agrupa responsabilidades:

| Ubicación bajo `apps/backend/app/` | Responsabilidad actual |
| --- | --- |
| `kits/{banking,telecommunications,telco_lineage,retail,healthcare}/` | Paquetes de participantes con manifiesto, fuentes y notebooks; `kits/naming.py` mantiene convenciones de destino. |
| `modules/ai_data_governance/` | Agentes, modelo y reconciliación única de Governance; fuentes/recursos declarados en `aidp/`. |
| `modules/ai_market_intelligence/` | API en `api.py`, cliente en `client.py`, instalación en `installation/`. |
| `modules/ai_gods_eye_view/` | Dominio, controles, captura, publicación, consulta e instalación del visor. |
| `modules/<módulo>/package/` | Metadatos versionados en `lab.json`; Governance y Market mantienen sus fuentes y recursos en `aidp/` junto a `package/`. |
| `packages/catalog.py`, `packages/catalog.json` | Resolución explícita y validación común de paquetes; `load_package()` no descubre módulos arbitrarios. |
| `services/oci/{aidp,identity,autonomous,credential_bootstrap}.py` | Capacidades OCI compartidas; `services/releases.py` mantiene el servicio de releases. |
| `api/{viewer_auth,viewer_identity}.py` | Acceso y presentación de identidad del visor; las rutas retiradas no se montan y devuelven 404. |

El [registro](../apps/backend/app/packages/catalog.py) resuelve los cinco kits desde `kits/` y los tres paquetes globales desde un mapa de rutas explícito. Valida `lab_id`, versión, scope y hashes del contrato existente. El directorio interno `ai_gods_eye_view` se publica con identidad de paquete `gods_eye_view`; los nombres de carpeta no redefinen IDs cloud.

[`AidpClient`](../apps/backend/app/services/oci/aidp.py) sigue siendo una fachada que reúne transporte y orquestación de participantes/Governance. Las excepciones y resultados compartidos se definen en [`contracts.py`](../apps/backend/app/services/oci/contracts.py), sin dependencia de clientes. Los módulos aún utilizan servicios y métodos de la fachada; agrupar archivos no elimina ese acoplamiento. Para la siguiente extracción interna, separar una capacidad concreta —workspace, catálogo, jobs, compute, agentes o permisos— conservando la fachada y sus consumidores, y comprobar comportamiento antes de continuar. No crear una capa nueva por cada método ni presentar ese desacoplamiento como ya implementado.

God’s Eye View separa su código Python en `modules/ai_gods_eye_view/runtime/` y sus assets AIDP en `package/`. Esto permite declarar un cierre de dependencias de post-apply con menos de 20 rutas, sin incorporar el corpus completo al paquete del runner. Las pruebas importan ese cierre aislado; el Dockerfile del visor reutiliza únicamente sus helpers compartidos con el mismo namespace Python.

La aplicación visual permanece en [`modules/ai-gods-eye-view`](../modules/ai-gods-eye-view), con build, dependencias upstream e imagen propios. Su VM privada se instala en el stack original. El paquete Python del backend administra el dominio; incorporar el visor completo a él no convertiría ambas aplicaciones en un único runtime.

### Relación con los recursos nativos

Oracle distingue archivos/carpetas/notebooks del workspace, workflows y computes vinculados, y datasets referenciados como `Catalog.Schema.Table`. Una carpeta no crea automáticamente un catálogo, esquema, tabla, compute ni agente. Ver [capacidades de AIDP](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/features-oracle-ai-data-platform.html) y [workspaces](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/workspaces.html).

El mapeo implementado se realiza mediante el loader, generadores e instaladores:

```text
Repo: kits/<kit>/ o modules/<módulo>/ (metadata en package/lab.json)
  lab.json + assets verificados
    → cargador + instalador/generador del dominio
    → archivos/notebooks en rutas Workspace explícitas
    → workflow: tareas, dependencias y claves de compute/agente explícitas
    → acceso a datasets mediante Catalog.Schema.Table y permisos nativos
```

Los kits publican bajo `/Workspace/<lab_id>/<participant_key>_<email>` y usan `oci_medallion.oci_<layer>.<tabla_prefijada>`. Governance y Market publican bajo `/Workspace/.artifact/<module_id>/`; God’s Eye View publica sus tres programas bajo `/Workspace/medallion/gods_eye_view/`. Estas rutas son contratos de sus instaladores, no una sincronización literal del árbol Git. Los notebooks de participantes `2.0.0` todavía exigen rutas/catálogos distintos: sigue pendiente [reconciliar y aceptar ese contrato](getting-started.md#current-implementation-limits).

### Contrato común de publicación

Governance y Market mantienen una única implementación. `aidp/manifest.json` declara identidad, raíz de publicación, assets editables/generados y recursos. El árbol `workspaces/default/files/` conserva la estructura relativa publicada bajo `/Workspace/.artifact/<module_id>/`. `default` es un alias local del workspace seleccionado, no un ID nativo ni una orden para crear otro workspace.

```text
aidp/
  manifest.json
  master_catalog/<catalogo>/schemas/<esquema>/tables/<tabla>.json
  workspaces/default/
    files/agents/<agente>/
    workflows/<workflow>.json
    compute/<spark>.json
    agent_flow/agents/<agente>.json
    agent_flow/compute/<ai_compute>.json
```

Los archivos describen recursos nativos: copiar un JSON no crea un workflow, tabla ni compute. Los instaladores consumen sus definiciones y vinculan identidades existentes verificadas. Governance tiene dieciocho tablas, dos agentes y cuatro tareas; Market declara cero tablas propias y cero workflows Spark, porque su grafo LangGraph no es un job Spark.

Cada asset editable tiene una única fuente. Los programas generados declaran sus fuentes de ensamblado y los bytes de destino reciben hashes después de configurar referencias no secretas. Las definiciones de tablas de Governance se agregan desde los JSON individuales; no existe otro modelo editable. Cambiar fuentes exige regenerar hashes de `package/lab.json` y actualizar la versión de paquete cuando corresponda.

Comprobación offline, sin crear recursos ni invocar modelos:

```powershell
python scripts/validation/check_aidp_modules.py
```

Después de una modificación intencional, `--refresh-hashes ai_data_governance` o `--refresh-hashes ai_market_intelligence` actualiza únicamente las fuentes declaradas; no incorpora archivos encontrados en el directorio. Para preparar una nueva versión de paquete, añadir `--pack-version <major.minor.patch>`. Revisar el diff y ejecutar las regresiones antes de publicar; no cambiar el contenido de una release ya publicada.

El comando valida manifiestos, rutas, descriptores, hashes y sintaxis Python. No acredita ejecución AIDP. El plugin AIDP Agent Flow LangGraph requiere una factory explícita `factory(runtime) -> compiled_graph` para su runner local; una entrada nativa con `setup`/`invoke` no basta. Utilice solo adaptadores verificados con servicios/modelo/checkpointer inyectados. Los constructores de Governance reciben hooks de autorización y persistencia explícitos; no se deben sustituir por servicios simulados para aparentar compatibilidad nativa.

### Pruebas y herramientas de evaluación

Las pruebas del backend se agrupan en `tests/modules/<módulo>/`, `tests/api/`, `tests/packages/`, `tests/services/` y `tests/core/`; `tests/fixtures/` contiene datos auxiliares. `tests/support/ai_dg_fixtures.py` concentra las bindings y fixtures compartidas de actividad/casos de Governance, sin importar módulos `test_*.py`. Los helpers versionados que consumen las CLI de evaluación permanecen en la raíz de `tests/` para conservar sus rutas de integración; no son paquetes de producto duplicados. Un bridge cuyo hash no coincide con la revisión fijada bloquea la evaluación; verificar la fuente aprobada antes de aceptar un resultado, conservando ese bloqueo.

Los recibos que fijan `factory_sha256`, `adapter_sha256` o `candidate_revision` solo son válidos mientras coincidan las fuentes y el candidato originales. El [factory de evaluación](../apps/backend/app/modules/ai_market_intelligence/aidp/workspaces/default/files/agents/ai_market_intelligence/ai_market_intelligence/factory.py) lo comprueba. Si cambian imports/código del helper, las aceptaciones anteriores pueden dejar de ser utilizables aunque el cambio sea organizativo. Revisar y repetir la evaluación pertinente; no sustituir hashes ni regenerar aceptación para ocultar una discrepancia.

### Herramientas por responsabilidad

Los únicos entrypoints de arquitectura en la raíz de `scripts/` son `arch-preflight.ps1` y `arch-postflight.ps1`; ambos llaman `validation/sentrux_scope.py` y preservan la referencia aprobada. Las demás herramientas se agrupan sin wrappers para las rutas anteriores:

| Directorio bajo `scripts/` | Uso |
| --- | --- |
| `validation/` | Preparar el ámbito de código para Sentrux y validar los manifiestos AIDP offline. |
| `development/` | Configurar OCI local y comprobar operaciones antes de reiniciar. |
| `release/` | Cargar imágenes verificadas, actualizar VMs y generar fechas de paquetes. |
| `deployment/` | Instalar/renovar TLS de IP pública. |
| `kits/` | Generar determinísticamente Telco Customer 360. |
| `modules/ai_data_governance/` | Verificar grafos, renderizar el modelo y evaluar sin efectos nativos. |
| `modules/ai_market_intelligence/` | CLI TEST, invocación/pilotos, evaluación sobre corpus sellado y reductor streaming de referencia. |
| `modules/ai_gods_eye_view/` | Renderizar el runtime, evaluar corpus y smoke de API/navegador local. |

La CLI Market comparte helpers colocados junto a su entrypoint; los tres scripts de imágenes también permanecen juntos para las copias standalone instaladas en la VM. Mover herramientas exige actualizar callers CI/Terraform y sus raíces de repositorio, sin cambiar las fuentes AIDP ni los fingerprints de aceptación para ocultar discrepancias.

<a id="validate-a-change"></a>
## Validar un cambio

Lea las instrucciones del repositorio y ejecute comprobación previa antes de cambiar código o infraestructura y comprobación posterior al terminar. Los scripts preservan las referencias de Sentrux: use `scripts/arch-preflight.ps1 -PreserveBaseline -Python <python-exe>` y su postflight correspondiente. `scripts/validation/sentrux_scope.py` incluye código versionado y código nuevo no ignorado, excluye contenido privado, generado y repositorios anidados y no modifica el índice Git. Herramientas ausentes y degradación hacen fallar el flujo.

La referencia aprobada es el commit `7ac5126`, CLI 0.5.7, con 117 funciones complejas. La referencia original se conserva en `.sentrux/history/2026-10-02.baseline.json`; `.sentrux/reference.json` registra hashes y procedencia. Aceptar deuda previa no significa resolverla: no recalibre usando el candidato modificado.

<a id="agent-size-and-architecture-rules"></a>
### Reglas de arquitectura y tamaño de agentes

La [política Sentrux](../.sentrux/rules.toml) permite entradas nativas y prompts extensos; omite deliberadamente `max_file_lines` y `max_fn_lines`. No divida un runtime cohesivo solo para reducir líneas. Se exige cero ciclos (`max_cycles = 0`) y ausencia de archivos con dependencias salientes excesivas (`no_god_files = true`, mide fan-out en 0.5.7). La longitud no exime de revisar complejidad, dependencias o seguridad.

La puerta de regresión conserva sus comprobaciones: caída máxima de calidad de 0.02 y aumento máximo de acoplamiento de 0.05, en unidades absolutas; cualquier aumento de ciclos, archivos con dependencias excesivas o funciones complejas falla. No se añade un umbral absoluto de acoplamiento. Consulte las implementaciones fijadas de [reglas](https://github.com/sentrux/sentrux/blob/f36da08a53e7f06c5b6aec33eb05816b371000ea/sentrux-core/src/metrics/rules/checks.rs) y [regresión](https://github.com/sentrux/sentrux/blob/f36da08a53e7f06c5b6aec33eb05816b371000ea/sentrux-core/src/metrics/arch/mod.rs).

Sentrux 0.5.7 puede ignorar claves TOML desconocidas. Las [pruebas de arquitectura](../apps/backend/tests/services/test_arch_validation.py) verifican claves admitidas y casos de agente de 10,000 líneas, ciclo y dependencias excesivas. Omitir estos casos por falta del ejecutable no completa preflight/postflight.

<a id="graphify-map-for-module-development"></a>
### Mapa Graphify para desarrollo

[`.graphifyignore`](../.graphifyignore) delimita código de los tres runtimes —Governance, Market Intelligence y God's Eye View—, servicios, catálogo, API, pruebas seleccionadas, frontend y hooks de despliegue. Excluye notebooks empaquetados, datasets, documentos, estado privado, repositorios anidados, dependencias y archivos generados. Sentrux sigue cubriendo el código completo. Consulte los conteos del resultado actual tras una actualización; no reutilice las métricas de un alcance anterior.

La configuración usa Graphify 0.9.93 sin dependencia adicional de aplicación, petición LLM externa ni hook Git. Solo para preparar el mapa inicialmente, con CLI instalada y desde esta raíz:

```powershell
graphify extract . --code-only --max-workers 2
graphify cluster-only . --no-label
graphify export wiki
```

Después de cambios de fuente use el postflight canónico: ejecuta `graphify update .`, `graphify export wiki`, reglas y puerta de regresión Sentrux. No indexe el workspace contenedor. `graphify-out/` permanece ignorado; consulte `GRAPH_REPORT.md` y `wiki/index.md`. `graphify export html` es opcional y requiere revisión en Browser si se genera. Las relaciones AST ayudan a navegar, pero no prueban transiciones LangGraph, autorización MCP, streaming ni instalación. Verifique las aristas inferidas contra el código; referencias externas pueden carecer de ubicación y llamadas recursivas pueden crear bucles propios.

### Comprobaciones funcionales y de publicación

Use pruebas pertinentes durante el desarrollo y la matriz CI antes de publicar. Seleccione un directorio `--basetemp` **nuevo e inexistente** para cada ejecución: pytest borra uno existente.

```powershell
./scripts/arch-preflight.ps1 -PreserveBaseline
python -m pip install -r apps/backend/requirements-dev.txt
python -m pytest apps/backend/tests terraform/tests --basetemp=.local/pytest-new-run
Push-Location apps/frontend
npm ci
npm test
npm run build
Pop-Location
terraform -chdir=terraform fmt -check -recursive
terraform -chdir=terraform init -backend=false
terraform -chdir=terraform validate
terraform -chdir=terraform test
$env:APP_RELEASE_SHA = git rev-parse HEAD
python scripts/release/write_package_versions.py
docker build --build-arg APP_RELEASE_SHA=$env:APP_RELEASE_SHA -f docker/Dockerfile -t aidp-lab:test .
docker build -f modules/ai-gods-eye-view/Dockerfile -t gods-eye-view:test .
./scripts/arch-postflight.ps1
```

Ejecute `terraform test` con la versión más reciente probada por CI. La imagen del visor ejecuta integración contra dependencias upstream fijadas; otro árbol `node_modules` no equivale a esa prueba. El [cargador de paquetes](../apps/backend/app/packages/catalog.py) valida manifiestos, hashes, filas CSV y dependencias; no reemplaza la [aceptación nativa del contrato de rutas](getting-started.md#current-implementation-limits). Informe herramientas ausentes o puertas fallidas sin reiniciar la referencia para ocultarlas.

Para cambios de módulos administrados por el portal, ejecute también `python terraform/tests/check_module_plan.py`: usa proveedores simulados y solo permite crear la VM del visor, su grupo dinámico y dos políticas. Las [pruebas del instalador](../apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_installation.py) cubren identidad, recuperación y restricciones del plan; las [del frontend](../apps/frontend/tests/gods-eye-view-module-manager.test.mjs), confirmación y progreso. No certifican OCI real. `enabled_vm_modules` admite únicamente `gods_eye_view`; otro módulo exige recursos, ciclo de vida, empaquetado y lista permitida explícitos.

Los cambios exclusivamente documentales requieren comprobar enlaces, encabezados, diagramas y afirmaciones contra código; no requieren compilaciones ni puertas de arquitectura salvo cambios ejecutables, de configuración o paquetes. [CI](../.github/workflows/ci.yml) es la automatización canónica; [Release](../.github/workflows/release.yml) añade reglas de etiquetas y artefactos.

<a id="check-object-controls"></a>
## Controles Object

God's Eye View conserva configuración y diario inmutable en Object Storage. SQLite es una proyección reconstruible; probar ETags obsoletos, escritores concurrentes, límites de secuencia y cancelaciones terminales con almacenes simulados e índices nuevos. El runtime no selecciona controles Oracle ni migra formatos anteriores.

```powershell
$env:PYTHONPATH = "apps/backend"
python -m pytest apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_control_store.py apps/backend/tests/modules/ai_gods_eye_view/test_gods_eye_view_object_post_index.py --basetemp=.local/pytest-controls-new-run -o addopts= -q
```

Estas pruebas no certifican permisos OCI, tareas Spark ni publicaciones reales. La aceptación exige salida nativa y acuerdo entre versión, snapshot y evidencia antes de habilitar consumidores.

<a id="change-packages-and-ai-workflows-safely"></a>
## Modificar paquetes y flujos de IA

- Los cinco kits mantienen contratos bajo `apps/backend/app/kits/<kit>/` y los tres módulos bajo `apps/backend/app/modules/<módulo>/package/`, resueltos por [el registro común](../apps/backend/app/packages/catalog.py). Consulte las [guías de módulos](README.md#shared-global-modules) para distinguir configuración y aceptación nativa. Cambie recursos mediante su generador o fuente y actualice versión, hashes y resultados juntos; no cambie bytes de una versión publicada.
- Alinee parámetros, comprobaciones de notebooks y nombres del aprovisionador; verifique el flujo emitido y una ejecución nativa.
- God's Eye View publica un `.py` legible por flujo y una entrada de agente desde sus [notebooks empaquetados](../apps/backend/app/modules/ai_gods_eye_view/package/notebooks). Regenere con `python scripts/modules/ai_gods_eye_view/render_gods_eye_view_runtime.py`; `--check` detecta desviaciones sin escribir. El despliegue conserva el cuerpo y sustituye solo `RUNTIME_CONFIG` explícito sin secretos; no se garantiza igualdad de todo el archivo entre entornos ni se oculta carga dinámica dentro de un flujo.
- Las [entradas y definiciones de Governance](../apps/backend/app/modules/ai_data_governance/aidp) se mantienen como fuentes canónicas. Actualice hashes al cambiar el paquete; el despliegue sustituye solo `CONFIG`. Las [pruebas del paquete](../apps/backend/tests/modules/ai_data_governance/test_ai_dg_standard_layout.py) verifican cuerpo y ejecución configurada; no hay un segundo renderizador o plantilla duplicada.
- Market Intelligence declara su runtime en `aidp/workspaces/default/files/agents/` y sus hashes en `package/lab.json`; los adaptadores están en `app/modules/ai_market_intelligence/installation/`. CI prueba sus datos LangGraph fijados por separado del servicio Social retirado. MCP, memoria e instalación nativos requieren aceptación separada.
- Verifique autorización, límites de consulta, evidencia/versiones y etiquetas sintéticas independientemente de la respuesta del modelo. Preserve límites tipados, series temporales completas, reintentos idempotentes y aclaración terminal tras reparaciones de plan fallidas.
- No incluya credenciales DB, wallets ni claves privadas en documentos, eventos o fuente generada. `AidpRuntime` suministra acceso OCI; Object Storage conserva controles. Acepte solo IDs, sesiones, variables y rutas canónicos; centralice sus formatos en [Contratos vigentes](contracts.md).

<a id="documentation-ownership"></a>
## Organización de la documentación

El README raíz presenta el proyecto; `getting-started.md` concentra preparación compartida, `docs/README.md` distribución y límites, `operations.md` procedimientos, `development.md` desarrollo y `kits/` o `modules/` comportamiento específico. Mantenga las guías en español y los identificadores, comandos y etiquetas de interfaz tal como existen. Enlace cada afirmación de comportamiento a manifiestos, implementación o pruebas. No convierta diagnósticos de una revisión, métricas históricas o recibos privados en instrucciones vigentes; tampoco presente una degradación anterior como validación aprobada. Preserve las referencias de arquitectura y documente el resultado de cada comprobación en su informe correspondiente.
