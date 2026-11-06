# Documentación de Starter Kits

El portal administra cinco kits de participantes. Su [catálogo](../apps/backend/app/packages/catalog.json) y [backend](../apps/backend/app/main.py) son la fuente del comportamiento implementado.

- [Primeros pasos](getting-started.md): instalación y ejecución.
- [Operación](operations.md): usuarios, reintentos y actualización.
- [Desarrollo](development.md): estructura y comprobaciones.
- [Contratos](contracts.md): paquetes, permisos e imágenes.
- [Separación de módulos](module-repository-separation.md): plan único y gates de migración.

## Kits

[Banking](kits/banking.md) · [Telecommunications](kits/telecommunications.md) · [Telco Customer 360 Lineage](kits/telco-lineage.md) · [Retail](kits/retail.md) · [Healthcare](kits/healthcare.md)

## Módulos independientes

| Repositorio | Alcance |
| --- | --- |
| [aidp-ai_data_governance](https://github.com/jgangini/aidp-ai_data_governance) | Agentes, tablas y workflow de gobierno. |
| [aidp-ai_market-intelligence](https://github.com/jgangini/aidp-ai_market-intelligence) | Agente Market e integración Apify. |
| [aidp-ai-gods-eye-view](https://github.com/jgangini/aidp-ai-gods-eye-view) | Visor, streaming, configuración integrada y agente. |

Governance y Market no requieren portal ni VM administrativa. Deploy Studio selecciona al administrador. God's Eye View conserva su interfaz en su propia VM. Cada repositorio mantiene documentación y pruebas propias; el plan distingue extracción, publicación y aceptación OCI.

Credenciales, recibos privados y logs temporales quedan fuera de estas guías.
