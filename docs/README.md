# Documentación de Starter Kits

El portal administra cinco kits de participantes. Su [catálogo](../apps/backend/app/packages/catalog.json) y [backend](../apps/backend/app/main.py) son la fuente del comportamiento implementado.

- [Primeros pasos](getting-started.md): instalación y ejecución.
- [Operación](operations.md): usuarios, reintentos y actualización.
- [Desarrollo](development.md): estructura y comprobaciones.
- [Contratos](contracts.md): paquetes, permisos e imágenes.

## Kits

[Banking](kits/banking.md) · [Telecommunications](kits/telecommunications.md) · [Telco Customer 360 Lineage](kits/telco-lineage.md) · [Retail](kits/retail.md) · [Healthcare](kits/healthcare.md)

## Módulos independientes

| Repositorio | Alcance |
| --- | --- |
| [aidp-ai-data-gobernance](https://github.com/jgangini/aidp-ai-data-gobernance) | Agentes, tablas y workflow de gobierno. |
| [aidp-ai_market-intelligence](https://github.com/jgangini/aidp-ai_market-intelligence) | Agente Market e integración Apify. |
| [aidp-ai-gods-eye-view](https://github.com/jgangini/aidp-ai-gods-eye-view) | Visor, streaming, configuración integrada y agente. |

Governance y Market se instalan desde Deploy Studio con el administrador seleccionado y no requieren portal ni VM administrativa. God's Eye View ofrece el visor y su configuración en su propia VM. Cada repositorio documenta sus requisitos, instalación, operación y límites.

Credenciales, recibos privados y logs temporales quedan fuera de estas guías.
