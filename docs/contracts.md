# Contratos vigentes

[Documentación](README.md) · [Operación](operations.md) · [Desarrollo](development.md)

## Paquetes

El [catálogo](../apps/backend/app/packages/catalog.json) contiene exclusivamente banking, telecommunications, telco_lineage, retail y healthcare. El [loader](../apps/backend/app/packages/catalog.py) resuelve rutas explícitas bajo app/kits y verifica hashes, versiones, ámbito y dependencias. No descubre módulos externos.

Los nombres lab.json, lab_id y labs siguen siendo campos del contrato actual de kits. Cambiar carpetas no cambia identidades nativas. Consulte el [límite pendiente de notebooks](getting-started.md#current-implementation-limits).

## Identidad

El [backend](../apps/backend/app/main.py) autentica al administrador y controla registro, asignación y reconciliación. Las sesiones siguen [security.py](../apps/backend/app/security.py). Las rutas de módulos y visor no se montan.

Starter Kits ofrece únicamente laboratorios: el formulario, Terraform y la API no admiten `deployment_mode`. El código del instructor es obligatorio en Deploy Studio y Terraform recibe `registration_code_hash` en formato PBKDF2. El registro público verifica el código, los límites de frecuencia y la disponibilidad de los servicios; las acciones administrativas siguen requiriendo sesión. El tipo de instalación no altera HTTPS, cookies seguras ni aislamiento de recursos.

El operador OCI es distinto de los participantes. El bootstrap entrega su credencial mediante un sobre cifrado de un solo uso, verifica identidad y huella y elimina el objeto después de confirmarlo. El sobre de credenciales usa el contrato estricto 3 y solo transporta la identidad OCI. El aislamiento de participantes reside en Master Catalog, carpetas y permisos de Workbench; Kits no crea memoria de agentes ni Autonomous.

Se rechazan usuarios, recursos y manifiestos ajenos. No hay aliases ni adopción automática de contratos retirados.

## Infraestructura

[Deploy Studio](../terraform/deploy-studio.json) usa una release fijada a un commit. Terraform crea AIDP o referencia una plataforma existente, prepara buckets y la VM del portal. El [hook](../terraform/hooks/post_apply.py) reconcilia workspace, catálogo, permisos y bootstrap.

No se importa estado ni se adoptan recursos de otras pilas. Aplicar este paquete sobre una pila combinada podría retirar recursos: no es un procedimiento de migración.

## Releases

El perfil kits-only publica dos assets: aidp-lab-image-amd64.tar.gz y aidp-release.json. El manifiesto exige schema_version=1, updater_protocol=2, deployment_profile=kits-only, repositorio confiable, commit exacto, SHA-256 y Linux AMD64.

El [loader](../scripts/release/load_release_image.py), el [updater](../scripts/release/vm_release_updater.py) y el [workflow](../.github/workflows/release.yml) verifican esos contratos. La instalación actual también debe declarar kits-only. Una VM combinada no se transforma mediante una actualización automática ni cambiando manualmente su perfil. Las releases históricas no se reescriben.

Las fechas de kits proceden del historial Git y hashes del manifiesto; no se sustituyen por la hora de compilación.

## Evidencia

Las pruebas locales no demuestran ejecución en AIDP. La aceptación nativa requiere evidencia del workflow y datos del participante correcto, conforme a los [límites de la implementación](getting-started.md#current-implementation-limits).

## Buckets de Kits

El formulario selecciona cuatro buckets Medallion distintos: Landing, Bronze, Silver y Gold. Cada capa permite crear o reutilizar un bucket del compartimento seleccionado. Landing también recibe el sobre cifrado de credenciales de un solo uso. Kits no configura ni crea un bucket de artefactos de Governance. El bucket técnico `aidp-data-<sufijo>` y sus permisos de consola se conservan; no es una quinta selección del formulario.

Los buckets de instalaciones previas permanecen en su stack. Un Terraform reducido no debe aplicarse sobre esos estados para eliminarlos; el cambio de configuración no constituye una autorización de borrado ni un procedimiento de migración.

## Plataforma existente

`aidp_mode=existing` consulta la plataforma mediante un data source: no la importa al estado y no la elimina al destruir el stack. Debe ser ACTIVE, STRUCTURED y pertenecer a la región y al compartimento seleccionados. Deploy Studio lista sus workspaces accesibles. Debe seleccionarse un workspace ACTIVE: la comprobación previa verifica la clave dentro de la plataforma usando la identidad del operador, resuelve su nombre y entrega ambos al portal. El hook vuelve a comprobar la selección antes de escribir contenido. Un AIDP nuevo conserva su workspace predeterminado. El operador necesita administración efectiva de AIDP; el hook la verifica antes de crear recursos.

Los nombres de Kits (`oci_medallion`, `aidp_cluster_shared_compute`, roles y `/Workspace/medallon`) no se adoptan de otro producto ni de un stack combinado. Antes de escribir, el hook rechaza colisiones sin la marca de propiedad del despliegue. Los recursos parciales de la misma operación conservan su marca y se reanudan. La autorización de usar una plataforma compartida no permite reasignar sus roles o datos existentes.

El gate fresh-only sigue rechazando planes que actualicen o destruyan recursos existentes. Cambiar el selector de una instalación ya aplicada no es un mecanismo de migración. La plataforma y bases de datos de instalaciones anteriores se conservan hasta una transición expresamente autorizada.

La configuración mantiene compatibilidad con Terraform 1.5.7: las validaciones de variables solo inspeccionan su propia variable. La obligatoriedad del OCID en modo existente se comprueba mediante una precondición del data source, antes de consultarlo.
