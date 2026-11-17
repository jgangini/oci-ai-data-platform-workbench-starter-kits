# Telecomunicaciones · Telecommunications

[Documentación](../README.md) · [Primeros pasos](../getting-started.md)

El [paquete `2.0.0`](../../apps/backend/app/kits/telecommunications/lab.json) relaciona consumo, planes y sitios de red para producir métricas mensuales y diarias. Usa CSV deterministas de entrenamiento; no se conecta a facturación ni a gestión de red reales. Antes de ejecutarlo, revise la [incompatibilidad actual entre notebooks y aprovisionador](../getting-started.md#current-implementation-limits).

## Entradas y flujo

El aprovisionamiento copia los [archivos fuente](../../apps/backend/app/kits/telecommunications/source) a `source/` del participante. El manifiesto fija sus filas y hashes.

| CSV | Filas | Contenido |
| --- | ---: | --- |
| `plans.csv` | 12 | Tipo, mensualidad, consumo incluido y tarifa adicional |
| `network_sites.csv` | 30 | Región, tecnología y capacidad diaria |
| `subscribers.csv` | 250 | Plan, sitio habitual, segmento y activación |
| `usage_events.csv` | 6,000 | Consumo de datos, voz y SMS, sitio y cargo |

La suma original de `usage_events.charge_amount` es **15,026.56**. Reconcilia la entrada, no la salida Gold filtrada.

El trabajo `wf_<participant_key>_telecommunications` ejecuta `01_landing_telecommunications` → `02_bronze_telecommunications` → `03_silver_telecommunications` → `04_gold_telecommunications` → `05_lineage_telecommunications`. Los [notebooks](../../apps/backend/app/kits/telecommunications/notebooks) usan tablas Delta administradas en las cuatro capas. Landing carga los CSV e identidad; Bronze conserva valores; Silver convierte tipos, normaliza, elimina duplicados y valida relaciones; Gold agrega el consumo aceptado con abonados o sitios. Los rechazos quedan en `quality_issues`.

## Resultados y aceptación

Los nombres físicos añaden `<participant_key>_` a estas tablas de `<catalog_name>.oci_gold`.

| Tabla | Agrupación | Métricas |
| --- | --- | --- |
| `telecommunications_subscriber_monthly` | Abonado, plan y mes | MB, minutos, SMS, cargos e indicador de exceso |
| `telecommunications_site_daily` | Sitio y fecha | Abonados distintos, MB, minutos y utilización porcentual |

`overage_flag` indica que el `usage_charge` agregado supera cero; no recalcula una factura con las franquicias del plan. La utilización divide MB entre capacidad diaria del sitio, multiplica por 100 y redondea.

- Landing y Bronze deben reconciliar las cuatro entradas. Silver valida campos obligatorios, tipos, enumeraciones, claves e identidad del participante.
- Capacidad y valores de consumo deben ser positivos; referencias inválidas y claves duplicadas se envían a cuarentena.
- `<participant_key>_telecommunications_quality_issues`, en `oci_silver`, y ambas salidas Gold deben contener datos.
- El [notebook final](../../apps/backend/app/kits/telecommunications/notebooks/05_lineage_telecommunications.ipynb) comprueba 15 tablas Delta administradas y la configuración de linaje.
- Revise el grafo nativo de ambas anclas Gold a nivel de entidad y columna, en ambas direcciones, con profundidad 8. Siga la identidad del abonado hasta el resumen mensual y `usage_events.usage_value` → `telecommunications_site_daily.data_mb`. El éxito del notebook no demuestra por sí solo la cobertura del grafo.

## Ejecución del participante

Siga [Primeros pasos](../getting-started.md), abra el flujo asignado y conserve sus parámetros. Ejecute las cinco tareas, revise las causas de cuarentena antes de comparar métricas y repita con entradas idénticas para comprobar conteos estables bajo sobrescritura. Explore con su `catalog_name` y prefijo; por ejemplo, compare la utilización por sitio y fecha. El [notebook Gold](../../apps/backend/app/kits/telecommunications/notebooks/04_gold_telecommunications.ipynb) define las métricas.
