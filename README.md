# Oracle AI Data Platform Workbench Starter Kits

Portal para organizar laboratorios de datos en Oracle AI Data Platform Workbench. El instructor configura el entorno y asigna kits; cada participante recibe identidad, carpetas, notebooks, workflows y permisos sobre sus propios recursos.

El portal funciona en modo **Laboratory**. El registro requiere el código del instructor; el administrador también puede crear participantes desde **Users**.

## Kits disponibles

| Kit | Resultado |
| --- | --- |
| [Banking](docs/kits/banking.md) | Clientes, sucursales y transacciones. |
| [Telecommunications](docs/kits/telecommunications.md) | Consumo de suscriptores y sitios de red. |
| [Telco Customer 360 Lineage](docs/kits/telco-lineage.md) | Catorce tareas para prepago, pospago y hogar. |
| [Retail](docs/kits/retail.md) | Clientes, productos y ventas. |
| [Healthcare](docs/kits/healthcare.md) | Pacientes, proveedores y actividad asistencial. |

## Funcionamiento

1. Deploy Studio prepara el compartimento, la plataforma AIDP, los buckets y la VM del portal.
2. El administrador accede al portal, configura el código del laboratorio y asigna los kits.
3. El registro reconcilia la identidad OCI del participante y aprovisiona contenido, workflows y permisos en Workbench.
4. El participante accede a Workbench para ejecutar sus notebooks y consultar las tablas y el linaje en Master Catalog.

Las canalizaciones usan Spark y cuatro capas Medallion: Landing, Bronze, Silver y Gold. Un quinto bucket almacena artefactos. Los nombres de tablas y workflows incorporan la identidad del participante y del kit.

## Despliegue

Seleccione una [release inmutable](https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits/releases) en Deploy Studio y cargue allí el perfil OCI y su clave API. El operador necesita permisos sobre los recursos del [paquete Terraform](terraform).

En **AI Data Platform Workbench → Services mode** puede crear una plataforma con nombre inicial `aidp_medallion` o elegir una existente. La plataforma reutilizada debe estar ACTIVE/STRUCTURED en la región y compartimento seleccionados. Se conserva fuera del ciclo de vida del stack y se rechazan colisiones con recursos ajenos.

El formulario configura cinco buckets distintos, el administrador del portal y el código de registro. Kits utiliza Workbench, Spark y Master Catalog; no requiere Autonomous ni un modelo de Agent Flow. La VM expone el portal y su API por HTTPS.

La guía de [primeros pasos](docs/getting-started.md) detalla requisitos, entradas, salidas y acceso. Revise sus [límites vigentes](docs/getting-started.md#current-implementation-limits): existe una discrepancia entre las rutas aprovisionadas y las validaciones de los notebooks que debe resolverse antes de aceptar las ejecuciones de participantes.

## Administración y actualización

**Users** administra participantes y kits; **Settings** mantiene la configuración de Workbench, zona horaria, código de registro y versiones. Las operaciones conservan estado para reintentar; actualizar el portal no reprovisiona automáticamente los kits asignados.

El actualizador verifica la release, su origen y sus hashes, comprueba la salud del candidato y conserva la imagen anterior para recuperación. Esta distribución requiere el perfil `kits-only`; no convierte instalaciones combinadas ni permite aplicar su Terraform sobre sus stacks. Consulte [operación](docs/operations.md) y [contratos](docs/contracts.md).

## Desarrollo y documentación

El catálogo y sus hashes están en [packages](apps/backend/app/packages); datasets y notebooks en [kits](apps/backend/app/kits). [Terraform](terraform) y sus hooks preparan la infraestructura. [Docker](docker) y [scripts](scripts) mantienen entornos, imágenes y validación.

La aplicación usa React y FastAPI. [Desarrollo](docs/development.md) describe Docker local, estructura y verificaciones; el [índice de documentación](docs/README.md) enlaza las guías de cada kit.

## Productos relacionados

Estos productos tienen instalación y operación propias; no son dependencias del portal de kits:

- [AIDP AI Data Governance](https://github.com/jgangini/aidp-ai-data-gobernance): agentes y workflow de gobierno de datos.
- [AIDP AI Market Intelligence](https://github.com/jgangini/aidp-ai_market-intelligence): investigación de mercado con Apify y agentes AIDP.
- [AIDP AI God’s Eye View](https://github.com/jgangini/aidp-ai-gods-eye-view): visor geoespacial, streaming y agente en su propia VM.

[MIT](LICENSE). Proyecto independiente, no producto oficial de Oracle. Instrucciones: [AGENTS.md](AGENTS.md).
