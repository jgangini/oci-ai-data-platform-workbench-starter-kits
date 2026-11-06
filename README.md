# Oracle AI Data Platform Workbench Starter Kits

Portal React y API FastAPI para registrar participantes y aprovisionar cinco kits de laboratorio en Oracle AI Data Platform Workbench. El registro requiere el código del instructor; el administrador también puede crear participantes desde Users.

| Kit | Resultado |
| --- | --- |
| [Banking](docs/kits/banking.md) | Clientes, sucursales y transacciones. |
| [Telecommunications](docs/kits/telecommunications.md) | Consumo de suscriptores y sitios de red. |
| [Telco Customer 360 Lineage](docs/kits/telco-lineage.md) | Catorce tareas para prepago, pospago y hogar. |
| [Retail](docs/kits/retail.md) | Clientes, productos y ventas. |
| [Healthcare](docs/kits/healthcare.md) | Pacientes, proveedores y actividad asistencial. |

[Primeros pasos](docs/getting-started.md) · [Operación](docs/operations.md) · [Desarrollo](docs/development.md) · [Contratos](docs/contracts.md)

El catálogo y sus hashes están en [packages](apps/backend/app/packages); datasets y notebooks en [kits](apps/backend/app/kits). [Terraform](terraform) y sus hooks preparan la infraestructura. [Docker](docker) y [scripts](scripts) mantienen entornos, imágenes y validación.

Governance, Market y God's Eye View se extraen a repositorios independientes. El [plan de separación](docs/module-repository-separation.md) registra su estado y pruebas pendientes. La separación de código no migra recursos existentes ni autoriza aplicar este Terraform sobre una pila combinada.

Revise los [límites nativos](docs/getting-started.md#current-implementation-limits) antes de considerar aceptado un kit. Publique solo releases inmutables verificadas.

[MIT](LICENSE). Proyecto independiente, no producto oficial de Oracle. Instrucciones: [AGENTS.md](AGENTS.md).
