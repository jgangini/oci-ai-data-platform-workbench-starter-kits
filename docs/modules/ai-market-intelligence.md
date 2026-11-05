# AI Market Intelligence

[Documentación](../README.md) · [Operación](../operations.md) · [Contratos vigentes](../contracts.md)

AI Market Intelligence mantiene una única implementación para investigación bajo demanda: selecciona fuentes, recupera documentos y produce respuestas vinculadas a evidencia. Su ID es `ai_market_intelligence`. El instalador prepara un agente TEST pausado; instalarlo no acepta su calidad de investigación ni autoriza capturas pagadas.

## Fuentes y recursos

El [manifiesto AIDP](../../apps/backend/app/modules/ai_market_intelligence/aidp/manifest.json) declara fuentes, destinos y recursos. El código portable está bajo `aidp/workspaces/default/files/agents/ai_market_intelligence/`, con requisitos, prompts y módulos Python. El [empaquetador](../../apps/backend/app/modules/ai_market_intelligence/installation/agent_package.py) ensambla la entrada legible y registra sus dependencias; no descubre archivos arbitrarios ni incorpora secretos al código.

La raíz de publicación es `/Workspace/.artifact/ai_market_intelligence/`. Los descriptores de agente y AI Compute están en `aidp/workspaces/default/agent_flow/`. El manifiesto declara cero tablas propias y cero workflows Spark: esta implementación no despliega el modelo SQL retirado. Su grafo LangGraph describe razonamiento, no un job Spark. No se crean recursos ficticios para rellenar carpetas.

El alias local `default` se vincula al workspace seleccionado en el portal. `package/lab.json` conserva versión y hashes del paquete; cada asset tiene una sola fuente editable. El paquete continúa sin aceptación nativa completa.

<a id="installation-and-recovery"></a>
## Instalación y seguridad operativa

El [cliente de instalación](../../apps/backend/app/modules/ai_market_intelligence/client.py) conserva las fases `verify → credentials → compute → files → agent → permissions → test → ready`. Registra intenciones antes de escribir y reconcilia resultados inciertos por la operación existente. Preserva archivos, credenciales y despliegues ajenos; no adopta instalaciones por similitud de nombre.

La preparación del compute conserva su clave conocida mientras OCI publica un estado operativo. Los estados transitorios o todavía ausentes mantienen la instalación pendiente; los estados terminales se rechazan sin crear un reemplazo. La operación, el workspace, la identidad y la configuración del recurso se verifican antes de reutilizarlo. Si la respuesta de creación aún no contiene una clave, la recuperación conserva la misma operación y su token de reintento.

La API no admite selectores de implementación. GET solo consulta; POST exige administrador nativo verificado, operación UUID estable y configuración validada sin devolver tokens en errores. El instalador comprueba los metadatos de la credencial en AIDP Credential Store y la reutiliza sin rotación; la conexión con Apify requiere una prueba independiente. El token completo nunca llega al estado público ni se almacena en el navegador.

La configuración inicial mantiene `research_enabled=false`. La comprobación de setup exige revisión exacta, sesión vinculada y respuesta completa con cero llamadas al modelo y cero capturas Apify. Una instalación ready o un TEST ACTIVE no certifica investigación, MCP ni aceptación semántica. Una actualización de fuente no sustituye silenciosamente el candidato enviado.

Las investigaciones conservan IDs de operación, capturas, referencias, checkpoints y gasto conocido/desconocido. Un timeout no permite otra captura ciega. El historial de evidencias y las solicitudes de aclaración pertenecen a la misma investigación; no se reinicia una captura para ocultar un fallo de entrega.

## Contrato de streaming público

El agente transporta NDJSON en el contenido del asistente: protocolo `ai-market-intelligence`, versión `2.0`, tipos `event`, `response`, `snapshot` y `heartbeat` de transporte. IDs/secuencias estables, revisiones de bloque y claves de fuente permiten representación incremental y replay; el razonamiento privado no se publica.

El [decodificador SSE](../../apps/backend/app/modules/ai_market_intelligence/installation/stream.py) extrae paquetes públicos, elimina duplicados acumulados y rechaza identidades contradictorias o entregas incompletas. El [reductor de referencia](../../scripts/modules/ai_market_intelligence/market-stream-example.mjs) conserva enlaces/identidades, detecta saltos y restaura replay consistente. Una corrección fallida retira el bloque y sus gráficos provisionales; los artefactos finales reemplazan las vistas previas, incluso cuando la lista final está vacía.

El cliente debe representar Markdown con HTML deshabilitado, actualizar por identidad y conservar referencias. Heartbeat solo acredita conexión viva. Reconectar reutiliza solicitud autenticada y última secuencia; desconectar no cancela capturas. El reductor no implementa autenticación ni toda la interfaz de chat. Fuente: [protocolo](../../apps/backend/app/modules/ai_market_intelligence/aidp/workspaces/default/files/agents/ai_market_intelligence/ai_market_intelligence/wire.py).

<a id="isolated-native-test-operations"></a>
## Desarrollo TEST aislado

[`scripts/modules/ai_market_intelligence/market.py`](../../scripts/modules/ai_market_intelligence/market.py) administra exclusivamente `/Workspace/.artifact/ai_market_intelligence/agents/ai_market_intelligence` mediante el plugin AIDP instalado y transporte autenticado del proyecto. Verifica workspace activo y rechaza cambiar fuentes compartidas con PROD. No publica PROD ni crea tablas del motor retirado.

Usar configuración privada y entorno alineado con las versiones realmente observadas. `prepare` ensambla el candidato; `sync`, `test-init`/`test-refresh`, `new-session` e `invoke` ejecutan pasos seleccionados con `--apply` cuando corresponde. `status` observa, no certifica. Consultar `--help` del script para sus argumentos. Credenciales, corpus y recibos quedan fuera de Git.

Las invocaciones conservan intenciones inmutables, paquetes públicos, tiempos de llegada y resultado bajo `.aidp-local/market/evaluations/`, ignorado por Git. No se reutiliza una etiqueta de estudio existente. Resume/replay conserva el ID; una entrega fallida no autoriza otra captura pagada. El piloto acotado vincula preguntas, hashes corpus/oráculo, runtime y fuente; admitir un dry-run es distinto de completar un caso pagado.

## Agent Hub y aceptación

La tarjeta del paquete describe capacidades; no habilita conectores ni permisos. **Open Agent Hub** abre la ubicación general del tenant Workbench verificado. No registra, activa ni acredita un chat específico del agente. Copy Endpoint es una acción independiente de la revisión nativa instalada. Publicación, tarjeta, Registry, segundo usuario y conversación/streaming requieren checks propios.

Antes de aceptar una release, verificar instalación desde artefacto inmutable en el destino: ownership/recuperación, código y dependencias, permisos, servicios modelo/MCP, admisión de gasto, evidencia real, calidad del corpus y entrega al cliente. La instalación necesita activación autorizada y aceptación del workflow TEST activo; el instalador pausado no lo realiza.

Fixtures, razonamiento sobre documentos congelados, conectores reales, setup nativo, investigación nativa, evaluación semántica y frontend se reportan por separado. Una observación de otro tenant o una respuesta parcial no cierra aceptación del candidato actual. Seguir [Desarrollo](../development.md) y [CI](../../.github/workflows/ci.yml) para checks mantenidos.
