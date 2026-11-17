# Banca · Banking

[Documentación](../README.md) · [Primeros pasos](../getting-started.md)

El [paquete `2.0.0`](../../apps/backend/app/kits/banking/lab.json) construye resúmenes de clientes y sucursales con CSV de entrenamiento deterministas. No consume operaciones de un banco real. Antes de ejecutarlo, revise la [incompatibilidad actual entre notebooks y aprovisionador](../getting-started.md#current-implementation-limits).

## Entradas y flujo

El aprovisionamiento copia los [archivos fuente](../../apps/backend/app/kits/banking/source) a `source/` del participante. El manifiesto fija sus filas y SHA-256.

| CSV | Filas | Contenido |
| --- | ---: | --- |
| `branches.csv` | 20 | Tipo de sucursal, región y apertura |
| `customers.csv` | 200 | Segmento, región y riesgo |
| `accounts.csv` | 320 | Titularidad, sucursal, saldo y tipo |
| `transactions.csv` | 4,000 | Cuenta, fecha, canal, moneda e importe |

La suma original de `transactions.amount` es **600,066.50**. Reconcilia la entrada; no es el total Gold después de la cuarentena.

El trabajo `wf_<participant_key>_banking` ejecuta, en orden, `01_landing_banking` → `02_bronze_banking` → `03_silver_banking` → `04_gold_banking` → `05_lineage_banking`. Los [notebooks](../../apps/backend/app/kits/banking/notebooks) escriben tablas Delta administradas en las cuatro capas. Landing añade la identidad del participante; Bronze conserva los registros; Silver normaliza tipos, conserva la versión más reciente por clave de negocio, valida relaciones y separa rechazos; Gold agrega los registros aceptados.

## Resultados y aceptación

Los nombres físicos añaden `<participant_key>_` a estas tablas de `<catalog_name>.oci_gold`.

| Tabla | Agrupación | Métricas |
| --- | --- | --- |
| `banking_customer_value` | Cliente | Cuentas, saldo, transacciones recientes, débitos/créditos y última transacción |
| `banking_branch_daily` | Sucursal y fecha | Cuentas activas, transacciones, importe total y promedio |

La ventana de cliente comprende 30 días respecto de la última transacción aceptada; no depende del reloj del equipo. Los créditos incluyen `credit` y `refund`. El resumen de sucursal usa todas las transacciones aceptadas.

- Landing y Bronze deben reconciliar las filas de entrada. Silver valida campos obligatorios, conversiones, enumeraciones, identidad y claves foráneas; sus filas no pueden superar las de Bronze.
- Los importes deben ser positivos. Los duplicados se ordenan por actualización antes de enviar los descartados a cuarentena.
- `<participant_key>_banking_quality_issues`, en `oci_silver`, debe contener al menos una fila; ambas salidas Gold deben contener datos.
- El [notebook final](../../apps/backend/app/kits/banking/notebooks/05_lineage_banking.ipynb) comprueba las 15 tablas declaradas, su contenido y el formato Delta administrado.
- En Master Catalog, revise ambas anclas Gold a nivel de entidad y columna, en ambas direcciones y con profundidad 8. Incluya `transactions.amount` → `banking_branch_daily.transaction_amount` a través de Landing, Bronze y Silver. La comprobación de configuración del notebook no acredita por sí sola todas las aristas del grafo nativo.

## Ejecución del participante

Siga [Primeros pasos](../getting-started.md), conserve los parámetros suministrados de participante, espacio de trabajo y catálogo, y ejecute las cinco tareas. Revise el primer fallo y `quality_issues` antes de aceptar agregados. Compare las salidas y su linaje nativo; repita con entradas idénticas para comprobar que la sobrescritura mantiene los conteos. Use su `catalog_name` y su prefijo de tablas, nunca los de otro participante. El [notebook Gold](../../apps/backend/app/kits/banking/notebooks/04_gold_banking.ipynb) define las métricas ejecutables.
