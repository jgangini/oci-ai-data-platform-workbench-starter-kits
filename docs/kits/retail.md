# Comercio minorista · Retail

[Documentación](../README.md) · [Primeros pasos](../getting-started.md)

El [paquete `2.0.0`](../../apps/backend/app/kits/retail/lab.json) transforma clientes, productos y pedidos en resúmenes de valor y actividad diaria. Usa CSV deterministas de entrenamiento con problemas controlados de calidad; no es un conector comercial real. Antes de ejecutarlo, revise la [incompatibilidad actual entre notebooks y aprovisionador](../getting-started.md#current-implementation-limits).

## Entradas y flujo

El aprovisionamiento copia los [archivos fuente](../../apps/backend/app/kits/retail/source) a `source/` del participante. El manifiesto fija sus filas y SHA-256.

| CSV | Filas | Contenido |
| --- | ---: | --- |
| `customers.csv` | 300 | Segmento, región, fidelización y alta |
| `products.csv` | 150 | Categoría, coste, precio de lista y estado |
| `orders.csv` | 1,200 | Cliente, canal, moneda, fecha y estado |
| `order_items.csv` | 3,000 | Producto, cantidad, precio y descuento |

La suma original de `order_items.quantity` es **7,487**; la cuarentena de Silver puede cambiar la cantidad representada en Gold.

El trabajo `wf_<participant_key>_retail` ejecuta `01_landing_retail` → `02_bronze_retail` → `03_silver_retail` → `04_gold_retail` → `05_lineage_retail`. Los [notebooks](../../apps/backend/app/kits/retail/notebooks) usan tablas Delta administradas en las cuatro capas. Landing añade identidad; Bronze conserva registros; Silver normaliza, ordena duplicados y valida relaciones; Gold agrega artículos aceptados junto con pedidos y costes. Los rechazos quedan en `quality_issues`.

## Resultados y aceptación

Los nombres físicos añaden `<participant_key>_` a estas tablas de `<catalog_name>.oci_gold`.

| Tabla | Agrupación | Métricas |
| --- | --- | --- |
| `retail_customer_value` | Cliente | Pedidos, unidades, ingresos brutos/netos, descuentos, pedido promedio y último pedido |
| `retail_product_daily` | Producto y fecha | Unidades, ingreso neto, margen bruto y unidades reembolsadas |

El importe bruto es `quantity × unit_price`; el neto resta el descuento del artículo. El margen resta `quantity × unit_cost` al neto. Los pedidos reembolsados contribuyen a los agregados y a `refunded_units`; el notebook no invierte automáticamente sus ingresos.

- Landing y Bronze deben reconciliar las entradas. Silver valida campos obligatorios, tipos, enumeraciones, identidad y referencias.
- Coste y precio de producto, cantidad y precio de artículo deben ser positivos. Duplicados y referencias inválidas de cliente, pedido o producto se envían a cuarentena.
- `<participant_key>_retail_quality_issues`, en `oci_silver`, debe contener al menos una fila; ambas salidas Gold deben contener datos.
- El [notebook final](../../apps/backend/app/kits/retail/notebooks/05_lineage_retail.ipynb) comprueba las 15 tablas Delta administradas y la configuración de linaje.
- En Master Catalog, revise ambas anclas Gold a nivel de entidad y columna, en ambas direcciones y con profundidad 8. Siga la identidad del cliente hasta `retail_customer_value` y la cantidad del artículo hasta `retail_product_daily.units`, con sus relaciones de productos y pedidos. La configuración de linaje no demuestra todas las aristas del grafo nativo.

## Ejecución del participante

Siga [Primeros pasos](../getting-started.md), conserve los parámetros de su flujo y ejecute las cinco tareas. Revise la cuarentena antes de aceptar los agregados, compare ambas salidas y su linaje, y repita con entradas idénticas para comprobar conteos estables bajo sobrescritura. Use su `catalog_name` y prefijo. Compare ingreso neto, margen y reembolsos por producto y día con las expresiones del [notebook Gold](../../apps/backend/app/kits/retail/notebooks/04_gold_retail.ipynb).
