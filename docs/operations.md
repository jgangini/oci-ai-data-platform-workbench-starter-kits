# Operación y recuperación

[Documentación](README.md) · [Primeros pasos](getting-started.md) · [Contratos vigentes](contracts.md)

## Identificar la instalación

En **Settings → Application** comprobar release instalada, commit y versiones de paquetes. Un push actualiza Git, no una VM. Un build `development` no es una release inmutable. Conservar la release resuelta, outputs y resumen privado de acceso con los registros operativos del entorno.

La tabla presenta kit, alcance, versión, fecha y configuración. La fecha corresponde al último commit del manifiesto exacto del paquete, representada en la zona guardada. Los metadatos generados se vinculan a commit, versión y SHA-256; si faltan o no coinciden no se inventa una fecha desde el filesystem. Los builds locales deben generar esa metadata mediante el [flujo de desarrollo](development.md). Ordenar o mostrar fecha no acredita que una versión haya sido instalada en AIDP. Fuente: [Settings](../apps/frontend/src/App.tsx).

Copiar un endpoint solo obtiene la dirección nativa verificada. **Open Agent Hub**, junto a Copy URL en Governance/Market Intelligence, permanece deshabilitado hasta que el módulo esté desplegado y exista una dirección Workbench verificada. Abre el hub general; no registra, activa ni concede acceso al agente. El cliente anuncia éxito al copiar y ofrece copia manual si el navegador la rechaza.

## Despliegue y acceso administrativo

Usar [Primeros pasos](getting-started.md) para entradas y límites. El [manifiesto Deploy Studio](../terraform/deploy-studio.json) utiliza schema v1 y su [gate](../terraform/release_gate.py) verifica el contrato. PLAN, APPLY y post-apply son fases distintas; no presumir una aprobación adicional introducida por este repositorio entre plan y apply.

El preflight verifica defaults heredados de tags: los buckets internos de AIDP no pueden responder valores de tags que exigen entrada del usuario. Configurar defaults automáticos antes; el despliegue no elimina defaults de la tenancy. Terraform crea la política del compartimento en home region y espera cinco minutos para propagación IAM antes de crear AIDP. La espera acotada no sustituye verificar el primer APPLY y work request nativo.

Los despliegues nuevos instalan una clave SSH pública RSA para `opc`; Deploy Studio recoge el artefacto privado `ssh-private-key.pem` y lo entrega en `vm-ssh-access.zip` con instrucciones. Es una credencial administrativa distinta del perfil API OCI. Restringir state Resource Manager y artefactos: la clave privada es estado Terraform sensible y no debe aparecer en logs, outputs públicos ni metadata VM.

Deploy Studio mantiene SSH público cerrado y no ofrece campo CIDR. Terraform directo conserva `ssh_allowed_cidr`, por defecto vacío; una apertura posterior requiere una regla OCI autorizada y un origen acotado, preferentemente `/32`. Una VM sin esa clave instalada requiere migración explícita; cambiar la imagen/correo no recupera su clave.

El correo de finalización presenta accesos y artefactos declarados por el runner, con credenciales aplicación/base de datos y código de laboratorio cuando corresponde. No incluye claves API OCI. Wallet, ZIP SSH y resumen de acceso son privados. El runner debe soportar la sección `email` del manifiesto; revisar sus artefactos y log saneado, no sustituirlos por diagnósticos privados sin filtrar.

## Usuarios y kits

| Acción en Users | Efecto |
| --- | --- |
| Add user | Seleccionar kits y/o lector God’s Eye View, sin selección inicial; aprovisionamiento reanudable. |
| Manage starter kits → God’s Eye View | Conceder/revocar grupo lector de una instalación lista; no instala el módulo. |
| Añadir kit | Assets, workflow, tablas y permisos propios del participante. |
| Update / Redeploy | Reconciliar el kit seleccionado con su versión incluida. |
| Retirar kit | Retirar solo workflow, tablas, objetos, contenido y permisos propios. |
| Eliminar participante | Limpiar kits asignados y después retirar la cuenta Identity. |

Un usuario solo lector no necesita workspace ni permisos developer AIDP. Añadir su primer kit provisiona esos recursos por el flujo normal. Con kits asignados debe quedar al menos uno; eliminar la última asignación corresponde a eliminar al participante. Conservar manifiestos e IDs y reanudar operaciones fallidas en lugar de crear cuentas/jobs/carpetas duplicados. Fuentes: [aprovisionamiento](../apps/backend/app/services/oci/aidp.py), [identidad](../apps/backend/app/services/oci/identity.py).

## Actualizar imágenes

En **Settings → Application**, **Update** abre la confirmación y **Accept** inicia la actualización. El panel muestra un único aviso: prioriza el error de la acción actual, después el resultado de la operación y finalmente los avisos de consulta de releases. Un fallo guardado sigue disponible tras recargar; publicar una release nueva no convierte una actualización fallida en exitosa.

El [workflow de release](../.github/workflows/release.yml) y el [updater VM](../scripts/release/vm_release_updater.py) exigen release estable nueva del repositorio confiable, imagen `linux/amd64`, manifiesto/checksum y comprobación del candidato en loopback. Conservar estado persistente y la imagen saludable anterior según el updater. El contenedor de aplicación no recibe Docker socket ni una interfaz general de comandos del host.

Actualizar la aplicación cambia paquetes disponibles para instalaciones/redeploy posteriores; no migra automáticamente kits, jobs o módulos activos. Comparar versiones instaladas/incluidas y ejecutar la operación explícita correspondiente. Usar únicamente assets y rutas canónicos de la versión actual; los clientes y formatos retirados se rechazan. Ver [Contratos vigentes](contracts.md).

Después comprobar `/api/health`, acceso administrador, release/commit, un ciclo representativo de kit, acceso al visor habilitado y comportamiento nativo de módulos. Salud del contenedor no valida procesamiento Spark ni inferencia.

<a id="global-module-lifecycle"></a>
## Ciclo de vida de módulos compartidos

Se administran en **Settings → Application**, separados de Users:

- [AI Data Governance](modules/ai-data-governance.md): dos agentes, cuatro tareas y 18 tablas compartidas; instalación TEST y aceptación nativa pendiente.
- [AI Market Intelligence](modules/ai-market-intelligence.md): agente TEST pausado, sin tablas ni workflows Spark propios; investigación y aceptación nativa pendientes.
- [God’s Eye View](modules/ai-gods-eye-view.md): visor privado, computes y workflows propios; administración de fuentes/identidad después de instalar.

El agente de God’s Eye View se crea y publica con retención de mensajes activada durante siete días y acción `DELETE_FIFO`. El instalador envía la política completa y comprueba su lectura nativa junto con el endpoint. Fuente: [configuración de retención de Oracle](https://docs.oracle.com/en/cloud/paas/ai-data-platform/aiwap/op-aidataplatforms-aidataplatformid-workspaces-workspacekey-agents-post.html).

### Instalación de la VM del visor

En una base preparada para módulos, revisar recursos/costos y confirmar Install. El instalador verifica stack original, fuente inmutable, recibo y variables antes de generar el plan. Solo admite creación de VM visor, dynamic group y dos políticas de alcance acotado, además de no-ops existentes. Cualquier escritura ajena, deriva de seguridad/configuración o cambio de variables bloquea APPLY; no crea otro stack ni VM no rastreada. Después sigue bootstrap AIDP y verificación nativa. Fuente: [lista permitida y plan](../apps/backend/app/modules/ai_gods_eye_view/runtime/installation.py).

En el bucket de artefactos se conservan `.control/modules/installation.json` y `.control/gods_eye_view/install/operation.json`, que vinculan entorno/fuente y operación/intento/jobs/fases. No editar recibos para saltar conflictos. Los controles y publicaciones del módulo están en Gold, separados de estos recibos.

La revisión del recibo pertenece a la infraestructura original, no a la imagen actual del portal. **Update** puede cambiar el commit de la aplicación sin impedir una instalación posterior del visor. El instalador conserva y comprueba la revisión original contra las variables del stack, junto con el SHA-256 de su archivo Terraform; una revisión distinta en el stack o un archivo modificado bloquean la operación antes de escribir.

Resource Manager puede omitir variables que heredan sus defaults: `portal_managed_modules = true` y `enabled_vm_modules = []` corresponden a una base preparada sin visor instalado. El instalador respeta esos valores del contrato vigente después de verificar el archivo inmutable, y modifica únicamente `enabled_vm_modules` al instalar el visor. Un `portal_managed_modules = false` explícito o flags retirados siguen bloqueando la operación.

GET solo observa. Close/Escape no cancelan; Resume confirma continuación del mismo ID, Retry confirma recuperación revisada y Verify comprueba sin reprovisionar. Reconciliar una respuesta perdida por GET antes de otro POST. Un timeout puede dejar OCI trabajando: seguir el job existente. Revisar primero Resource Manager y después salidas AIDP, publicación y visor; una VM saludable no acredita conversación. Bases sin red o recibo actuales se rechazan, sin vía de activación antigua.

El registro `enabled_vm_modules` implementa únicamente `gods_eye_view`. Otro módulo exige código, packaging, lista Terraform y aceptación explícitos; añadir un nombre no habilita carga arbitraria de VMs. Ver [contrato de plan](../terraform/tests/check_module_plan.py).

Una base sin recibo, red o recursos de identidad actuales no se adopta ni se migra desde el portal. Corregir el contrato del entorno fuera de esta operación antes de volver a intentarla; una imagen nueva no lo crea automáticamente.

## Destrucción y retención

La VM visor, grupo y políticas pertenecen al stack original. Un Destroy debe incluir sus recursos rastreados; comprobar plan e inventario final. El instalador no incorpora teardown general de AIDP. Limpieza de participantes/Governance, borrado sintético y retención son operaciones separadas.

Antes de destruir, revisar escritores activos y buckets retenidos/reutilizados. Quitar una VM no demuestra que hayan desaparecido workflows, tablas u objetos. Revisar las fuentes actuales: [Terraform del visor](../terraform/h_gods_eye_view.tf), [almacenamiento](../terraform/f_oci_objectstorage_bucket.tf), [bootstrap](../terraform/hooks/gods_eye_view_bootstrap.py) y [ciclo de vida AIDP](../apps/backend/app/services/oci/aidp.py).

## Limpieza sintética

Es una operación de datos, no desinstalación. Confirmar alcance social o sensores. Reintentar una falla conserva ID/diario después de diagnosticar; un ID cancelado es terminal y requiere nueva confirmación/ID. Parcial/cancelado no equivale a completado. No borrar checkpoints ni estado de diálogo para forzar éxito. La [guía del visor](modules/ai-gods-eye-view.md) detalla datos reales, controles e historial que deben conservarse.

## Controles Object del visor

Las instalaciones actuales inicializan controles Object y diario inmutable en el bucket Gold seleccionado. Verificar configuración, ETags, publicación y contenido del diario antes de declarar el runtime listo. No existe un camino alternativo de controles Oracle ni un migrador de instalaciones antiguas. No borrar estado para pasar una comprobación; mantener la operación y resolver el conflicto de ownership o contenido. Ver [contratos de datos](contracts.md#datos-y-evidencia) y [guía del visor](modules/ai-gods-eye-view.md).

## Diagnóstico y recuperación

| Síntoma | Comprobar primero |
| --- | --- |
| Participante pendiente | Fase/operación, visibilidad y permisos exactos; no duplicar usuario. |
| Guard de workspace/catálogo | Paquete instalado frente a parámetros; [límite conocido](getting-started.md#current-implementation-limits), sin deshabilitar guards. |
| Workflow no inicia | Estado y referencia exacta del compute. |
| Linaje ausente | Tareas completas, escrituras y configuración; no sustituir evidencia por prosa. |
| Agente 502/timeout | Despliegue exacto, compute, modelo y herramienta/log nativo. |
| Captura completa, tabla vieja | Durabilidad Landing, backlog, publicación y proyección. |
| Reset incompleto | ID, error/cancelación, writer y diario de reemplazo; no borrar originales prematuramente. |
| HTTP 409 al editar | Revisión/ETag actual y cambio concurrente; no sobrescribir a ciegas. |
| Índice local no disponible | Integridad del diario Object y proyección SQLite; no borrar fuente autoritativa. |
| Governance pendiente | Snapshot actual, compute agente, modelo y RBAC. |

Mantener privados logs, IDs de petición y recibos; sanear secretos/datos personales al compartir. Estado persistente, referencias de release y backups deben conservar sus controles. Restaurar un snapshot anterior después de nuevas escrituras puede perder capturas o cancelaciones: reconciliar primero. No existe garantía general de recuperación entre todos los almacenes; validar restauración aislada antes de depender de ella.

Las rutas Social Network retiradas devuelven HTTP 404 y la imagen no elimina sus recursos remotos. Market Intelligence tiene su [instalación y recuperación](modules/ai-market-intelligence.md#installation-and-recovery) independiente y aceptación pendiente; ni Agent Hub ni un estado activo sustituyen sus gates.
