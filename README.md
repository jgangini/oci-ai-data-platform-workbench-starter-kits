# Oracle AI Data Platform Workbench Starter Kits

Cinco laboratorios de ingeniería de datos y tres módulos compartidos para Oracle AI Data Platform (AIDP) Workbench. El proyecto incluye portal React, API FastAPI, Terraform, datasets/notebooks versionados y workflows/agentes nativos. Los módulos candidatos señalan su aceptación pendiente.

## Empezar

- [Primeros pasos](docs/getting-started.md): preparar el entorno y ejecutar kits.
- [Operación y recuperación](docs/operations.md): usuarios, instalación y actualizaciones.
- [Desarrollo local y validación](docs/development.md): modificar el repositorio.
- [Índice de documentación](docs/README.md): guías vigentes y distribución del código.

Para desplegar, utilizar una [release inmutable validada](https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits/releases). Una rama de desarrollo no es una release; revisar los [límites actuales](docs/getting-started.md#current-implementation-limits).

## Kits de participantes

Cada participante recibe su workflow y nombres de tablas propios dentro del catálogo y esquemas compartidos, con los mismos assets versionados. Las rutas de notebooks mantienen un [límite nativo pendiente](docs/getting-started.md#current-implementation-limits).

| Kit | Resultado |
| --- | --- |
| [Banking](docs/kits/banking.md) | Analítica de clientes, sucursales y transacciones. |
| [Telecommunications](docs/kits/telecommunications.md) | Consumo de suscriptores y sitios de red. |
| [Telco Customer 360 Lineage](docs/kits/telco-lineage.md) | Pipeline de catorce tareas para prepago, pospago y hogar. |
| [Retail](docs/kits/retail.md) | Valor de clientes y ventas por producto. |
| [Healthcare](docs/kits/healthcare.md) | Utilización por paciente y actividad de proveedores. |

## Módulos compartidos

Se administran en **Settings → Application**, independientemente de asignar kits. La base prepara portal HTTPS y red compartida; God’s Eye View incorpora su VM privada mediante el mismo stack Resource Manager, previa confirmación. Ver [instalación y recuperación](docs/operations.md#global-module-lifecycle).

| Módulo | Alcance |
| --- | --- |
| [AI Data Governance](docs/modules/ai-data-governance.md) | Dos agentes, dieciocho tablas y workflow documental; aceptación funcional en TEST pendiente. |
| [AI Market Intelligence](docs/modules/ai-market-intelligence.md) | Agente de investigación e instalación TEST; aceptación nativa y de calidad pendiente. |
| [God’s Eye View](docs/modules/ai-gods-eye-view.md) | Reportes sociales/sensores en mapa, evidencia y consultas Gold. |

## Código fuente

| Directorio | Contenido |
| --- | --- |
| [`apps/backend`](apps/backend) | API, identidad, aprovisionamiento y servicios compartidos. |
| [`app/kits`](apps/backend/app/kits) | Los cinco kits de participantes y sus assets versionados. |
| [`app/modules`](apps/backend/app/modules) | Tres módulos del backend, cada uno con su paquete AIDP en `package/`. |
| [`app/packages/catalog.py`](apps/backend/app/packages/catalog.py) | Registro explícito y validación común de paquetes. |
| [`apps/frontend`](apps/frontend) | Portal de registro y administración. |
| [`modules/ai-gods-eye-view`](modules/ai-gods-eye-view) | Aplicación del visor, build/imagen independientes y contrato upstream. |
| [`terraform`](terraform) | Infraestructura, manifiesto Deploy Studio y hooks. |
| [`docker`](docker) / [`scripts`](scripts) | Entornos, imágenes, releases y checks. |
| [`docs`](docs/README.md) | Guías de uso, desarrollo, operación y contratos vigentes. |

Las instrucciones para contributors están en [AGENTS.md](AGENTS.md).

## Licencia

[MIT](LICENSE). Los componentes upstream y datasets conservan sus licencias; consultar la [integración del visor](modules/ai-gods-eye-view/README.md). Proyecto independiente, no producto oficial de Oracle. Las marcas pertenecen a sus titulares.
