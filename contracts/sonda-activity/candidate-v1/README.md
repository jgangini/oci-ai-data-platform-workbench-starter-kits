# Activity v1 candidate 4

Unaccepted candidate for consumer integration. The 1.4.0 native component embeds
generated copies of these helpers, with Activity off by default. Installing the
code grants no scope or activation; rendered joint acceptance is still required.
Do not use its file hashes as an agreed production capability pin. Requirements remain in
[the canonical Sonda guide](../../../docs/modules/sonda-contract.md#live-chat-and-cross-application-activity-integration-requirements).

`reference.py` defines the closed shapes; `schema.json` is its JSON Schema export.
Run `python contracts/sonda-activity/candidate-v1/export.py --check` with the existing
backend environment. Valid fixtures are synthetic; `reject-*` must be rejected.
The Python codec additionally checks event byte limits, duplicate JSON keys,
same-run/attempt membership, required autonomous identities and completed-result references. Those semantic checks must
also be implemented by consumers; JSON Schema validation alone is insufficient.

This is the **broker read response**, not an OCI producer upload. The authenticated
broker supplies `received_at` and `ingestion_sequence`; native producers cannot
claim those values or choose their authority. Scope contains registry/generation;
the user binding is authenticated server state, never a browser field granting
access. Cursors are opaque and scope/run bound; their signatures are not defined
by this codec. Every events response, including an empty tail response, requires
`resume_cursor`: the broker-issued durable position after that batch. Consumers
retain it for future reads even when `next_cursor` is null. `next_cursor` is only
pagination through the currently available batch backlog; null means caught up,
not that the run completed. The broker must issue the checkpoint even before the
first event and must never derive it from client-supplied sequence values. Missing
or expired cursors require an explicit resync/error flow, not silently returning
an empty history. Signatures, expiry and retention remain broker acceptance gates.
`items` and `events` have at most 100 records; encoded batches have
at most 1 MiB and events 16 KiB. The broker must enforce cumulative 64 KiB public
text and 10000 events per run separately from this stateless codec.

`final_ref` is a broker-resolved result ID, hash and byte length, not an arbitrary
URL or OCI path. The broker must authorize resolution for the same run/scope,
verify the hash and enforce the 1 MiB maximum. Final contents need their separate
public result contract before any rendering. `text_delta` is provisional public
text only. The observer may map snake_case fields into its internal types.

Producer identity, effect authority, deduplication, attempt resumption, ordering,
terminal monotonicity, retention, pagination and SDK turn correlation remain
integration gates. This codec does not implement or prove any of them. The native
producer envelope and exact typed final result are still to be agreed with Huginn.

The [producer proposal](producer.md) records admission, stale-reference and
conditional-write races, native SDK evidence and remaining transport gates.
`public_stream.py` is an inactive candidate helper for one structured model
stream: it exposes only `answer`/`summary`, reports provisional-text degradation
and preserves the final dictionary for external validation. Its focused tests
do not establish journal or rendered-interface acceptance.
`producer.py` supplies closed admission/current/claim and private journal codecs;
`producer-schema.json` and `producer-*` fixtures export those models. Parsing
requires exact canonical bytes after model defaults and datetime normalization;
use `producer_encoded(model.model_dump(mode='json'))`, including its trailing
newline. JSON Schema alone does not establish that byte equality, admission
binding, CAS, authenticated producer role or result semantics. Completion uploads
use a private `execution_completion` receipt, never a canonical public terminal.
The candidate manifest also records the six Python codec/helper source hashes
for copy review; these remain drift checks, not accepted capability pins.

`journal.py` is an inactive single-owner adapter for conditional object writes.
It consumes an invocation-specific claim before model execution, checks ownership
before publishing each hash-linked batch, flushes the first public delta
immediately and batches subsequent deltas at a bounded interval. Its caller must
serialize writes and call `flush_due()` during SDK pauses. Delivery uncertainty
stops observation writes without retrying business work or inventing success.
Fake-adapter tests cover these transitions; they do not prove OCI CAS or UI timing.

`native_bundle.py` composes the canonical runtime sources into ordinary
namespaced Python. It removes only the reviewed sibling loaders, rejects unknown
imports/globals and preserves wire bytes. It performs no filesystem or network
access. Offline bundle checks cover collisions, admission and journal parity;
three/four-source callers remain supported and the optional fifth source is the
result codec. Native integration still needs separate acceptance.

`results.py` and `result-schema.json` define an inactive result envelope bound to
the authorized scope/run/attempt and exact hash/length reference. Chat and Gold
payloads require the caller's existing contextual semantic validator; the codec
does not replace citation, chart or committed-publication validation. Native
steps have closed work identities, counts and report references, and always
remain publication-pending. `no_change` carries no new outputs or report refs.
`result-native-step.json` is synthetic and proves no business publication.

`chat-admission-input.json` records the existing Core `ChatInput` validation and
canonical input hash, including UTF-8, decomposed Unicode and preserved ID-array
order. Consumers reuse that Core model and encoder, including the trailing LF;
they must not hash arbitrary raw JSON or normalize question text differently.

Each run describes one effective execution attempt; its `run_id` and `attempt_id`
are immutable and bound one-to-one by the server. A reconnect, replay, or read of
an already saved native result does not create an execution attempt or announce
a new model/tool start. Only an authorized effective re-execution creates a new
run/attempt. `replay-scenario.json` is a synthetic consumer scenario retaining the
same identities through reads, a caught-up tail and replay; it is not runtime proof.

`investigation_id` is the existing case's 64-hex identity, not a UUID. `work_id`
is the existing native UUIDv5 generated from the generation and immutable input
hash in `sonda_runner.create_case_work`. Autonomous runs require both, bound to
the server's CaseWork. One investigation may have multiple work items as its
configuration/state/input changes; identical input keeps the same work identity.
The observer must not create a new UUID mapping from the investigation hash.
Events responses require `attempt_id` even when empty, and events must match both
wrapper run and attempt. The authenticated reader must additionally compare the
persisted run descriptor, scope and user binding; codec equality is not permission.
These identity rules do not promise exactly-once model execution or billing.

Candidate.4 separates execution kind from origin. Native invocations require
their real agent UUID/name; provider and Gold runs have null agent fields and
an exact component/kind pair. A typed causal parent links another run/attempt
and its span/event. Core references and `execution_ref` come only from verified
receipts. They are not URLs, authentication or permission to read another run.
The broker may enrich only the read projection monotonically (one null-to-real
execution reference, unique appended core messages); admission descriptors and
identity/parent fields remain immutable. An unavailable parent stays pending
rather than creating a cross-run span edge or reopening a terminal parent.

Each tool start declares an immutable domain. Domain/role shape checks reject
an agent claiming a provider capture; the broker still validates the registered
span, authorized catalog and actual receipt before projecting a finish. Only
`activity_broker` projects canonical terminal events. Neutral stages carry
`status=progress|degraded|uncertain` (default progress); degraded/uncertain require
that neutral role and no business effect. Timeout/EOF are not confirmed failures.
`span-policy-scenarios.json` supplies state-policy cases for consumer tests;
passing the stateless codec does not establish that a reducer enforces them.
`validate_run_event` additionally binds a projected event to the caller's
already authorized run/attempt and its component/domain. A neutral broker can
project lifecycle stages and terminals; model-text deltas belong to native
invocations. This function does not resolve a missing span, authenticate an
upload, verify a provider receipt or grant access to the supplied run.
