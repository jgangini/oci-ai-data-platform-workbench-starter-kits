# Salud · Healthcare

[Documentación](../README.md) · [Primeros pasos](../getting-started.md)

El [paquete `2.0.0`](../../apps/backend/app/kits/healthcare/lab.json) produce resúmenes de utilización por paciente y actividad diaria por profesional. Los CSV son datos deterministas de entrenamiento, no registros clínicos reales. Antes de ejecutarlo, revise la [incompatibilidad actual entre notebooks y aprovisionador](../getting-started.md#current-implementation-limits).

## Entradas y flujo

El aprovisionamiento copia los [archivos fuente](../../apps/backend/app/kits/healthcare/source) a `source/` del participante. El manifiesto fija sus hashes y filas.

| CSV | Filas | Contenido |
| --- | ---: | --- |
| `patients.csv` | 240 | Edad agrupada, región, cobertura y riesgo |
| `providers.csv` | 48 | Especialidad, región, centro y capacidad |
| `appointments.csv` | 900 | Reserva, horario, paciente, profesional y estado |
| `encounters.csv` | 700 | Atención efectiva, horarios, diagnóstico/procedimiento y coste |

La suma original de `encounters.cost_amount` es **557,597.00**. Reconcilia la entrada, no un total Gold posterior a la eliminación de registros inválidos.

El trabajo `wf_<participant_key>_healthcare` ejecuta `01_landing_healthcare` → `02_bronze_healthcare` → `03_silver_healthcare` → `04_gold_healthcare` → `05_lineage_healthcare`. Los [notebooks](../../apps/backend/app/kits/healthcare/notebooks) usan tablas Delta administradas en las cuatro capas. Landing añade identidad; Bronze conserva valores; Silver normaliza tipos, ordena duplicados, valida referencias y horarios y registra rechazos con su causa; Gold agrega las citas y atenciones aceptadas.

## Resultados y aceptación

Los nombres físicos añaden `<participant_key>_` a estas tablas de `<catalog_name>.oci_gold`.

| Tabla | Agrupación | Métricas |
| --- | --- | --- |
| `healthcare_patient_utilization` | Paciente | Citas, ausencias, atenciones, coste total y última atención |
| `healthcare_provider_daily` | Profesional y fecha | Citas programadas/completadas, ausencias, atenciones, duración promedio y coste |

La salida de pacientes parte de los pacientes aceptados y une su actividad con `left join`. La de profesionales parte de las fechas de citas y une las atenciones realizadas. La duración se calcula en minutos entre inicio y fin aceptados.

- Landing y Bronze deben reconciliar las entradas. Silver valida campos obligatorios, conversiones, enumeraciones, identidad y referencias.
- Costes de atención y capacidad diaria deben ser positivos. El fin de cada cita o atención debe ser estrictamente posterior al inicio.
- `<participant_key>_healthcare_quality_issues`, en `oci_silver`, y ambas salidas Gold deben contener datos.
- El [notebook final](../../apps/backend/app/kits/healthcare/notebooks/05_lineage_healthcare.ipynb) comprueba las 15 tablas Delta administradas y la configuración de linaje.
- En Master Catalog, revise entidad y columna en ambas direcciones con profundidad 8. Siga identidad del paciente → `healthcare_patient_utilization` y coste de atención → `healthcare_provider_daily.total_cost` a través de Landing, Bronze y Silver. El éxito del notebook no acredita todas las aristas del grafo nativo.

## Ejecución del participante

Siga [Primeros pasos](../getting-started.md), abra el flujo asignado con sus parámetros y ejecute las cinco tareas. Revise rechazos de horarios y referencias en `quality_issues`, ambas salidas Gold y su linaje. Repita con entradas idénticas para comprobar conteos estables bajo sobrescritura. Conserve su `catalog_name` y prefijo. Compare ausencias y duración de atención por profesional y día: representan fases diferentes, definidas por el [notebook Gold](../../apps/backend/app/kits/healthcare/notebooks/04_gold_healthcare.ipynb).
