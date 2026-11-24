# Pruebas y aceptación

[Documentación](README.md) · [Desarrollo](development.md) · [Tutorial](tutorial.md)

| Comprobación | Evidencia que aporta |
| --- | --- |
| Contrato de Deploy Studio | El backend reconoce el manifiesto v2, sus campos, las salidas, los hooks y las referencias del correo. |
| Pruebas Python | Los contratos de kits y despliegue, el registro, las reglas de arquitectura y el aprovisionamiento simulado respetan el comportamiento esperado. |
| Pruebas de React y compilación | Los flujos del portal pasan las pruebas del frontend y el paquete compila. |
| Terraform e imagen | Los proveedores, formatos, configuración y planes simulados pasan sus comprobaciones, y la imagen compila. |
| Ejecución en Workbench | Un participante real del laboratorio puede ejecutar el workflow asignado y verificar tablas y linaje de ese paquete. |

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

1. Comprueba que el aprovisionamiento terminó, que las rutas coinciden con el paquete instalado y que los permisos funcionan con la cuenta del participante.
2. Ejecuta cada workflow con sus parámetros asignados. Revisa las aserciones de sus notebooks, el contenido de las tablas Gold y el linaje nativo en Master Catalog.
3. Registra release, versión y hash del kit, tarea, estado y resultado esperado. Guarda los logs y recibos con datos de la tenancy fuera de Git.

El estado activo del portal o un Apply correcto solo acredita su fase. La [incompatibilidad de rutas de los notebooks 2.0.0](getting-started.md#current-implementation-limits) sigue siendo un requisito de aceptación en OCI.
