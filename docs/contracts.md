# Contratos vigentes

[Documentación](README.md) · [Operación](operations.md) · [Desarrollo](development.md)

Esta versión admite las interfaces y formatos actuales. No interpreta nombres anteriores como alias, no adopta instalaciones antiguas ni convierte sus manifiestos automáticamente. Un formato no admitido se rechaza; no se borra ni transforma su recurso remoto para forzar aceptación. Actualizar el código no ejecuta una migración OCI.

## API, sesiones y permisos

El visor usa `/gods-eye-view/`, `/admin/gods-eye-view`, `/api/gods-eye-view/*` y `/api/admin/gods-eye-view/*`. Los accesos de prueba local usan `/api/local/gods-eye-view/*`. Las rutas retiradas, incluidas Social Network y los prefijos de visor anteriores, devuelven HTTP 404 y no ofrecen redirección ni ciclo de vida alternativo.

Las sesiones firmadas contienen exactamente `sub`, `exp` y un `sid` válido. Cookies sin el identificador de sesión actual se rechazan; el usuario debe iniciar sesión de nuevo. Los lectores usan subject `viewer:<id>` o `local-gods-eye-view:<id>` en el entorno local. Los proxies emiten solo `X-Gods-Eye-View-User` y `X-Gods-Eye-View-Role` después de autenticar; esos headers no conceden acceso anónimo. Fuentes: [sesiones](../apps/backend/app/security.py), [acceso al visor](../apps/backend/app/modules/ai_gods_eye_view/runtime/access.py), [login OCI](../apps/backend/app/api/viewer_auth.py).

El campo de acceso lector es `gods_eye_view_access`. La membresía se verifica para la cuenta y el entorno actuales; no se infiere de campos antiguos ni de un enlace Agent Hub. Reintentos conservan la operación y los recursos verificados, sin ampliar permisos.

## Paquetes y destinos AIDP

El [registro de paquetes](../apps/backend/app/packages/catalog.py) resuelve los cinco kits en `app/kits/` y los tres módulos bajo `app/modules/<módulo>/`, con metadatos en `package/lab.json`. Governance y Market mantienen sus fuentes y recursos en `aidp/`; el visor conserva sus assets en `package/`. Los IDs vigentes son `banking`, `telecommunications`, `telco_lineage`, `retail`, `healthcare`, `ai_data_governance`, `ai_market_intelligence` y `gods_eye_view`. Se conservan los campos `lab_id`, `labs` del catálogo y el archivo `lab.json`, que forman el contrato actual. La carpeta Python `ai_gods_eye_view` tiene ID de paquete `gods_eye_view`.

Cada asset declarado debe coincidir con su hash, versión, ámbito y dependencias. Los manifiestos de instalación deben corresponder al formato actual, propietario, destino y recursos exactos; no se busca un manifiesto antiguo en otra ruta ni se adopta por nombre aproximado. Cambiar una carpeta del repositorio no redefine claves nativas, tablas ni rutas Workspace. La [incompatibilidad pendiente de notebooks 2.0.0](getting-started.md#current-implementation-limits) sigue siendo un requisito de aceptación propio.

Governance y Market mantienen una única implementación, sin selectores de runtime en API, entorno ni Terraform. Su contrato AIDP está en `aidp/manifest.json`; el paquete conserva versión y hashes en `package/lab.json`. Los recursos y archivos se vinculan por identidad exacta. La aceptación TEST y la publicación de producción son gates separados. Las llamadas nativas `/20240831/dataLakes` utilizadas para agentes/sesiones siguen siendo una dependencia actual. Fuentes: [contrato](../apps/backend/app/packages/aidp.py), [Governance](modules/ai-data-governance.md), [Market](modules/ai-market-intelligence.md).

## Configuración e infraestructura

Se utilizan exclusivamente las variables canónicas `GODS_EYE_VIEW_*` para el visor. `GODS_EYE_CONTROL_BUCKET` identifica explícitamente el bucket de controles Object y no se infiere de Landing. `AidpRuntime` es la credencial OCI compartida; la selección verifica identidad, unicidad, tipo y estado, sin fallback a nombres retirados ni credenciales con base de datos. Fuentes: [configuración](../apps/backend/app/config.py), [credencial del runtime](../apps/backend/app/modules/ai_gods_eye_view/runtime/runtime_secrets.py).

Una base preparada registra la fuente inmutable y el stack Resource Manager original. `enabled_vm_modules` admite únicamente `gods_eye_view`. El instalador valida la lista de recursos permitidos, variables y recibo antes de actuar. No hay adopción de stacks, jobs o agentes históricos ni cadenas Terraform de renombrado para estados anteriores. Un entorno sin los contratos requeridos se rechaza; no se completa con una VM no rastreada. Ver [operación](operations.md#global-module-lifecycle).

## Releases e imágenes

Las releases estables inmutables contienen estos cuatro assets:

| Componente | Imagen comprimida | Manifiesto |
| --- | --- | --- |
| Aplicación | `aidp-lab-image-amd64.tar.gz` | `aidp-release.json` |
| Visor | `gods-eye-view-image-amd64.tar.gz` | `gods-eye-view-release.json` |

Las fechas de paquetes se obtienen del manifiesto único con ID y hash exactos en el commit de la imagen, incluso si cambió su carpeta en el repositorio. El historial Git acredita procedencia; no habilita interfaces de clientes retirados. Un paquete modificado sin commit verificable queda sin fecha.

Los manifiestos usan `schema_version=1`, `updater_protocol=1`, repositorio confiable, commit exacto, SHA-256 y plataforma `linux/amd64`. El loader exige el par canónico completo; no descarga otro nombre de componente si falta. La publicación verifica el conjunto exacto de assets y las imágenes. Las releases históricas no se reescriben para convertirlas al formato actual. Fuentes: [release](../.github/workflows/release.yml), [loader](../scripts/release/load_release_image.py).

Los despliegues nuevos del visor utilizan `/opt/gods-eye-view`, contenedores `gods-eye-view`, `gods-eye-view-candidate` y `gods-eye-view-previous`, comando `gods-eye-view-release-update` y lock `/run/gods-eye-view-release-update.lock`. El updater de la aplicación usa `releases/current.json`; no busca el recibo de una distribución anterior. Recuperar el candidato de la misma instalación y conservar la imagen saludable anterior son medidas operativas actuales, no soporte de clientes retirados. Ver [updater del visor](../scripts/release/gods_eye_view_release_update.py) y [aplicación](../scripts/release/vm_release_updater.py).

## Datos y evidencia

Las vistas `oci_gold.territorial_incidents`, `territorial_evidence`, `territorial_sensors` y `territorial_event_posts`, los prefijos `04_gold/prisma/*` y el descriptor `.control/prisma/agent.json` son nombres primarios actualmente usados por los productores/consumidores del dataset. No ofrecen interfaces alternas para clientes antiguos. Se conserva su identidad; retirar aliases no autoriza borrar, duplicar o reinterpretar datos.

Los controles actuales del visor viven en `.control/gods_eye_view/` mediante Object Storage y un diario inmutable; SQLite es una proyección reconstruible. No se admite un runtime de controles Oracle ni se incluyen migradores para esos controles. Autonomous conserva otras responsabilidades de la plataforma y la memoria de agentes, independientes del visor. Fuentes: [controles](../apps/backend/app/modules/ai_gods_eye_view/runtime/control_store.py), [índice](../apps/backend/app/modules/ai_gods_eye_view/runtime/post_index.py), [Gold](../apps/backend/app/modules/ai_gods_eye_view/runtime/gold_reader.py).

Los recibos de evaluación vinculan factory, adaptador, corpus y candidato por hash. Cambiar esas fuentes invalida la coincidencia: revisar y repetir la evaluación pertinente, sin reemplazar hashes para aparentar aceptación. Un fixture, setup TEST o estado ACTIVE no acredita investigación, inferencia ni aceptación nativa. Ver [validación y procedencia](development.md#repository-and-aidp-resources).
