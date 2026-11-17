# Desarrollo y validación

[Documentación](README.md) · [Operación](operations.md) · [Contratos](contracts.md)

## Estructura

| Ubicación | Responsabilidad |
| --- | --- |
| apps/backend/app/kits | Cinco paquetes y convenciones de nombres. |
| apps/backend/app/packages | Catálogo y validación de contratos. |
| apps/backend/app/services/oci | AIDP, Identity Domains y bootstrap de credenciales. |
| apps/backend/app/services/releases.py | Versiones de la aplicación. |
| apps/backend/app/main.py | Composición de API y operaciones de participantes. |
| apps/frontend | Registro, Users y Settings. |
| terraform | Recursos OCI, manifiesto y hooks. |
| scripts/kits | Generadores de contenido de kits. |
| scripts/release | Metadatos, imágenes y actualización. |

Los módulos independientes no son dependencias Python ni rutas HTTP del portal.

## Entornos

Use Git, Docker Compose, Python 3.12 y Node 22 según [CI](../.github/workflows/ci.yml). Terraform y OCI están fijados en el workflow y [a_versions.tf](../terraform/a_versions.tf).

Copie [.env.example](../.env.example) a .env.dev solo si no existe, configure hashes locales propios y ejecute docker compose -f docker/docker-compose.dev.yml up --build -d. El perfil local usa fixtures y HTTP en loopback. No acredita permisos ni jobs reales.

El [asistente OCI-local](../scripts/development/bootstrap_local_oci_env.py) y [Compose](../docker/docker-compose.oci-local.yml) conectan a recursos reales: no son una copia aislada. Proteja configuración, clave y correo de acceso.

## Validación

1. Ejecute scripts/arch-preflight.ps1.
2. Ejecute pytest sobre apps/backend/tests y terraform/tests con las dependencias declaradas.
3. Ejecute npm test y npm run build en apps/frontend.
4. Ejecute formato, validación y pruebas Terraform con proveedores simulados según CI.
5. Ejecute scripts/arch-postflight.ps1 para actualizar el grafo existente y comprobar Sentrux.

En Windows use un --basetemp nuevo bajo .local: pytest borra destinos existentes. Las pruebas nativas Sentrux/PowerShell pueden omitirse en contenedores Linux, pero deben ejecutarse en el host; informe cada omisión.

La aceptación nativa sigue los [límites documentados](getting-started.md#current-implementation-limits) y comprueba los workflows y datos del participante en su instalación AIDP.

## Publicación

El perfil kits-only publica exclusivamente imagen y manifiesto del portal. Respete la [frontera de actualización](contracts.md#releases). No incluya credenciales en imágenes, metadatos, fuentes o fixtures.
