# Native activity admission and journal proposal

Status: under review by the native runtime, Huginn broker and observer owners.
This is an extension of the [integration requirements](../../../docs/modules/sonda-contract.md#live-chat-and-cross-application-activity-integration-requirements),
not an accepted transport. Candidate.4 extends the read codec for joint review. No producer
or observation subscription is activated by this document.

## Fixed trust boundary

Huginn authenticates the user, authorizes the generation and persists a turn
descriptor before making exactly one native invocation. The observer only reads
authorized runs. It never admits work, consumes a claim or invokes an agent.

Object keys derive from the installed artifacts bucket and the existing
`operational_prefix(binding) + 'activity/v1/'`. An incoming reference is only a
canonical UUID. Clients and model output cannot choose a bucket, key prefix,
callback, URL, authority or artifact location. The existing shared OCI credential
is a trusted server boundary: a descriptor hash is integrity evidence, not a
separate identity or privilege boundary between holders of that credential.

## Admission binding and one-use claim

The immutable `admissions/{reference}/descriptor.json` contains:

- protocol version, reference, scope (`registry_id`, `generation_id`);
- immutable `run_id`, `request_id` (Huginn's server turn), `attempt_id`, `agent_id`
  and source (`huginn_chat` or `autonomous_study`);
- creation and admission-expiry timestamps, with a bounded lifetime;
- native-session digest and exact canonical input digest;
- for autonomous work, the existing work UUID, input digest and investigation
  hash, verified against the protected work projection;
- approved configuration and tool-catalog references used for interpretation.

The broker supplies the reference through
`metadata['sessionvariables.sondaActivityRef']`. The optional session variable is
declared with `shouldLog=false`, with no default. Native code reads it directly
from the authenticated invocation's `chat_context`; it does not add it to model
messages, durable graph state or the core ChatInput v2 payload.

The native thread ID comes from `flow_setup`, before applying the generation's
memory namespace. The runtime checks the descriptor against the actual agent,
generation, session and canonical input, not values asserted in the prompt.
Missing, malformed, expired, mismatched and unknown references are denied.

One-use alone does not prevent a retained, unclaimed reference being mistaken
for a later identical question in the same session. Maintain a current pointer
key derived from scope, agent and the native-session digest. Before each POST,
the broker CAS-publishes the new reference, epoch, run, attempt and admitted
status. It cannot replace a running or unresolved lease to start another POST.

Native admission follows this order:

1. Read the descriptor and current pointer. Validate reference, epoch, scope,
   agent, session, request digest and admission deadline. Retain the pointer ETag.
2. Generate a fresh invocation-local `owner_nonce`. Create the immutable
   admission claim with `if-none-match:*`; an existing claim rejects another
   invocation, even when all descriptor fields match.
3. CAS the current pointer from admitted to running with the retained ETag,
   reference, epoch and owner nonce. If another admission won, fail before the
   model or tools run. Do not reuse or rearm the consumed claim.
4. An uncertain write can be reconciled only by this invocation's exact bytes
   and fresh owner nonce. A different invocation cannot adopt a matching claim.

A crash before step 3 burns the admission without starting work. A crash or
timeout after running is uncertain until reconciled; expiry does not free it.
Broker reconnection, F5 and final-result reads never repeat these steps. A new
execution attempt needs explicit reconciliation and a new immutable admission.

## Producer events and broker projection

Native upload events contain only the admitted identities, `event_id`,
`producer_stream_id`, `producer_sequence`, `span_id`, nullable parent/causation,
`emitted_at`, type and its allowed public payload. They omit `authority`,
`received_at`, `ingestion_sequence` and replay cursor; the broker assigns these
after authenticating and validating the admission and producer role.

Allowed types remain stage, tool_start, tool_finish, tool_error, text_delta and
run_terminal. No graph state, prompts, reasoning, SQL, connector arguments,
credential material or arbitrary exception text is uploaded. Limits match the
read candidate: 16 KiB per event, at most 100 events / 1 MiB per batch, 64 KiB
public text per run and 10,000 events per producer stream.

Journal batches and their index entries are immutable. Conditional head writes
publish a contiguous sequence of hash- and length-checked entries. A producer
reconciles a lost acknowledgement against exact bytes; it never repeats a model,
tool or data write to reconstruct telemetry. Orphaned unreferenced objects do not
become visible events. The broker deduplicates event IDs and rejects conflicting
sequence reuse. Its ingestion order and opaque replay cursor are independent
of producer order and wall clocks.

Within `admissions/{admission_id}/streams/{producer_stream_id}/`, batch keys are
`batches/{batch_id}-{sha256}.json` and index keys are
`indexes/{index_sequence:020d}-{sha256}.json`. The sequence is a zero-padded
20-digit decimal integer. Starting from `head.json`, a reader can GET the last
index by sequence/hash, then decrement the sequence and use
`previous_index_sha256` to recover its predecessor without listing objects or
knowing an earlier UUID. `index_id` remains an immutable logical identity checked
against the head; it is not part of the object key. Every read verifies the hash,
canonical bytes, exact scope/run/attempt/stream and contiguous sequence before
exposing records. These paths are fixed server rules, never client-supplied URLs.
For a native admission, `producer_stream_id` equals the admitted run's
`attempt_id`. The writer and admission-binding validator enforce that equality.
The broker therefore knows the stream key before POST, directly from the
authorized descriptor, without listing or waiting for the final response.
This rule concerns private native uploads; the broker's own projected terminal
events need not impersonate that producer stream.

The following policy needs joint agreement before changing the read schema:

| Operation domain | Who may establish its effect | Evidence |
| --- | --- | --- |
| research_preparation | native_agent: prepared | approved tool catalog and validated case request |
| query_read | native_agent: executed | actual completed read and bounded result summary |
| provider_capture | provider_broker: executed | provider execution and budget ledger |
| publication | gold_writer: committed | committed publication receipt |
| turn lifecycle | neutral activity broker | authenticated durable admission / failure record |

A finish resolves against its registered start/span and exact catalog entry.
Names alone, model arguments and self-asserted authority do not establish a
domain. Candidate.4 adds the explicit start domain and neutral broker role.
Degraded/uncertain observation states are neutral stages, without a business
effect. Only the broker projects a canonical terminal from a validated result or
confirmed native failure. Timeout, EOF, expired lease and telemetry loss do not
establish a terminal. Activity disabled preserves legacy behavior; enabled
requires valid admission without a silent legacy fallback.

Native invocation, provider execution and Gold publication use separate runs
and spans, with a typed causal parent and verified core references. Source is
the originating chat/study, while component and execution kind identify the
actual executor. Non-native runs do not invent a native agent UUID. Receipt-only
references can enrich a broker read projection monotonically; they never mutate
an admitted descriptor or reopen its terminal. Missing parents remain unresolved
references, with authorization checked before resolving another run.

## Public text, final results and latency

The native public-answer node consumes the same structured model run with
`.astream()`, extracts only `answer` (chat) or `summary` (study report), and emits
append-only deltas while generation continues. This does not stream evaluation
reasons, planning JSON or tool-call arguments. No second model call or completed
answer chunking is allowed. Rewritten partial text or failed delivery degrades
the provisional stream; the authoritative validated final can replace it.

The final chat artifact reuses `sonda_chat.public_response`, including validated
sources and charts. The autonomous public artifact must reuse the existing
validated CoreReport to SondaFeedback projection: title, summary, business
status/revision, investigation and criterion references, indicators, evidence
references and coverage limitations. A finished invocation or evaluated batch
must not imply a concluded investigation. Waiting for evidence, a prepared
request, a limited report and a completed investigation must stay distinguishable.
A complete report exceeding the 1 MiB read limit must remain intact; a bounded
projection declares omitted counts and retains its authorized opaque report ID.
Its envelope and resolver still require integration acceptance. A worker's
`committed` result establishes its saved work artifact, not Gold publication.
`final_ref` resolves only a
fixed private result ID, exact scope/run/attempt, hash, bytes and validated type.
The inactive `results.py` codec and exported result schema enforce that envelope
and require existing contextual chat/Gold validators. Integrity alone cannot
approve a payload. A `native_step` uses `evaluated` without report references,
`report_prepared` with them, or `no_change` without new outputs. All retain
`publication_status=pending`. Its work
input hash must match the caller's already resolved protected work record.
The optional native bundle includes the same codec. No consumer path is accepted
or activated merely by generating or importing it.

The broker consumes the journal while the original invocation is in flight and
pushes public deltas to Huginn SSE. The observer consumes authorized reads of
that same run. Flush during generation is required. Poll and flush cadence must
be measured across producer, broker and rendered UI; a one-second reader can
miss an entire short native generation window. Cadence is not an accepted SLA.

## Evidence and remaining acceptance

An isolated native SDK test on the current model produced 53 append-only public
snapshots in one model call: first at 4033.200 ms and stream completion at
4916.550 ms. The final structured response was valid. The declared no-log
metadata reference and authenticated native session matched. It read no user
data and performed no provider capture or SQL. Its temporary agent and files
were removed. These observations verify SDK capability, not journal, UI or
cross-turn acceptance. No model change is currently justified for streaming.

A second isolated native test exercised the conditional OCI journal with one
synthetic model stream and no SQL, captures or user-memory calls. First public
text was emitted at 5253.138 ms, persisted at 5347.430 ms, and the structured SDK
stream completed at 6171.927 ms; its private completion persisted at 6285.104 ms.
An independent reader observed public text while the invocation was in flight,
then replayed seven indexes and 103 events using only GET, checking hashes,
scope, attempt and sequence continuity. This proves that isolated native-to-OCI
path, not browser delivery, cross-user authorization or concurrent CAS races.
Its result contained only synthetic text and was not a validated business final.
The temporary agent, workspace files and exact diagnostic objects were removed
after verification; the installed agents and production fence stayed unchanged.

Acceptance still requires native chat and autonomous runs shown in both actual
interfaces; first persisted delta and tool start visible before terminal/finish;
failure after partial output; F5 and reconnect without another invocation;
same-session repeated and omitted/stale references; concurrent admission CAS;
cross-user/scope/run/attempt denial; duplicate and conflicting producer events;
and final artifacts validated before charts or success are displayed.
