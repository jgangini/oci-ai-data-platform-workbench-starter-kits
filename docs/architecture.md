# Arquitectura de Starter Kits

[Documentación](README.md) · [Tutorial](tutorial.md) · [Despliegue](getting-started.md)

Deploy Studio entrega un portal HTTPS y prepara un entorno de laboratorios en OCI. Los instructores administran participantes en el portal. Los participantes ejecutan sus flujos y consultan los resultados en Workbench.

## Partes del sistema

| Componente | Responsabilidad |
| --- | --- |
| Deploy Studio y OCI Resource Manager | Validan las entradas y ejecutan el plan y Apply de la release fijada. El [manifiesto](../terraform/deploy-studio.json) define el formulario y las fases. |
| Portal de Starter Kits | React presenta el registro y las vistas administrativas. FastAPI coordina el aprovisionamiento, el seguimiento y los reintentos. [Interfaz](../apps/frontend/src/App.tsx) · [API](../apps/backend/app/main.py) |
| OCI Compute y red | Una VM sirve el portal por HTTPS desde la subred pública del stack. [Instancia](../terraform/g_oci_core_instance.tf) · [Red](../terraform/e_oci_core_vcn.tf) |
| OCI Identity Domains e IAM | El portal reconcilia la identidad del participante. Los permisos determinan qué recursos puede usar. [Grupos y políticas](../terraform/i_oci_identity.tf) · [Coordinación](../apps/backend/app/main.py) |
| Workbench, Spark y Master Catalog | Workbench contiene carpetas y workflows. Spark procesa los datos. Master Catalog registra tablas administradas y su linaje. [Hook](../terraform/hooks/post_apply.py) · [Aprovisionador](../apps/backend/app/services/oci/aidp.py) |
| OCI Object Storage | Cuatro buckets distintos respaldan Landing, Bronze, Silver y Gold. Terraform también define el almacenamiento privado de bootstrap. [Recursos y validación](../terraform/f_oci_objectstorage_bucket.tf) |

## Despliegue y propiedad

El operador puede crear AIDP o seleccionar una plataforma reutilizable y su workspace. Resource Manager despliega los recursos que administra el stack. El hook posterior prepara o valida el workspace, el catálogo, el cómputo y los permisos. La plataforma reutilizada queda fuera del ciclo de vida de Terraform y las colisiones con recursos ajenos detienen la instalación. [Plataforma y políticas](../terraform/j_oci_ai_data_platform.tf) · [Contrato](contracts.md)

## Del registro al resultado

1. El participante selecciona un kit y envía el registro con el código del instructor. El portal conserva el estado de la operación para permitir reintentos.
2. La API reconcilia la identidad y publica los archivos de muestra, notebooks, workflow y permisos en el workspace. Las rutas, nombres y permisos mantienen separados los recursos de cada kit y participante.
3. Workbench ejecuta el workflow de Spark. Landing recibe los archivos, Bronze conserva los registros, Silver valida y normaliza y Gold publica las tablas del ejercicio.
4. El participante abre sus tablas en Master Catalog y revisa el linaje. Las [pruebas de aceptación](testing.md) distinguen el aprovisionamiento de la ejecución real.

[Catálogo de kits](../apps/backend/app/packages/catalog.json) · [Convenciones de nombres](../apps/backend/app/kits/naming.py) · [Guías por kit](README.md#kits)
