<p align="center">
  <img src="docs/assets/starter-kits-cover-aidp-white.png" width="224" alt="Un castor ensambla cuatro capas de datos con el icono blanco de OCI AI Data Platform en la ficha superior">
</p>
<h3 align="center">Oracle AI Data Platform Workbench Starter Kits</h3>
<p align="center">Cinco laboratorios con datos de muestra para procesar, validar y rastrear datos en Oracle AI Data Platform Workbench.</p>
<p align="center">
  <a href="LICENSE"><img alt="Licencia MIT" src="https://img.shields.io/badge/licencia-MIT-526d54?style=flat-square"></a>
  <a href=".github/workflows/ci.yml"><img alt="Flujo de integración continua" src="https://img.shields.io/badge/GitHub-Actions-384556?style=flat-square"></a>
</p>
<p align="center"><strong>Tecnologías</strong></p>
<p align="center">
  <a href="docs/architecture.md"><img alt="Oracle AI Data Platform Workbench" src="https://img.shields.io/badge/OCI-AIDP_Workbench-ad3c2c?style=flat-square"></a>
  <a href="docs/architecture.md#partes-del-sistema"><img alt="Spark" src="https://img.shields.io/badge/procesamiento-Spark-9b4a23?style=flat-square"></a>
  <a href="docs/development.md"><img alt="FastAPI y React" src="https://img.shields.io/badge/portal-FastAPI_%2B_React-35617b?style=flat-square"></a>
</p>
<p align="center"><a href="docs/tutorial.md">Tutorial</a> · <a href="docs/getting-started.md">Despliegue</a> · <a href="docs/architecture.md">Arquitectura</a> · <a href="docs/testing.md">Pruebas</a></p>

## Cómo funciona

El instructor configura el portal y habilita el registro. Cada participante elige sus kits, activa su cuenta de OCI y recibe carpetas, notebooks, workflows y permisos propios dentro de un workspace compartido. En Workbench ejecuta un workflow de Spark y comprueba los resultados y el linaje en Master Catalog.

| Kit | Qué explora |
| --- | --- |
| [Banking](docs/kits/banking.md) | Actividad por cliente y por sucursal. |
| [Telecommunications](docs/kits/telecommunications.md) | Uso de suscriptores y sitios de red. |
| [Telco Customer 360 Lineage](docs/kits/telco-lineage.md) | Linaje de prepago, pospago y hogar en un flujo de 14 tareas. |
| [Retail](docs/kits/retail.md) | Ventas, productos y clientes. |
| [Healthcare](docs/kits/healthcare.md) | Actividad asistencial con datos de demostración. |

El [tutorial de Banking](docs/tutorial.md) recorre un caso desde el CSV de transacciones hasta el resumen diario por sucursal. Para ejecutar el ejercicio en OCI, verifica primero la compatibilidad de rutas del paquete instalado. [Límite de los notebooks 2.0.0](docs/getting-started.md#current-implementation-limits)

## Documentación

- [Primeros pasos](docs/getting-started.md) para desplegar el portal y recuperar los accesos.
- [Tutorial de uso](docs/tutorial.md) para seguir el ejercicio de un participante.
- [Arquitectura](docs/architecture.md) para entender los componentes, el despliegue y los datos.
- [Pruebas](docs/testing.md) para distinguir validación local y aceptación en Workbench.
- [Índice completo](docs/README.md) con operación, desarrollo, contratos y módulos independientes.

Proyecto independiente, sin afiliación oficial con Oracle.
