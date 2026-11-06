# Separación de módulos en repositorios independientes

[Documentación](README.md) · [Contratos actuales](contracts.md) · [Desarrollo](development.md)

Fecha: 2026-11-05. Estado: implementación en curso; corte local kits-only validado, repositorios privados creados y candidatos independientes en pruebas. Aceptación OCI y publicación de releases pendientes.
Fuente congelada para los cuatro trabajos: `3c82617fd298933995e954955841007253cc0985` (`v4.0.11`).
Este documento es el contrato de coordinación. Cada destino mantiene su inventario en
`docs/migration-assessment.md`; esos inventarios complementan este plan, no crean otro estándar.

## Objetivo y límite

Convertir Governance, Market Intelligence y God’s Eye View en productos desplegables y
actualizables por separado desde Deploy Studio. El repositorio actual conservará únicamente
los cinco kits de participantes y los servicios que necesitan: Banking, Telecommunications,
Telco Customer 360 Lineage, Retail y Healthcare.

Corrección de alcance del usuario: ninguno de los tres productos incorpora un portal como
Starter Kits. Governance y Market se instalan y administran mediante Deploy Studio y sus
herramientas nativas AIDP, sin frontend ni VM de portal. Deploy Studio lista el administrador
elegible para la instalación. God’s Eye View es el único producto con interfaz propia en su
VM: visor y configuración del streaming/proveedores integrada, sin otro portal de instalación.

La separación de código no migra automáticamente las instalaciones existentes ni modifica
su propietario Terraform. Primero se obtienen productos reproducibles; después se retiran
las dependencias del general. Las instalaciones actuales permanecen en su versión y stack
hasta una transición explícita. No se incorporan aliases ni clientes antiguos.

La validación inicial acredita instalación, recuperación y recursos nativos. La calidad de
respuestas de agentes, una campaña de investigación y el consumo pagado de actores son
aceptaciones distintas. Refactorizar un agente sí requiere comprobar su comportamiento
afectado; un estado ACTIVE por sí solo no prueba equivalencia.

## Responsables

| Propietario | Repositorio GitHub solicitado | Carpeta local | Trabajo exclusivo |
| --- | --- | --- | --- |
| Este chat | `jgangini/oci-ai-data-platform-workbench-starter-kits` | `D:/dev/codex-oci-ai-data-platform-workbench-starter-kits` | Contrato común, separación de servicios compartidos, portal de kits, corte y coordinación con Deploy Studio. |
| [Governance](codex://threads/01a1ae24-1247-7c43-a7f9-2333802f7f6b) | `jgangini/aidp-ai-data-gobernance` | `D:/dev/codex-aidp-ai-data-gobernance` | Dos agentes, tablas, workflow documental e instalador sin interfaz, invocado por Deploy Studio. |
| [Market](codex://threads/01a1ae23-cce8-76c3-8a40-44bcff3a0d12) | `jgangini/aidp-ai_market-intelligence` | `D:/dev/codex-aidp-ai_market-intelligence` | Agente, instalador, entrada protegida y validación Apify. |
| [God’s Eye View](codex://threads/01a1ae23-f55c-74e3-b111-6c9ff0b059cf) | `jgangini/aidp-ai-gods-eye-view` | `D:/dev/codex-aidp-ai-gods-eye-view` | Visor upstream, extensiones AIDP, agente, administración de streaming y VM del producto. |

Los nombres GitHub se conservan exactamente como los pidió el usuario. No obligan a
renombrar IDs de paquetes, claves AIDP ni recursos existentes. Cada chat usa su `cwd`
explícito: la metadata inicial de los tres chats aún mostraba `codex-cloudtechnext`.
La consulta autenticada inicial de los tres remotos devolvió HTTP 404; su creación o acceso
debe verificarse en la fase de repositorios. No se ha creado ningún remoto durante el plan.

Cada chat modifica únicamente su destino. El origen es de solo lectura para los tres.
Este coordinador es el único dueño de los cambios al origen, CloudTechNext, publicación en
su catálogo y operaciones sobre recursos compartidos. Los chats no reinician Docker,
servicios, stacks, agentes o workflows compartidos por su cuenta.

## Evidencia y dependencias que hay que separar

| Fuente actual | Consecuencia para la extracción |
| --- | --- |
| [Catálogo](../apps/backend/app/packages/catalog.py) | Registra kits y los tres módulos; eliminar sus entradas únicamente cuando los consumidores hayan sido desacoplados. |
| [Servicio AIDP](../apps/backend/app/services/oci/aidp.py) | Importa constantes de Governance y credenciales desde God’s Eye View; los kits necesitan un servicio OCI sin dependencias de dominio de módulos. |
| [Composición API](../apps/backend/app/main.py) y [portal](../apps/frontend/src/App.tsx) | Montan instaladores, permisos y pantallas de módulos. No basta con mover `app/modules`. |
| [Autonomous](../apps/backend/app/services/oci/autonomous.py) | Revisar qué provisión pertenece a kits, memoria o Governance antes de retirar imports y schemas. |
| [Contrato Deploy Studio](../terraform/deploy-studio.json), [hooks](../terraform/hooks) y [Terraform AIDP](../terraform/j_oci_ai_data_platform.tf) | El hook incluye fuentes de módulos. Terraform crea AIDP sin selector existente; `new/existing` para AIDP es trabajo nuevo, aunque Autonomous y buckets ya ofrecen selección. |
| [Contrato Governance](https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits/blob/3c82617fd298933995e954955841007253cc0985/apps/backend/app/modules/ai_data_governance/aidp/manifest.json) | Declara 18 tablas, fuentes y assembly. Hay código importable y ensamblado mediante strings/extracción de símbolos: conservar el primero y sustituir el ensamblado frágil, no reescribir todo sin discriminación. |
| [Contrato Market](https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits/blob/3c82617fd298933995e954955841007253cc0985/apps/backend/app/modules/ai_market_intelligence/aidp/manifest.json) | El instalador actual no crea tablas ni workflows Spark. Conservar esa realidad; no añadir recursos solo para llenar carpetas. |
| [Upstream del visor](https://github.com/jgangini/oci-ai-data-platform-workbench-starter-kits/blob/3c82617fd298933995e954955841007253cc0985/modules/ai-gods-eye-view/upstream.json) | Ya existe commit/archive/hash y parches con hashes. Esta capacidad se traslada al nuevo producto. |
| [Contratos de releases](contracts.md#releases) | Los cuatro assets actuales están acoplados al general. Definir conjunto exacto por producto y actualizar loader, CI y updater juntos. |

El grafo existente fue consultado como mapa: su reporte indica commit `2adb89a4`, anterior
al corte. Las decisiones anteriores se verificaron en el código actual. No se acredita la
arquitectura nueva mediante ese grafo ni con los tests de la release anterior.

## Arquitectura de destino

Cada producto posee su instalador, adaptadores OCI, fuentes AIDP, infraestructura,
artefactos de release, actualización y documentación. Governance y Market son paquetes
Python/CLI y hooks sin servidor web permanente; estado, errores y recuperación se integran
en Deploy Studio. No se extraen sus pantallas React ni se crean APIs HTTP de portal propias.
Arko guía límites y contratos del código Python sin imponer FastAPI, React o una VM.
Solo GodEye necesita composición API, autenticación e interfaz en su VM, reutilizando el
stack pertinente. No ejecutar `init` sobre una carpeta con contenido.

La consulta Arko `context ai_data_governance --root <origen>` no pudo obtener un mapa:
el origen no tiene `.arko/project.json`. Su incorporación a los destinos es trabajo de
implementación; leer la skill no acredita que `arko check` haya pasado.

El repositorio de kits no será una biblioteca de la que dependan los módulos. Ninguna
imagen o hook necesita rutas de un repositorio hermano, imports `app` del general,
submódulos que cambien con `main`, ni el portal original en ejecución.

Durante la extracción se copia solo la infraestructura/adaptación mínima demostrada por
imports y pruebas, con inventario de procedencia y commit. Se acepta inicialmente una
pequeña base repetida y propiedad explícita por producto; no se crea un cuarto framework
compartido ni una dependencia central de releases para sustituir el monolito. Si aparece
una biblioteca realmente estable, se decide aparte con consumidores y versionado claros.

Distribución orientativa, ajustada a responsabilidades comprobadas por cada chat:

```text
src/<producto>/         # instalador, dominio y adaptadores OCI importables
apps/                   # solo GodEye: API e interfaz de su VM
  backend/app/
  frontend/
aidp/
  manifest.json         # una sola relación fuente -> recurso nativo
  master_catalog/<catalog>/schemas/<schema>/tables/
  workspaces/<logical-workspace>/
    files/agents/<agent>/
    files/python/
    notebooks/
    workflows/
    compute/
    agent_flow/         # definiciones de agentes/deployments/AI compute
terraform/
  deploy-studio.json
  hooks/
docker/                 # GodEye; en otros productos solo si el runner lo necesita
scripts/                # build, validación, publicación, actualización
tests/                  # o tests existentes junto a la aplicación; una convención
docs/
```

Se mantiene el esquema de manifiesto vigente mientras se verifica la ubicación exacta
de cada definición. La estructura es un espejo lógico: workflows, compute y Master Catalog
son objetos API, no carpetas que se creen por subir archivos. El manifiesto distingue
fuentes, archivos Workspace, objetos API y dependencias, con IDs lógicos y bindings del
entorno separados. `.artifact` es una ruta nativa de publicación explícita, no una segunda
fuente editable ni un lugar para secretos. No renombrar rutas/IDs persistentes al mover
carpetas. Crear solo directorios con contenido real; Market puede declarar listas vacías.

Los agentes tendrán módulos importables por responsabilidad (graph, state, nodes, tools,
prompts, adapters) bajo su fuente única. Los notebooks invocan el entrypoint; no contienen
una segunda implementación. Si AIDP necesita un programa autocontenido, se genera al
empaquetar con una lista explícita de fuentes, orden, hashes y pruebas de equivalencia.
No editar el generado, ocultar bundles codificados ni ejecutar strings como código de negocio.

## Contrato común de despliegue y redespliegue

Cada repositorio entrega `terraform/deploy-studio.json` schema 1, Terraform, preflight y
post-apply autocontenidos, outputs y release verificable con fuentes/paquetes inmutables.
GodEye incluye su imagen de VM; Governance y Market no necesitan una imagen de portal.
Los `project_id`
de las tres soluciones deben ser únicos y congelarse con su primera release. CloudTechNext
consume el manifiesto genérico; no tendrá un `if` por módulo.

El consumidor comprobado es
`D:/dev/codex-cloudtechnext/app/backend/app/services/deployment_source.py`:
`ManifestField`, `PreflightContract`, `PostApplyContract` y `DeploymentManifest`.
Ya acepta secretos de formulario, `secret_inputs` separados para preflight/post-apply y
campos condicionales. No acepta claves extra ni un options_source AIDP arbitrario. Primera
versión: selección `new/existing` y OCID explícito validados por preflight; un selector de
descubrimiento nuevo requiere cambio genérico y tests del consumidor, coordinado aquí.

### Administrador seleccionado en Deploy Studio

El formulario común listará administradores elegibles de la identidad/dominio y destino
seleccionados, usando la identidad OCI autenticada del despliegue. Se envía un ID estable;
el nombre/correo es presentación, no autorización. El backend revalida usuario activo,
dominio, ámbito y permisos antes de instalar y al reanudar. Cambiar de tenancy, dominio o
plataforma invalida la selección anterior. No mostrar usuarios de otro ámbito ni permitir
que un ID manipulado se convierta en administrador.

En una plataforma existente se comprueban los roles efectivos AIDP. En una plataforma nueva
se lista la identidad elegible del dominio, se declaran las asignaciones necesarias dentro
del plan autorizado y se verifica su efectividad después del bootstrap, antes de publicar
agentes/workflows. No se puede consultar roles de una plataforma que todavía no existe.
El administrador queda vinculado al recibo y a los recursos; Resume conserva ese vínculo.

Seleccionar un usuario no permite firmar como él: los hooks actúan con las credenciales
autorizadas del operador y los permisos/delegación admitidos por OCI. No se solicita una
contraseña local `admin` ni se crea otro provisioner como atajo. Si falta capacidad de
actuar/asignar bajo esa identidad, preflight lo informa antes de mutar recursos.

Este listado es capacidad nueva del consumidor Deploy Studio: implementar un origen de
opciones genérico, su API autenticada y tests de aislamiento, selección obsoleta, usuario
sin privilegios y revalidación. No declarar un `options_source` no admitido en los manifiestos
antes de desplegar soporte. La integración pertenece a este chat; los tres productos
consumen el mismo contrato de administrador verificado.

| Recurso | Nuevo | Existente | Propiedad / validación |
| --- | --- | --- | --- |
| Compartment | Crear cuando se elija. | OCID autorizado. | Identidad, región y permisos exactos; no adoptar por nombre. |
| AIDP y workspace | Crear plataforma y workspace explícitos. | Plataforma y workspace seleccionados; validar `/roles` con identidad efectiva. | La plataforma externa no entra como recurso a destruir en el stack del producto. Validar API, endpoints y conectividad. |
| Autonomous | Crear si el producto realmente lo necesita o se solicita. | OCID, conectividad, wallet/credencial y schema autorizado. | Una base existente no sustituye AIDP; la app no obtiene ADMIN permanente para tareas ordinarias. |
| Buckets | Crear los que usa el producto, incluido artefactos. | Validar región, namespace, permisos y bindings de volumen/catalog. | Separar datos, controles y recibos; no eliminar bucket externo. Mantener las cinco selecciones distintas cuando aplique medallion. |
| VM / red | GodEye: imagen y recursos de red declarados. Governance/Market: sin VM de portal. | Solo destino compatible con identidad, recibo y contrato verificados cuando el producto requiera VM. | No sobrescribir cualquier VM por IP. La primera instalación y la actualización de una instalación propia son acciones diferentes. |

En God’s Eye View, Autonomous no es una dependencia de consulta o escritura Gold. No
añadirla artificialmente para que los tres Terraform sean idénticos. Soportar parámetros
comunes no obliga a crear recursos innecesarios.

El recibo de instalación registra producto, deployment/operation ID, commit/digests,
stack, destino, identidades exactas y recursos `owned` frente a `external`, con versiones
de contrato. No contiene claves privadas ni tokens. Se almacena duraderamente con
escrituras condicionales/lock para excluir ejecuciones concurrentes.

- **Deploy:** valida primero, genera plan y crea únicamente lo elegido.
- **Resume:** continúa el mismo operation ID y recursos comprobados tras un fallo o reinicio.
- **Update:** cambia a una release validada de la misma instalación, con checks del candidato y recuperación de artefactos; GodEye incluye rollback de imagen.
- **Redeploy:** reconcilia los recursos propios conservando datos, IDs, permisos y checkpoints; no es destroy/create.
- **Delete:** requiere alcance explícito y plan; excluye recursos externos/compartidos y no borra datos por defecto.

Estas son las obligaciones del contrato de destino, no capacidades acreditadas por un
botón actual. El inventario de Governance encontró que la reconciliación vigente admite
`install` y bloquea otras operaciones. El redespliegue solicitado exige implementación y
regresión propias; el borrado permanecerá deshabilitado mientras no tenga soporte seguro.

Una VM existente arbitraria necesita un contrato de bootstrap soportado, acceso legítimo,
revisión de puertos/servicios y rollback; si no puede comprobarse, se rechaza con motivo.
No se promete esa capacidad por el mero hecho de poder instalar sobre AIDP existente.

Secretos: `.oci` solo por la configuración privada autorizada; jamás copiarlo a commits,
imágenes, correo de acceso o artefactos de release. El token Apify pasa por `secret_inputs`
y la credencial runtime protegida; comprobar que no termine en Terraform state, plan,
user-data, outputs, logs o respuestas del navegador. El nuevo formulario no lo hash-ea
como contraseña, porque el runtime necesita la credencial real.

## Trabajo por producto

### Governance

Extraer dominio e instalación, incluidas las responsabilidades hoy embebidas en el servicio
AIDP general. Reorganizar los dos agentes con AIDP Agent Flow LangGraph: adapters nativos
separados del grafo portable, estado tipado, nodos/herramientas comprobables y prompts
mantenidos como fuentes. Conservar contratos de 18 tablas, permisos, workflow y memoria
que el inventario confirme. Probar equivalencia del programa publicado y sus entradas,
no solo que el Python compile. No confundir errores funcionales preexistentes con la
extracción ni dar por completada una ejecución documental fallida.

No trasladar `GovernanceModuleManager` ni crear administración React/FastAPI propia.
Extraer su lógica de instalación a comandos/hooks que consumen el administrador verificado,
persisten el recibo y exponen progreso/reanudación a Deploy Studio.

### Market Intelligence

Incluir el token Apify en el despliegue. El preflight verifica autenticación, actores exactos
declarados por el producto y acceso/configuración relevante; no basta el HTTP 200 de la
cuenta. Distinguir actor visible, permisos de ejecución, alquiler/entitlement, límites y
disponibilidad de build. Registrar únicamente resultados no sensibles por actor.

Las comprobaciones sin coste no deben iniciar runs. Si Apify no permite demostrar un
permiso sin ejecución, devolver `no comprobado` con el requisito concreto; no marcarlo
válido ni ejecutar una captura pagada oculta. Un smoke pagado necesita alcance y límite
explícitos. Revalidar al cambiar token/actores y antes del primer trabajo, porque los
permisos pueden cambiar después del despliegue. Tests: token inválido, acceso denegado,
actor ausente, restricciones, timeout y recuperación sin duplicar runs.

El inventario actual contiene trece perfiles de actor, uno suspendido. Derivar los actores
requeridos del registro canónico y sus builds fijados; no duplicar esa lista en Terraform
ni reactivar un perfil suspendido. La instalación actual almacena la credencial, pero no
comprueba todos esos permisos ante Apify: ese preflight también es funcionalidad nueva.

Preservar el instalador durable, espera del compute y estado inicial TEST instalado/pausado.
La migración no habilita investigación ni horarios ni inventa workflows Spark o tablas.
El administrador y el token Apify se introducen en Deploy Studio; no hay diálogo Market,
frontend, login local ni VM de administración en el producto independiente.

### God’s Eye View

Extraer backend del módulo, visor, configuración de proveedores/OCI, interfaz de streaming
social y sensores, schedules, acciones Run/Pause y controles de concurrencia. La VM del
producto servirá la configuración autenticada dentro de la interfaz GodEye, sin replicar
el portal Starter Kits ni depender de Settings del portal de kits. La instalación y el
redespliegue pertenecen a Deploy Studio. Trasladar también los endpoints/controladores
reales del streaming, no solo copiar componentes React.

Separar en esa VM el ingreso TLS/autenticación, API de administración y runtime del visor
mediante servicios internos. El runtime y puertos privados no se publican directamente.
Identidad OCI, sesiones y RBAC deben funcionar sin el portal original. Lector no puede
administrar streaming, secretos o modelos. No confiar en headers del cliente; solo los
emite el gateway tras autenticar. Verificar revocación y aislamiento de participantes.

Conservar controles durables Object Storage, Gold de AIDP, checkpoints, publicaciones,
computes separados y workflows social/sensores. Mover la UI no autoriza iniciar, reiniciar
ni borrar streams. El agente se organiza con LangGraph sin cambiar por accidente su
contrato de evidencia, alcance de publicación o herramientas autorizadas.

Para actualizar [upstream](https://github.com/bilawalsidhu/gods-eye-view):

1. Registrar commit, archive SHA-256, licencia y avisos de terceros en el lock de upstream.
2. Separar extensiones propias de los puntos mínimos de parche. Conservar la procedencia
   del actual `upstream.json` y su commit fijado; no asumir que la última versión es compatible.
3. Preparar cada actualización en rama/PR: descargar el archive fijado, comprobar hashes,
   aplicar parches de forma determinista y fallar si un contexto no coincide.
4. Ejecutar build nativo, tests de integración AIDP, streaming, RBAC y contratos API.
   Arko organiza el control Python; no convierte el frontend upstream en otra aplicación.
5. Publicar una imagen inmutable con digest y manifiesto; actualizar con candidato saludable
   y rollback a la imagen anterior. La VM no hace `git pull main` ni compila upstream al arrancar.

## Secuencia y entregables

| Fase | Responsable | Entrega y condición para pasar |
| --- | --- | --- |
| 0. Inventario | Tres chats + coordinador | `migration-assessment.md` por producto; lista de archivos y consumidores fuera del módulo, tests, recursos y brechas. Fuente congelada idéntica. |
| 1. Contratos | Coordinador integra propuestas | Matriz new/existing, propiedad, inputs/outputs, release y selector de administrador con validación backend; fixtures del parser real Deploy Studio. Decisiones de VM existente explícitas. |
| 2. Extracción | Cada chat en su destino | Repositorio Git independiente con procedencia del corte, exclusiones privadas, CI, fuentes, Terraform y docs. Sin imports ni descargas runtime del monolito. No retirar origen aún. |
| 3. Refactor de producto | Cada chat | Arko por responsabilidades; agentes organizados; streaming propio GodEye; preflight Apify. Commits separados de extracción para comparar comportamiento. |
| 4. Release candidata | Cada chat; validación coordinada | Tests, paquetes/digests, imagen solo donde corresponda, manifiesto y contratos completos. Crear/publicar los repos exactos y revisar visibilidad/acceso antes de catalogar. |
| 5. Deploy Studio | Coordinador | Registrar las tres releases verificadas; fuente siempre tag inmutable resuelto a SHA. Test de nuevo/existente y resume/update/redeploy, serializando las operaciones cloud compartidas. |
| 6. Kits únicamente | Este chat | Retirar módulos y sus consumidores del general; CI completo y deployment de kits sin imágenes/rutas de módulos. No actualizar la VM combinada automáticamente. |
| 7. Transición de entornos | Coordinador + dueño del producto | Plan por instalación con identidades/estado/recibos, copia recuperable y punto de corte; un único propietario por recurso. Separada de esta extracción de código. |

Se puede paralelizar inventario, extracción y tests locales en destinos aislados. El contrato
común precede a la implementación de preflight/release; el retiro del origen depende de las
tres entregas. La coordinación consulta los resultados de los chats y envía instrucciones
de la siguiente fase, no les encarga modificar simultáneamente archivos compartidos.

## Retirada del repositorio general

Este chat mantiene una lista de consumidores verificados, con estos pasos:

1. Separar capacidades OCI que usan kits de la orquestación Governance y las credenciales
   implementadas dentro de GodEye. Evitar dependencias de kits hacia `app.modules`.
2. Dejar solo cinco entradas de kits en catálogo; quitar routers, workers, modelos y UI de
   instalación/administración de módulos. Conservar identidad y registro de participantes.
3. Quitar del hook/bootstrap únicamente fuentes y recursos propios de módulos. Auditar
   Terraform, include_paths, Docker COPY, startup, Nginx, update, release y CI juntos.
4. Retirar pruebas exclusivas ya transferidas; conservar pruebas de contratos, permisos y
   aprovisionamiento de kits. Cada eliminación tiene sustituto/destino o motivo de obsolescencia,
   no se decide por cantidad de tests o por el nombre antiguo `lab` de un contrato vigente.
5. Actualizar documentación y AGENTS.md al comportamiento real kits-only en el mismo cambio.
   Hoy sus reglas describen el producto combinado; no cambiarlas anticipadamente como si
   la migración estuviera terminada.
6. Publicar una release de cambio de alcance explícito. El updater de una instalación
   combinada debe rechazar el salto kits-only si perdería controles de módulos. Es un guard
   de migración, no un adaptador para clientes antiguos. Mantener la imagen anterior utilizable.

No ejecutar `terraform apply` sobre el stack actual tras quitar recursos de la configuración:
eso puede proponer su destrucción. El código nuevo se prueba en destinos propios. Una
transferencia futura de estado requiere plan separado, inventario exacto y comprobación
de ausencia de creates/deletes/replacements inesperados; nunca dos states dueños del mismo recurso.

## Validación y documentación de cierre

Cada repositorio usa un solo preflight/postflight, Sentrux con su baseline y reglas reales,
Graphify actualizado si ya existe, y los tests pertinentes. Arko `check` se incorpora con
mapas/contratos válidos; no aceptar un baseline nuevo para ocultar una ruptura. No crear
grafos en carpetas vacías ni copiar el baseline de otro producto.

Matriz de aceptación mínima por producto:

- Release construible desde checkout limpio sin repos hermanos; secretos ausentes de Git,
  archivo Terraform distribuido, imagen y artefactos públicos.
- Parser Deploy Studio schema 1 y runner real: paths, dependencias, inputs secretos,
  post-apply, errores redactados y outputs; administrador elegible revalidado en servidor.
- Governance y Market se instalan y reanudan sin portal, servidor HTTP permanente ni VM
  administrativa; no se solicita usuario/contraseña local. GodEye conserva su única UI.
- Terraform con mocks para combinaciones new/existing y pruebas nativas acotadas para
  los límites reales; sin reemplazar ni destruir recursos externos.
- Reintento tras timeout/reinicio y doble envío: un operation ID, un conjunto de recursos,
  sin ampliar permisos ni arrancar dos productores.
- Agentes creados y configuración nativa de la revisión comprobada. TEST y PROD separados.
  Donde exista workflow: schedule persistido, ejecución observada y resultado informado
  como SUCCESS/FAILED/RUNNING, sin presentar un fallo como prueba superada.
- Actualización fallida recupera los artefactos previos (imagen en GodEye); redeploy preserva datos, membresías y
  checkpoints; revocación sigue efectiva después de reiniciar/actualizar.
- General: kits operativos sin importar, empaquetar ni administrar ninguno de los módulos.

Documentación final en español, en cada repo: README breve; primeros pasos; arquitectura y
mapeo a AIDP; despliegue nuevo/existente; operación/reanudación/actualización/rollback;
identidad/roles/secretos; contratos y limitaciones; desarrollo y validación. GodEye incluye
guía de actualización upstream/parches y administración streaming. Market incluye la matriz
de actores y niveles de comprobación. Governance incluye grafos, nodos, tablas y workflow.
Registrar evidencia y comandos realmente ejecutados; tenant IDs, tokens y recibos privados
se mantienen fuera de las guías reutilizables.

## Estado de ejecución

- Los tres repositorios privados están creados. Governance publica v0.2.0-rc.2 y Market v0.1.0-rc.2, ambas privadas e inmutables, con las fichas AIDP AI Data Governance y AIDP AI Market Intelligence. God’s Eye View publicó v1.0.0 y prepara la corrección de primer arranque. Las releases no acreditan aceptación OCI.
- Origen: portal/API/catálogo exclusivos para los cinco kits, Terraform y hooks desacoplados, distribución de dos assets con protocolo2 y perfil kits-only. El updater rechaza cruces desde instalaciones combinadas.
- Origen: frontend76 pruebas y compilación aprobados; backend/Terraform 380 casos aprobados en la suite integrada final; once comprobaciones nativas omitidas en Linux y cubiertas por19 pruebas de arquitectura en Windows. Releases43/43, Terraform validate y12 pruebas simuladas aprobados. Imagen Docker compilada y arrancada sin red externa: salud/login HTTP200 y rutas retiradas del visor HTTP404. Publicación pendiente.
- Origen: Graphify actualizado; Sentrux dos reglas aprobadas y gate sin degradación (6504 a6657, cero ciclos y god files). El aumento de coupling0.02 a0.06 se registra; no cambió el baseline.
- CloudTechNext: selector genérico oci_aidp_administrators, contexto de dominio/plataforma, candidatos activos para new, RBAC efectivo para existing y revalidación del servidor. Pruebas de inventario/parser85, API74 y autorización5 aprobadas; buildfrontend y pruebas de formulario aprobados. Revisión independiente completada y ruta API AIDP alineada con los instaladores. Postflight final aprobado: 21 reglas Sentrux, calidad6704 a6708 y sin degradación, tras separar validación de identidad, paginación y consulta de roles. La referencia inicial se conserva.
- Los nuevos productos deben completar instalación/reanudación/update/redeploy, empaquetado sin dependencias del monolito, publicación y aceptación nativa serializada. Un candidato parcial privado no acredita esos gates.
- Los documentos del origen describen el corte local actual. Las referencias al código retirado en el inventario de extracción apuntan a la fuente congelada, no a rutas desaparecidas.
- Starter Kits admite únicamente Laboratory: se retiraron selector, variable y ramas del modo Production; el código de registro sigue siendo obligatorio. El commit 8acfae1 del PR50 pasó CI, las 76 pruebas frontend y build, la suite backend/Terraform, 12 escenarios Terraform y postflight (dos reglas, gate sin degradación, calidad6659). Se conserva la configuración técnica de seguridad para producción. Main y la release anterior permanecen sin modificar.
- La réplica privada de CloudTechNext se verificó contra una copia consistente de producción. La API y el worker tienen acceso temporal autorizado a GitHub; los tokens no se entregan a hooks, Terraform ni VMs. Los resolvers reales descargaron Governance y Market rc.2. La aceptación pendiente creará un AIDP con Governance y reutilizará esa plataforma para Market y GodEye; todavía no hay APPLY de estos candidatos. Falta seleccionar el Identity Domain del nuevo entorno.

- Kits: se retiró Autonomous, su paquete SQL sin consumidores y el bootstrap de wallet. Los 34 notebooks de los cinco kits no consumen Oracle JDBC/oracledb ni memoria de agentes; las tablas y el aislamiento efectivos pertenecen a Spark/Master Catalog. El formulario incorpora AIDP nuevo/existente y mantiene el límite fresh-only. Esta revisión pertenece al PR50; no cambia la release combinada v4.0.11 ni sus stacks existentes.

- Validación del corte sin Autonomous: Terraform validate y 14 planes simulados aprobados; 243 casos Python de hooks/Terraform/bootstrap aprobados y 27 casos del updater aprobados tras revisar sus mounts. El coordinador ejecutó el backend completo en Docker sin red con resultado correcto y 11 omisiones de herramientas nativas. Graphify actualizado; dos reglas Sentrux y gate aprobados, calidad 6691, cero ciclos y god files. La reutilización nativa de AIDP sigue pendiente de la prueba OCI autorizada; estas comprobaciones no certifican recursos remotos. El workflow de releases exige un commit alcanzable desde main, por lo que no se publicó una release candidata del PR50 ni se alteró ese gate.
