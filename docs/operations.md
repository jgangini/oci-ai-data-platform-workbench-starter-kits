# Operación y recuperación

[Documentación](README.md) · [Primeros pasos](getting-started.md) · [Desarrollo](development.md)

## Usuarios

El administrador usa /admin/users para crear participantes y asignar uno o varios kits. El portal conserva la operación y consulta el progreso. Los fallos no borran el estado necesario para reintentar.

El portal funciona exclusivamente como laboratorio y permite el registro con el código del instructor. Si falta su verificador, el registro devuelve un error de configuración; el administrador puede guardar un código válido desde Settings. Añadir, retirar o eliminar material mantiene el aislamiento del participante y solo actúa sobre recursos verificados como propios.

## Configuración y versiones

/admin/settings mantiene AIDP Workbench, zona horaria y versiones de aplicación y paquetes. Los instaladores de módulos pertenecen a sus [repositorios independientes](README.md#módulos-independientes).

El [updater](../scripts/release/vm_release_updater.py) exige una release inmutable, prepara candidato, comprueba salud y conserva la imagen anterior para recuperación. Solo una instalación marcada kits-only puede recibir esta distribución. No cambie manualmente el perfil de una VM combinada para saltar ese control.

Actualizar el portal no reprovisiona todos los kits de participantes. Las acciones individuales deben conservar identidad, permisos y versiones exactas.

## Diagnóstico

- Revise fase y error del servidor antes de reintentar.
- Compruebe salud de portal, Identity Domains, AIDP y bootstrap de la misma instalación.
- Conserve logs y recibos en almacenamiento privado; no publique claves, wallets ni configuración completa.
- Ante un fallo de actualización, confirme versión activa y salud.
- No destruya la pila para resolver un fallo de aplicación.

El [plan de separación](module-repository-separation.md) distingue código, releases y aceptación OCI. Los módulos ya instalados permanecen en sus pilas hasta una transición explícita; esta distribución no importa automáticamente su estado.
