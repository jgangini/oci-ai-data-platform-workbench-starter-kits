# Contratos vigentes

[Documentación](README.md) · [Operación](operations.md) · [Desarrollo](development.md)

## Paquetes

El [catálogo](../apps/backend/app/packages/catalog.json) contiene exclusivamente banking, telecommunications, telco_lineage, retail y healthcare. El [loader](../apps/backend/app/packages/catalog.py) resuelve rutas explícitas bajo app/kits y verifica hashes, versiones, ámbito y dependencias. No descubre módulos externos.

Los nombres lab.json, lab_id y labs siguen siendo campos del contrato actual de kits. Cambiar carpetas no cambia identidades nativas. Consulte el [límite pendiente de notebooks](getting-started.md#current-implementation-limits).

## Identidad

El [backend](../apps/backend/app/main.py) autentica al administrador y controla registro, asignación y reconciliación. Las sesiones siguen [security.py](../apps/backend/app/security.py). Las rutas de módulos y visor no se montan.

Starter Kits ofrece únicamente laboratorios: el formulario, Terraform y la API no admiten `deployment_mode`. El código del instructor es obligatorio en Deploy Studio y Terraform recibe `registration_code_hash` en formato PBKDF2. El registro público verifica el código, los límites de frecuencia y la disponibilidad de los servicios; las acciones administrativas siguen requiriendo sesión. El tipo de instalación no altera HTTPS, cookies seguras ni aislamiento de recursos.

El operador OCI es distinto de los participantes. El bootstrap entrega su credencial mediante un sobre cifrado de un solo uso, verifica identidad y huella y elimina el objeto después de confirmarlo. El paquete SQL de Autonomous restringe operaciones sobre esquemas de participantes; su nombre interno no significa que AI Data Governance esté instalado.

Se rechazan usuarios, recursos y manifiestos ajenos. No hay aliases ni adopción automática de contratos retirados.

## Infraestructura

[Deploy Studio](../terraform/deploy-studio.json) usa una release fijada a un commit. Terraform prepara AIDP, Autonomous nueva o existente, buckets y la VM del portal. El [hook](../terraform/hooks/post_apply.py) reconcilia workspace, catálogo, permisos y bootstrap.

No se importa estado ni se adoptan recursos de otras pilas. Aplicar este paquete sobre una pila combinada podría retirar recursos: no es un procedimiento de migración.

## Releases

El perfil kits-only publica dos assets: aidp-lab-image-amd64.tar.gz y aidp-release.json. El manifiesto exige schema_version=1, updater_protocol=2, deployment_profile=kits-only, repositorio confiable, commit exacto, SHA-256 y Linux AMD64.

El [loader](../scripts/release/load_release_image.py), el [updater](../scripts/release/vm_release_updater.py) y el [workflow](../.github/workflows/release.yml) verifican esos contratos. La instalación actual también debe declarar kits-only. Una VM combinada no se transforma mediante una actualización automática ni cambiando manualmente su perfil. Las releases históricas no se reescriben.

Las fechas de kits proceden del historial Git y hashes del manifiesto; no se sustituyen por la hora de compilación.

## Evidencia

Las pruebas locales no demuestran ejecución en AIDP. La aceptación nativa requiere evidencia del workflow y datos del participante correcto. El [plan de separación](module-repository-separation.md) registra los gates de los módulos.
