# Documentación

[Proyecto](../README.md)

Guías del comportamiento implementado en este repositorio. El manifiesto de cada paquete determina versiones, hashes y resultados esperados; los recibos y salidas nativas determinan qué está instalado y qué ejecutó realmente cada entorno. Un test local o un despliegue ACTIVE no acredita aceptación completa en OCI.

## Por dónde empezar

| Necesidad | Guía |
| --- | --- |
| Preparar el entorno o ejecutar un kit | [Primeros pasos](getting-started.md) y [límites vigentes](getting-started.md#current-implementation-limits). |
| Administrar usuarios, módulos y actualizaciones | [Operación y recuperación](operations.md). |
| Modificar y validar el código en local | [Desarrollo](development.md). |
| Consultar formatos admitidos y rechazos | [Contratos vigentes](contracts.md). |

## Kits de participantes

Cada kit mantiene su dataset, dependencias, tablas, workflow y checks de resultado; el aprovisionamiento separa recursos por participante.

- [Banking](kits/banking.md)
- [Telecommunications](kits/telecommunications.md)
- [Telco Customer 360 Lineage](kits/telco-lineage.md)
- [Retail](kits/retail.md)
- [Healthcare](kits/healthcare.md)

<a id="shared-global-modules"></a>
## Tres módulos compartidos

| Módulo | Comportamiento y límites |
| --- | --- |
| [AI Data Governance](modules/ai-data-governance.md) | Dos agentes, dieciocho tablas y workflow documental; aceptación funcional en TEST pendiente. |
| [AI Market Intelligence](modules/ai-market-intelligence.md) | Investigación y recuperación propias con instalador TEST. El paquete permanece planned hasta aceptación nativa. |
| [God’s Eye View](modules/ai-gods-eye-view.md) | Visor privado de publicaciones sociales/sensores, evidencia, controles durables y consulta Gold. |

Instalar módulos es distinto de asignar kits. Governance y Market Intelligence se administran en **Settings → Application**; God’s Eye View también incorpora su VM privada mediante el stack original y permite conceder acceso lector a usuarios. Las guías separan comprobaciones locales, instalación y aceptación nativa.

## Distribución y flujo actuales

| Ubicación | Responsabilidad |
| --- | --- |
| [`apps/frontend`](../apps/frontend) | Portal de registro y administración React. |
| [`apps/backend`](../apps/backend) | API FastAPI, identidad, aprovisionamiento y ciclo de vida. |
| [`app/kits`](../apps/backend/app/kits) | Cinco kits de participantes: `lab.json`, datasets y notebooks versionados. |
| [`app/modules`](../apps/backend/app/modules) | Governance, Market Intelligence y God’s Eye View del backend; Governance y Market tienen fuentes/recursos en `aidp/`, con metadatos del paquete en `package/lab.json`. |
| [`app/packages/catalog.py`](../apps/backend/app/packages/catalog.py) | Registro explícito y loader común que valida identidad, ámbito, hashes y dependencias de kits/módulos. |
| [`app/services`](../apps/backend/app/services) | Capacidades OCI de AIDP, identidad, Autonomous y bootstrap de credenciales; servicio de releases. |
| [`app/api`](../apps/backend/app/api) | Acceso/identidad del visor; las rutas retiradas no se montan y devuelven 404. |
| [`modules/ai-gods-eye-view`](../modules/ai-gods-eye-view) | Aplicación visual independiente, upstream fijado, parches y su propia imagen/VM privada. |
| [`terraform`](../terraform) | Infraestructura, contrato Deploy Studio y hooks de reconciliación. |
| [`docker`](../docker) y [`scripts`](../scripts) | Entornos locales, imágenes, releases y validación. |

La VM pública autentica y da acceso a AIDP y al visor privado. El módulo Python `app/modules/ai_gods_eye_view` administra el dominio y sus fuentes AIDP; `modules/ai-gods-eye-view` contiene la aplicación visual desplegable. Son responsabilidades distintas y no se copian íntegramente a Workbench. La distribución interna y el [mapeo a recursos nativos](development.md#repository-and-aidp-resources) se explican en Desarrollo.

Spark ejecuta los workflows y agentes dedicados utilizan los servicios/modelos configurados. Landing, Bronze, Silver y Gold tienen buckets seleccionados distintos; el bucket de artefactos almacena tablas framework y recibos de instalación. Los controles operativos del visor y su diario están en Object Storage del bucket Gold; SQLite es una proyección reconstruible. La ubicación real de tablas se comprueba en Master Catalog, no se deduce del nombre de una carpeta.

Terraform y los [hooks](../terraform/hooks) reproducen el entorno; los recibos protegidos permiten reanudar operaciones. Ninguna guía da por migrados los recursos de una instalación existente al actualizar código o imagen. Para contributors, las reglas están en [AGENTS.md](../AGENTS.md) y los checks en [Desarrollo](development.md).

Mantener aquí explicaciones reutilizables. Credenciales, IDs de tenant, recibos privados, logs de ejecución y diagnósticos temporales quedan fuera de esta documentación.
