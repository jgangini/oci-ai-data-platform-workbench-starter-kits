# Pruebas y aceptación

[Documentación](README.md) · [Desarrollo](development.md) · [Tutorial](tutorial.md)

| Comprobación | Evidencia que aporta |
| --- | --- |
| Contrato de Deploy Studio | El backend reconoce el manifiesto v2, sus campos, las salidas, los hooks y las referencias del correo. |
| Pruebas Python | Los contratos de kits y despliegue, el registro, las reglas de arquitectura y el aprovisionamiento simulado respetan el comportamiento esperado. |
| Pruebas de React y compilación | Los flujos del portal pasan las pruebas del frontend y el paquete compila. |
| Terraform e imagen | Los proveedores, formatos, configuración y planes simulados pasan sus comprobaciones, y la imagen compila. |
| Ejecución en Workbench | Dos participantes reales completan sus cinco workflows, verifican sus tablas y reciben una denegación de permisos cuando consultan las del otro. |

## Verificación local

Usa las dependencias y versiones del [workflow CI](../.github/workflows/ci.yml). Consulta [desarrollo](development.md#validación) para preparar el entorno, el preflight y el postflight de arquitectura. Ejecuta las pruebas Python desde la raíz del repositorio.

```sh
python -m pip install -r apps/backend/requirements-dev.txt
python -m pytest apps/backend/tests terraform/tests
```

En `apps/frontend`, ejecuta `npm ci`, `npm test` y `npm run build`. Para Terraform usa el directorio `terraform` y las versiones declaradas en CI. El workflow ejecuta formato, inicialización sin backend, validación y las pruebas de planes simulados.

## Contrato del portal y del correo

En un entorno con las dependencias del backend de CloudTechNext, ejecuta el comando `deploy_studio.py check <ruta-starter-kits> --studio <ruta-cloudtechnext>` del plugin OCI Deploy Studio. El comando `verify-studio <ruta-cloudtechnext>` ejecuta las regresiones del portal y pruebas sintéticas del correo. Para validar el contenido del kit, usa URLs HTTPS, usuario y contraseña sintéticos y comprueba las dos secciones de acceso y los adjuntos esperados. [Correo y accesos del despliegue](getting-started.md#correo-y-accesos-del-despliegue)

## Aceptación en Workbench

1. Aprovisiona los cinco kits para dos participantes de prueba sin privilegios administrativos. Comprueba el catálogo asignado a cada participante.
2. Ejecuta cada workflow desde su cuenta. En el historial verifica la identidad efectiva de `Run As`, el resultado de todas las tareas y las tablas Gold de los cinco kits para ambos participantes. Reejecuta los workflows para comprobar la actualización de sus tablas.
3. Desde cada cuenta, consulta una tabla Gold propia y luego intenta consultar esa misma tabla en el catálogo del otro participante. Usa una tabla que exista en el otro catálogo y comprueba que OCI indique una denegación de permisos. Repite la comprobación en ambos sentidos para los cinco kits.
4. Registra release, versión y hash del kit, ejecución, identidad efectiva y resultados. Guarda los logs y recibos con datos de la tenancy fuera de Git.

La primera ejecución requiere que la identidad del job pueda crear sus tablas. Oracle aplica los permisos del esquema a los objetos que contiene. Conceder permisos de creación a participantes sobre un esquema compartido expone tablas de otros participantes. [Identidad de ejecución](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/run-identity.html) · [Permisos de esquema](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aidug/permissions-model.html#schema-permissions)

La prueba local ejecuta las validaciones iniciales de los 34 notebooks con los parámetros que genera el backend y comprueba que rechacen rutas y catálogos ajenos. La aceptación nativa exige los resultados de Spark y el linaje del paquete instalado.
