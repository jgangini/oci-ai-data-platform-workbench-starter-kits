# Tutorial de Banking

[Documentación](README.md) · [Arquitectura](architecture.md) · [Pruebas](testing.md)

Sigue el kit **Banking** desde los archivos de muestra hasta el resumen diario de transacciones por sucursal. El ejercicio usa 4.000 transacciones sintéticas y permite revisar qué datos llegaron al resultado y qué registros quedaron en cuarentena. [Datos y métricas del kit](kits/banking.md)

## Antes de empezar

Necesitas un portal de laboratorio disponible, un código de registro del instructor y acceso al correo con el que activarás tu cuenta OCI. Si administras el laboratorio, usa la guía de [despliegue y acceso](getting-started.md).

Comprueba que la asignación usa Banking `2.0.1` y ejecuta el workflow con los parámetros que configuró el aprovisionador. Si el portal muestra una versión anterior, el instructor puede actualizar ese kit desde Users. [Versiones y aceptación](getting-started.md#current-implementation-limits)

## Como participante

1. Abre el portal, elige **Banking** y completa tu nombre, correo y código y pulsa **Create account**. Espera el mensaje **Your starter kit account is ready**. Si ofrece un reintento, continúa la misma operación.
2. Si OCI Identity Domains te pide activar tu cuenta, sigue el enlace de su correo de bienvenida. En el portal, selecciona **Open AI Data Platform Workbench**. En tu carpeta de Banking, localiza `source/transactions.csv`, `lab-manifest.json` y los cinco notebooks.
## Ejecuta el workflow cuando la instalación esté aceptada

1. Abre tu workflow `wf_<participant_key>_banking`. Conserva los parámetros que el portal asignó a tu participante. Ejecuta las tareas en orden y revisa el resultado de cada una.
2. En Master Catalog, abre tu tabla `<participant_key>_banking_branch_daily` en el esquema `oci_gold`. Comprueba que tiene filas y que ofrece el importe total de transacciones por cada sucursal y fecha.
3. Inspecciona el linaje de `transaction_amount` hacia `transactions.amount`. Si quieres investigar registros descartados, consulta tu tabla `<participant_key>_banking_quality_issues` en `oci_silver`.

Si el workflow falla por rutas o catálogo, registra la primera tarea que falló y sus parámetros. Conserva las validaciones del notebook y verifica la instalación con el instructor.

## Qué hace el sistema

El portal selecciona el paquete del catálogo y aprovisiona la carpeta, los archivos, los notebooks y el workflow con la identidad del participante. En Spark, las capas Landing y Bronze conservan las filas de entrada. Silver valida claves y tipos, resuelve duplicados y separa rechazos. Gold agrega los registros aceptados. El último notebook y la revisión de Master Catalog ayudan a comprobar tablas y linaje. [Paquete y notebooks de Banking](../apps/backend/app/kits/banking) · [Criterios de aceptación](testing.md)
