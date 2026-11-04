# AI Market Intelligence v2

[Current module and legacy behavior](ai-market-intelligence.md)

V2 is an implementation in progress, separate from the legacy research graph.
The checks below do not establish complete connector, Agent Hub or corpus
acceptance.

## Implemented slice

The company comes from the current request or its explicit conversational scope,
not an embedded corpus brand. An explicit company change supersedes the earlier
subject. Search country and language are operator defaults, not evidence of a
company's location or coverage. Evaluation fixtures and company-specific corpora
remain separate from production rules.

The intake objective is a derived summary of that request. Its model contract
preserves the requested objects, alternatives and explicit restrictions; it must
not infer a narrower universe from a commercial relationship or an unverified
premise. Context may resolve an omitted reference without replacing the current
request. The original question continues into planning, analysis, drafting and
review alongside the brief. These instructions and bounded model comparisons do
not establish universal semantic compliance or make an attributed partial answer
a complete resolution.

Source selection ranks promising reading destinations among admitted search
candidates. A product-specific page may warrant reading even when its preview
does not identify the manufacturer; the later analysis must establish that
relationship from captured content. Name-only matches to a place or unrelated
brand do not justify reading. Selection retains the global ten-destination
budget and may return no candidates; this guidance does not guarantee retrieval
or a useful answer.
When exact product pages are available, selection gives them priority over a
sibling product or a general catalog. A manufacturer hint for another product
does not establish a shared manufacturer. Broad requests covering any product
of a brand can still use those categories; the rule follows the request scope.

For product questions, planning keeps the product and its explicitly supplied
retailer/channel together in the emitted search terms. Manufacturer discovery
also expresses the requested manufacturing relation in the initial query, with
short alternatives for that relation and the material market held in common.
Known identity anchors remain required; proposed producer names are hypotheses.
A product page need not contain those relation words to warrant selection.
The purpose retains the full research question and unverified hypotheses.
This planning guidance does not make a returned candidate or snippet evidence.
The permitted refinement follows the same anchor priority. Entity and material
market are joint conditions; OR alternatives represent variants of one concept.

Follow-ups resolve an owned report by the requested company and scope, allowing
unambiguous name variants. The default report is not an exhaustive history.
Only the reuse route loads up to eight previous finished request candidates;
summaries and proposals may consume that bounded legacy window. No matching
report produces a clarification without recapturing. Source ownership/session
checks precede selection. A rejected summary publishes no substitute report,
references or artifacts; the original remains in the conversation. Coverage
identifies `reused_request_id`, `transformation_verified` and `no_new_capture`.
After a verified transformation, its selected research report becomes the next
implicit follow-up source; the pointer never advances to the shortened summary.
Historical public gaps are scoped again when adapting an earlier report.
Summary generation preserves the requested presentation: a requested number of
points becomes separate Markdown list elements; a paragraph request stays a
paragraph. Summary review checks that transformation as well as factual support.
A format rejection cites the literal question with `evidence_id=question`;
documentary objections retain their source/report attribution. A faithful paragraph
does not satisfy a request for three points. These checks remain bounded model
decisions and do not establish universal format adherence.

- `v2_contracts.py` defines bounded domain requests, evidence, public blocks,
  artifacts and versioned events. Model proposals grant no external authority.
  Calendar-only intake boundaries become inclusive UTC days in code; timestamps
  retain their instant and normalize to UTC. Invalid dates or timestamps without
  a timezone remain rejected. This avoids another model call just to format dates.
- `v2_workflow.py` prepares one plan, processes source routes serially, publishes
  narrative blocks only after factual review and allows at most one justified
  refinement per source; web permits it after the current compatible candidates
  are exhausted, while a global destination remains and without an explicit URL.
  A financial attempt retains its slot but does not block web's first read.
  Failed reads consume their slots; pending and uncertain reads must reconcile
  before another batch. Persistent gaps and domains without a verified finding
  produce a partial result. The final response and terminal event commit together.
  Web reads Google's compatible candidates in batches of up to two, sharing at
  most ten distinct web/PDF destinations across the request. Each terminal batch
  is analyzed and reviewed before the durable cursor can advance. Sufficient
  evidence stops acquisition early. Selection, original SERP and per-batch receipts
  survive recovery. Effective destinations of permitted redirects are excluded
  from later selection. Explicit web URLs also use batches of two. Web does not
  automatically follow page links. Failed pages retain their slots and limitations.
  Previews do not become page facts. Financial-document discovery keeps its
  source-specific ranking and can reserve one slot for an observed PDF link. An unsupported
  query refinement closes that source as partial and preserves its verified
  blocks; permission, receipt and destination failures still fail closed.
  New plan/refinement model contracts propose keyword concept groups. The
  connector compiler joins alternatives with OR where
  supported and requires the concepts jointly. YouTube uses one alternative per
  concept without claiming Boolean search. Google documentary keywords remain
  unquoted to permit search-engine spelling correction; X and YouTube retain
  their literal-term conventions. Dates and social destinations stay
  controller-owned. Canonical state retains the compiled query for replay;
  earlier recorded string queries remain compatible with the existing compiler
  guards. Keyword matching does not establish relevance or factual support.
  A general social mention search uses the entity and unambiguous aliases;
  output formats, task words and the market are not mandatory lexical filters.
  Specific requested topics can still supply additional keyword concepts.
  Intake uses the known conversation topic for abbreviated questions and selects
  the necessary sources. Financial-document tasks run before auxiliary web tasks
  in financial mode so they can use the shared document allowance. Empty financial
  collections prohibit findings/facts without sending an unused empty enum that
  the OCI structured-output endpoint rejects.
  An implicit recurring service complaint can request attributed historical
  experiences through web and X, without treating its premise as established or
  imposing a recent-conversation window. Neutral operations and explicit
  source restrictions keep their narrower routes. A clarification about an
  unidentified offer can distinguish prepaid credit from a free prize
  conditionally; it does not establish legitimacy, fraud or an official promotion.
  General valuation methodology searches use the standard financial concept;
  the companies and intended application remain in the task's purpose.
  For an explanation of an effect, analysis extracts the documented links from
  the starting variable through relevant intermediate steps to the requested
  result, preserving conditional application and unsupported gaps. A list of
  definitions alone does not replace that mechanism.
  Drafting and verification receive actual inventory counts. Verification sees
  cited documents first, then other recovered documents within the context bound;
  uncited text cannot justify a wrong citation. Inventory counts distinguish
  observations from distinct URLs and do not establish independent sources.
  Free-form analysis gaps remain private diagnostics. Public final coverage uses
  a code-owned notice limited to the recovered evidence; context omission counts,
  collection limits and reviewed findings remain visible. This applies to new
  responses and their persisted events/replay, without rewriting old reports.
  A rejected narrative draft can use the existing single repair to propose up
  to four independent units. A grouped review must return one explicit verdict
  per unit; omitted, duplicated or unknown verdicts fail closed. Only approved
  units enter the public block, and discarding any unit leaves the source partial.
  Financial documents and structured series retain their specialized repair.
  Nonfinancial HTML that fits completely in two paragraph-aligned extracts of
  at most 12000 characters each and 60000 UTF-8 bytes retains the entire admitted
  body without a lossy outline decision. Each split keeps the full-body hash and
  character offsets, even when no content is omitted. Prior provider truncation
  remains visible. Oversized or unsplittable text, PDFs and financial documents
  retain the existing selection and qualifier guards; context/page limits do not
  increase. Complete projection refers to admitted text, not all site content.
  Financial facts distinguish exact numeric amounts from `reported_text`:
  literal qualified amounts, bounds, approximations and ranges remain attributed
  text with their source excerpt. They never become numeric points, graphs or
  computed changes. Exact amounts retain the strict sign, token and scale guards.
  The server expands a reported-text anchor to its complete contiguous source
  paragraph, preserving internal line breaks and qualifiers; over 1000 characters
  is rejected rather than truncated. Exact amounts retain their 50-character bound.
  The grouped context counts unit text before selecting evidence and refuses
  review if a cited document was omitted. Repair adds no search or new loop.
- Recurring proposals use one separate bounded extraction decision inside the
  existing proposal node. They retain the requested subject, objective and literal
  cadence; unprovided timezone and destination stay null. Missing subject,
  objective or cadence produces a partial proposal. The original request is
  retained, sources must be enabled, and approval/task/notification authority
  remains code-owned and disabled. This adds no autonomous scheduler or agent.
  A proposal-only intake clarification does not block that draft: missing
  scheduling details remain visible as pending fields, without execution.
- `v2_state.py` stores compact owner/candidate-bound request snapshots and public
  event replay. It does not construct `Records`, the fourteen-table framework or
  a SQL projection. Conditional writes, publication identity and cancellation
  protect retries and recovery. Effect admission remains a required injected
  boundary; a new request snapshot does not authorize a paid start.
  Report selection is an additive private request field. Existing snapshots
  without it remain compatible; recovery preserves a selected ID rather than
  choosing another report. No SQL migration or business table is required.
  A failed request releases its native session only when every effect is already
  completed or rejected. Uncertain model/capture effects continue to hold it.
- `v2_effects.py` records conditional model/Apify admission, returned receipts,
  cached responses and unresolved submissions. Page/document destinations have
  one conditional ten-URL allowance for the request, including retries and
  other document routes. Saved selection and receipts resume existing reads
  without consuming a new slot. Pending/uncertain attempts retain their slots. An uncertain start is not repeated
  when its run ID was lost. Request and receipt objects have separate v2 paths;
  they do not overwrite legacy state or another request. Intake time is frozen
  for the request, including after a restart.
- `v2_collector.py` connects the existing provider and pinned compilers. It
  separates SERP discovery from public-page/social reads and admits discovered
  URLs through recorded search results or one literal PDF link from a completed
  page read. Derived document receipts retain the parent evidence, receipt,
  text hash and observed link. One hop consumes the same ten-destination limit;
  PDF content cannot authorize another hop. A model proposal cannot approve its
  own destination. Polling is bounded; pending runs keep their identity.
  Passage selection receives the durable active task and its current refined
  query as well as the overall question. It evaluates the local document purpose:
  a method can be relevant without naming the investigated companies. An example
  about another company supplies no company-specific figures for the request.
  The selected spans retain their original offsets, page and document hashes;
  this context change grants no additional destination or passage allowance.
  Known start rejections retain only a safe classification and non-sensitive
  field name, without rejected values or echoed schemas. Ambiguous starts remain
  uncertain and are not retried.
  If a verified `SUCCEEDED` run returns a dataset rejected by the source
  normalizer with `ValueError`, its effect completes with `invalid_dataset`,
  preserving the receipt and cost. The source reports unavailability with zero
  evidence; this is not a valid empty search. Cache/recovery preserves that
  classification without another start or read. Receipt, transport, cancellation
  and cost-verification failures retain their existing pending/uncertain guards.
  Status reads preserve tool exceptions and invalid identity/status validation
  errors. Only completion timestamps and cost verification for an already bound
  receipt become pending accounting, including usage metadata bound to a
  different run or multiple runs. Conflicting MCP metadata aliases remain a
  protocol validation error. None of these errors authorizes another start.
- `actor_tools.py` is the executable application-tool catalogue consumed by both
  planning and execution. Each named tool has a fixed Actor profile, strict
  argument schema, work limit and result kind. The shared MCP's start/status/
  dataset operations stay private transport; neither the planner nor a tool's
  arguments can select arbitrary Actors, builds, credentials or billing options.
- `v2_artifacts.py` creates presentation datasets with source references, units
  and null values. Conflicting financial points are not silently merged.
- Shared primitives in `runtime_core.py`, shared Actor profiles, financial/SERP
  parsers and the existing MCP provider can be imported without the legacy
  business schema. The legacy package retains its contracts and tests.
  A dataset JSON payload plus its matching MCP summary is decoded as one dataset;
  errors, unrelated notices and competing payloads remain rejected. Unusable
  organic URLs are omitted with their positions recorded, without visiting them
  or discarding compatible candidates. Query/page identity and result limits
  remain required; omitted destinations do not authorize extra reads.

The collector interface receives the stable operation key, authenticated request,
task, scope and previous operation identity. Local tests exercise both the graph
with a recorded collector and the new collector with recorded provider responses.
The native adapter can enable research with an explicit protected service
binding and an exact observed dependency profile. It requires the authenticated
AIDP subject, session and trace, preserves the native tool context and isolates
request/effect objects by owner and native session. Explicit financial selections
are validated before admission; a model cannot invent an approved ticker or
indicator. Deployment and semantic acceptance remain separate gates.

### Actor-specific capabilities

| Application tool | Reviewed mechanism | Interpreted result |
| --- | --- | --- |
| `web_search_serp` | One localized Google query, up to10 candidates | Titles, snippets and candidate URLs |
| `website` | One depth-zero adaptive-browser batch of up to2 selected relevant Google candidates | Captured page text and page failures |
| `x_search` | Conversation query with controller-owned dates, or bounded documentary keyword references | Public post identity, text and publication time |
| `youtube_search` | One phrase, configured weekly filter, no paid AI/video add-ons | Video title and description metadata |
| `facebook_search` | Official Facebook Search Scraper, `searchType=posts` | Keyword-post sample; dates filtered locally |
| `instagram_search` | Official Instagram Search Scraper, live popular-reel search | Caption sample; original owner and dates, no video analysis |
| `facebook_posts` / `instagram_posts` | Explicit, approved public destinations | Destination-bound posts and collaboration attribution |
| `yahoo_history` | Approved tickers and dates | Daily market observations, units and missing values |
| `world_bank` | Approved countries, indicator codes and years | Annual series and source/update metadata |

The social search tools are distinct from profile readers. Facebook's returned
`user` and `collaborators` do not establish that every mentioned entity authored
the post. Instagram mentions do not transfer ownership. Popular results can be
old; filtering them out does not prove the absence of recent platform activity.
Facebook search admits reviewed post, reel, group-permalink and `permalink.php`
identities through its own parser, without broadening explicit profile readers.
Posts contribute their retrieved text and media references. Valid identities
without text/caption are retained when media is available, explicitly marked
`content_basis=media_metadata`; their contents are not inferred. Rows without
either text or media are omitted with a visible count and partial coverage.
A query-bound Instagram `no_items` marker completes the known capture with empty
data; other provider errors and mismatched markers remain rejected.
The general `apify/instagram-scraper` search candidate is not active: the reviewed
probe returned unrelated hashtag metadata instead of the requested posts.

Queries retain the requested company, explicit subject/object, relation and
concrete actions, product or metric. Staff-duty queries keep the employee context
and requested functions alongside the company; cleaning or checkout words alone
can retrieve consumer products or payment systems. Product questions retain their
commercial object without introducing employees. Queries normally use one or two necessary keyword
concepts; a third requires a material period, market or other constraint. OR
alternatives express variants of the same concept, without mixing an entity
with a relationship or broadening a distinctive phrase to an ambiguous word.
A methodological search uses a standard concept, rather than all clauses of
the causal question. Company-performance discovery need not require both the
company and its holding in every query.
Web discovery can reformulate once after the current suitable candidates are
exhausted, while a destination remains and the user supplied no explicit URL.
Refinement recovers omitted material context rather than repeating the same query;
an identical query reuses its recorded result. Two searches without suitable
candidates close the source. Failed reads retain their slots; additional distinct
candidates can use the remaining global ten-destination allowance.
Possible manufacturers named in a question remain hypotheses for later checking;
they are not mandatory search terms. A reformulation retains the principal
company/product and can remove an unverified candidate to discover the actual
relationship. Finding another supplier does not refute the proposed candidates.
When a web task still awaits its first read, a financial refinement cannot
consume the last destination. The same reservation applies before a new automatic
PDF hop from a financial landing page. Already admitted PDF operations remain
recoverable with their original state and receipt; the guard only prevents new
reads. Financial discovery and its single PDF hop retain their own traversal
bounds; the shared destination total remains ten across the request and recovery.
Financial discovery does not require an annual-report format unless requested;
an official-source requirement prefers the issuer or regulator over mirrors.
A discovered result explicitly marked `[PDF]` can use the existing safe PDF
reader even when its URL is a download endpoint without a `.pdf` suffix. The
marker is only a routing hint tied to that selected URL. The reader still
validates the public destination, redirects, response type, PDF bytes and size;
HTML cannot become PDF evidence. Other candidates do not transfer their format
to the selected destination, and replay reuses the admitted operation.

An explanation of a valuation mechanism does not itself request a market quote.
For named real companies, unrestricted requests use financial discovery for
company context and web discovery for methodology, with one initial destination
reserved for each need. A hypothetical example can use methodology alone.
Methodological queries need not contain company names or a geographic market
unless jurisdiction matters. Company identity, ownership and financial figures
still require their own evidence; a methodology or an index of reports cannot
prove them. Conditional interpretations must identify unverified premises and
cite the applicable method. Requested numerical comparisons still use financial
documents and require their periods, units and comparable values.

Unrestricted requests for others' experiences can use web plus X documentary
references. The controller selects `Top`, at most20 posts and no lower date bound
for this mode; future posts are rejected. It is a historical keyword sample,
not a representative population or proof of authenticity. Explicit source and
publication-period restrictions remain binding. Conversation searches retain
their existing date window.

The brief may carry `search_country_code`, a two-letter uppercase Google region
for the resolved market. Missing or null values preserve the service default;
they cannot enable an unconfigured search capability. This optional JSON field
requires no SQL migration and old briefs remain readable. The country changes
discovery ranking without establishing a source's geography or coverage. Web
keywords also retain the explicit market where it delimits the question.

V2 selects Website Content Crawler's `playwright:adaptive` renderer with one
concurrent request, depth zero and batches of two within the ten-destination allowance.
Linked-file downloads and screenshots remain disabled. JavaScript rendering
does not guarantee access: short challenge pages with a recognized verification
heading and proof-of-work text are reported as access failures, not evidence.
An inaccessible destination consumes its admitted place; it does not authorize
a replacement URL or an additional page.

Observed YouTube channel names and URLs survive normalization as `author_name`
and `author_url`. They identify the observed publisher without establishing an
official corporate relationship. Drafting, factual review and consolidation all
receive the same admitted brief: its dates describe the requested scope, while
publication dates and captured text establish the source's actual contents.
YouTube also retains literal `source_title` and `source_description` fields;
the compatible concatenated `text` does not define their boundaries. Older
evidence without those fields remains readable, without reconstructing metadata.
Findings keep atomic source associations; grouping documents under a topic does
not transfer details between them. Factual review distinguishes a changed fact,
condition or attribution from harmless paraphrase or editorial wording. This
contract still requires real-model and native acceptance evidence.

### Publication and media links

Each public `references[]` entry binds `evidence_id`, `source`, the publication
`url`, observed/publication times and up to eight `media` assets. Assets contain
`kind` (`image`/`video`), `url`, `delivery` (`direct`/`page`) and `role`
(`attachment`/`thumbnail`/`preview`). Draft events, final responses, replay and
report follow-ups use the same evidence-bound manifest. Only cited evidence is
delivered. Clients render links from this manifest, not destinations invented
in model text. The example stream consumer validates these fields and retains
them; a client should display media only for its visible blocks/final references,
not every historical reference cached during replay.

Facebook uses returned native HD/SD video URLs and image metadata; Instagram
uses returned video/display/carousel URLs; X uses typed attachments and MP4
variants when present. YouTube supplies its watch-page link plus thumbnail,
without claiming a downloadable video file. Website media is limited to Open
Graph metadata already returned by the admitted pages. Search snippets do
not supply page media. Missing assets stay missing; there are no extra captures,
page reads, media downloads, OCR, visual analysis or transcription for this step.

Signed media query strings are preserved exactly. Public HTTPS validation,
deduplication and per-publication bounds apply; rejected/excess candidates have
`media_omitted_count`. `media_content_analyzed=false` is explicit. CDN URLs can
expire; the publication permalink and capture time remain available. Media URLs
in the structured media fields are removed before context budgeting and inference; models receive only
counts/kinds and cite evidence IDs. Facebook datasets are read in small pages
because native video metadata can exceed MCP response limits; this reads the
same completed capture and retains overflow detection.

Input work limits remain separate from billing caps. Unlimited Instagram and
World Bank calls omit the billing-item option after independent minimum-charge
rejections; their own input limits and parser bounds remain enforced. Multiple
profile destinations share the total post allowance. This does not remove the
operation ledger or make an uncertain start safe to repeat.

Actor start and running receipts establish identity before accounting settles.
They do not assert a final cost. Completed captures require a consistent,
settled receipt before admitting observations. An inconsistent known receipt
remains resumable under its existing Run ID; it must not become a failed request
that permits another capture. Malformed identities and settled-cost conflicts
remain rejected, and unknown expenditure remains unknown.

A terminal ScraperLink run can contain an error instead of a SERP page. Only
the observed single-row shape with an exact keyword echo, integer page one and
bounded nonempty error text is treated as technical unavailability. The known
terminal receipt, cost and `provider_result_error` remain durable; cached recovery
returns the same unavailable condition without another start, status read or
destination selection. The error is not evidence of an absence of public results.
Foreign queries, mixed result/error pages and malformed shapes still fail
validation.

`v2_documents.py` reads public HTTPS PDFs after validating every DNS address and
pinning the connection while retaining verified TLS SNI. Redirects are rechecked;
download, page, text and per-stream decoding limits apply. Extracted pages retain
their locator and document hash. This is text extraction, without OCR or a claim
that complex tables were interpreted completely. Per-stream limits are not a
global process memory/CPU sandbox. Document receipts survive retries, and a
cancelled request cannot admit the next document in a batch.

A direct PDF and a PDF linked from an admitted page use the same failure rule.
A definite format, size, validation or HTTP-status rejection is persisted and
consumes its destination without being retried. Other admitted pages remain in
the collection. Timeouts, cancellation and unexpected failures retain uncertainty;
they do not authorize another download. This distinction applies to public
document reads, without changing paid Actor or model receipt reconciliation.
Public PDFs that open with an empty password are readable within the same
bounds, including when their encryption flag remains set after opening. No
nonempty password is attempted. Password-protected files remain unsupported;
there is no password search, new crypto dependency or removal of extraction limits.

V2 retains at most 600,000 UTF-8 bytes of extracted text per captured HTML
document or PDF, separately from the model context. PDFs still have the existing
download, first-50-page and decoding bounds; text outside those limits remains
unread. Legacy website normalization retains its previous prefix policy.
`v2_passages.py` provides a bounded outline for long documents. One recorded
selection chooses up to two enumerated passages per document according to the
current question. Each passage is a contiguous source slice of at most 12,000
characters. Unknown, duplicate or excessive IDs fail before publication; an
empty selection does not manufacture a relevant fragment. Short documents keep
their existing path. The model receives selected text rather than the full
retained source, and the normal 90/100 KB context limits continue to apply.
Narrative passages preserve whole paragraphs. Financial passages require whole
heading sections or pages; oversized sections are omitted rather than splitting
a qualifier, scale or table header from its amount. Omitted blocks remain a
coverage limitation. Identical HTML text at different offsets has distinct
evidence identities, so repeated desktop/mobile content cannot overwrite a
previous fragment during the collection commit.

Passage locators include character offsets and a SHA-256 of the admitted page
text. This text hash is distinct from the binary PDF `document_sha256`, which
remains unchanged. Coverage explicitly reports selection and omitted content.
Projection can read an existing dataset without starting an Actor, but an old
snapshot containing only a prefix cannot recover its discarded text by itself.
The outline is capped and may omit candidates; selection is not a claim that
the entire document was analyzed. It grants no new URL authority or page budget.

`v2_financial.py` converts literal quoted amounts with an explicit decimal
separator and scale. Extraction selects an included page ID and a complete
numeric literal. Code attaches a bounded contiguous excerpt from that page
instead of asking the model to reconstruct the quote. It normalizes a literal
percentage suffix only for percent facts, preserving its sign and spacing:
`114 %` becomes 114 percentage points with scale 1, never 1.14. A monetary symbol prefix
is accepted only when that complete literal occurs on the selected page; the
stored amount becomes its numeric token without inferring a currency. Signs and
accounting parentheses cannot be lost across the symbol. Substrings of larger
numbers or signed amounts cannot serve as the amount. This proves literal
presence, not the meaning of a column. The factual review also checks entity, reporting basis,
period, currency, unit and column assignment before a series becomes public.
Document extraction classifies units as monetary amount, count, percentage or
ratio. Code applies the source scale to base units and constructs the chart's
unit label; an amount multiplied by a million cannot retain a millions label.
An unspecified currency stays unspecified. Both the first draft and its bounded
correction receive the calculated series and variations, before public review.
Verified artifacts can appear provisionally and are finalised at consolidation.
Saved-report follow-ups reuse the same conversation's evidence and artifacts;
they review the transformation against the saved report without recapturing.
Recurring-research proposals do not create jobs or send notifications.

The graph permits all eight source routes within an explicit 144-step limit. Each
source has one initial query and at most one justified refinement. Identical
replayed blocks keep their revision. Final output preserves provider/evidence
limitations and complete verified blocks when a rejected synthesis cannot fit
the public terminal text. Aggregate artifacts/events can reference up to 800
observations; progressive acquisition does not enlarge that output contract.
individual prose blocks remain limited to fifty citations.

Analysis prioritizes the latest web batch when the existing 90 KB context bound
requires selecting observations. Omitted coverage stays visible; earlier evidence
and verified blocks remain durable. Final coverage reports attempted, retrieved
and unretrieved destinations separately. Exhausting the available readings without
support is scoped to the consulted sources, not proof that no public answer exists.
Useful partial findings and task sufficiency are separate decisions. `gap` retains
material unanswered aspects of the original request and the current task even when
findings are publishable. `refinement_query=null` does not clear that gap: the next
read can use the existing SERP. Accessory limitations and unrequested conditions do
not require additional readings.

A rejected final synthesis cannot automatically restore earlier drafts. The
graph reviews each preserved block against the documented objections; those
objections are feedback, not factual evidence. A rejected block uses its existing
repair allowance and a corrected event supersedes it only after explicit unit
approval. Otherwise it is visibly withdrawn. The repair reviewer receives the
question, rejected text and objections within the same bounded evidence context.
Consolidation then recomputes references and final artifacts from the surviving
blocks, so retired claims cannot return through fallback or replay.

Each real evidence inventory of a source permits one factual draft repair, with
at most two repairs per source across its bounded collections. Rewording a draft
or changing which citations it selects cannot acquire a new repair: the binding
uses all admitted evidence IDs of that source. A legacy source-only reservation
does not acquire another allowance. When new evidence cannot produce an accepted
block, an earlier block is visibly withdrawn from the consolidated answer;
its provisional artifacts are excluded from the final artifact set.

The analysis context records how many complete documents were included or
omitted. An omission is persisted in the domain result, exposed with the draft
and forces a partial final response. The combined PDF limit also reports the
number of omitted pages. Valid financial series are not silently capped at ten
charts. A research scope may identify a public account without inventing a
geographic market; material ambiguity still requires clarification.

An unsupported narrative gets at most one correction over the same evidence
per domain and request. The corrected text is reviewed again; rejection closes
that domain with a visible limitation. This does not admit another capture or
repeat corrections indefinitely. A successful transport alone is not useful-answer
acceptance.

An explicit structured financial selection requests new research. Comparing
periods or requesting a chart does not by itself mean reusing a previous report.
Followups retain the original report as their canonical source, including after
multiple summaries. Their reviewer receives the actual cited document text;
membership in the report's citation list alone does not establish support.

## Facebook and Instagram

The shared compilers/provider now support `facebook_posts` and `instagram_posts`
for explicitly authorized public destinations. The pinned Actor builds were
checked from Apify's public metadata. Facebook permalink parameters that identify
a post are preserved. Instagram selects basic data explicitly; neither reader
advertises global keyword search or complete comment coverage.

Evidence retains the validated requested account and actual author. Instagram
collaborations are admitted only when the Actor explicitly reports the requested
account among its coauthors; they are labelled as collaborations rather than
exclusive authorship. Unsupported account associations are rejected. Social
time bounds use the Actor's accepted UTC `Z` syntax.

Topic discovery must resolve and authorize public destinations before reading.
The compilers reject an invented destination or a free-text keyword passed as a
post-reader request. Native connector acceptance is still required; exposing an Actor to Codex does not add that
capability to the agent.

An unlimited monetary grant is explicit in the compiler: `unlimited=True` with
no monetary cap omits `maxTotalChargeUsd`. It does not remove source, item,
time-window, query, provenance or idempotency controls. Legacy bounded calls
keep their previous behavior.

For unlimited X searches, the compiler also omits the billing option
`callOptions.maxItems`, while retaining the Actor's work limit
`input.maxItems`. It does not replace unlimited spending with the Actor's
minimum price. The public MCP/client forwards the two billing options to Apify;
the observed minimum-price rejection does not establish a particular conversion
formula or server default. [Apify run options](https://docs.apify.com/api/v2/actors-runs-post).

Conversation windows retain the existing connector bounds: at most 31 days,
and at most seven days for the configured YouTube week filter. Wider ranges
are unsupported by these compiled profiles; they must not be reported as a
successful empty search. Documentary retrieval uses the separately admitted
capture window rather than treating the reporting period as publication dates.

YouTube's verified `NO_VIDEOS` marker is an empty result only when it is the
single dataset row and matches the admitted query, public results URL and the
configured week/date filters. Unknown errors, mismatched queries, mixed
video/error rows and altered filters still fail validation. The empty result
retains the successful run and accounted cost; recovery uses that receipt and
does not start another search. It establishes no global absence of videos.

## Native package and exact environment

`agent_package(config, version='v2')` selects an explicit module allowlist before
assembly. The generated Python contains no `SCHEMA_DEFINITION`, `Records`,
legacy `Journal` or SQL projection. The default selection remains legacy for
existing installer callers; v2 deployment is an explicit separate target.

V2 declares `httpx==0.28.1` and `pypdf==6.20.1` in its own requirements asset.
The package leaves Oracle-managed framework and SDK distributions to the native
runtime and verifies observations separately; it does not install a public OCI
SDK over Oracle's preview build.

## Independent TEST installer

The administrator API selects v2 with `runtime_version: "v2"` in the existing
installation POST body. Observation and endpoint GETs use `?runtime_version=v2`.
Omitting the selector preserves legacy callers and receipts. Unknown versions
are rejected. This selector is available to the new frontend; the existing
Settings UI still uses its legacy contract until it supplies that selection.

`market_installation/v2_installer.py` owns a separate receipt and agent under
`/Workspace/.control/modules/ai_market_intelligence_v2_installed/`. The development
CLI's experiment folder remains independent. An existing file, manifest or agent
without this operation's ownership is preserved and requires reconciliation.
V2 compute has its own name, `aidp_ai_market_intelligence_v2_compute`; an explicitly
reused v2 compute retains its original creation operation. Legacy compute is not
adopted implicitly.

V2 phases are `verify → credentials → compute → files → agent → permissions →
test → ready`. They require neither the legacy catalog/schema nor Spark, model
bootstrap jobs or SQL tables. The existing shared OCI and project Apify credential
checks are reused. The installation initially pauses research and does not
publish PROD. First TEST initialization, refresh and session creation persist
their intents before submission. A lost acknowledgement is reconciled against
the recorded identity rather than starting another operation.

The native probe requires the exact source hash, authenticated session, required
package observations, a completed response and zero model/Actor calls. Failed
assistant messages, provisional output followed only by `[DONE]`, wrong source
and wrong sessions cannot release `ready`. The receipt keeps the observed
versions; this setup probe establishes neither full dependency parity nor MCP,
research, corpus or frontend acceptance. The endpoint route returns only that
verified ACTIVE TEST revision. Legacy destructive reinstall is unavailable for
v2. Enabling research and testing its service binding remains an explicit,
separate operation through the reviewed v2 TEST workflow below.

Native TEST records may omit `deploymentVersion`. The installer then binds the
record's key and creation/update metadata, still requiring the exact source
probe and an unchanged deployment after that probe. Session responses may omit
`deploymentType`; those sessions must match the selected TEST endpoint, agent
and compute. An explicit other deployment type is rejected.

If the backend bundle changes after TEST was submitted, v2 can finish only that
original operation after verifying the remote ownership descriptor, Python SHA
and aggregate Python/requirements SHA against its saved intent. It does not
replace the package or change the receipt's target hash. Status exposes target
and bundled hashes separately. Earlier phases still require the original bundle;
this recovery path is not an upgrade or publication approval.

Minimal paused configurations remain available for metadata probes. Active
configurations require the reviewed service references, owner/generation scope,
candidate revision, source capabilities, search locale, explicit spending mode
and exact observed runtime profile. `inspect_runtime`, `verify_transport` and
`check_services` separate zero-inference setup checks from research. Ordinary
chat uses the authenticated native trace for its request ID; API clients may
provide a stable request ID and explicit financial selections. Replay and
cancellation remain scoped to the authenticated native conversation.

`runtime_versions.py` compares exact Python patch and shared package versions.
Missing versions remain unknown; Oracle's documented framework support is not
an observation of a running compute. The actual TEST candidate must supply its
metadata and matching source hash before choosing the local dependency pins.
Oracle-managed `aidputils` remains a native integration boundary, not a local
stub advertised as a matching installation.

A real, source-bound TEST metadata observation on 2026-11-02 reported Python
3.11.13, langchain-core 1.4.6, LangGraph 1.2.4, checkpoint 4.1.1, prebuilt 1.1.0,
Pydantic 2.14.1, MCP 1.26.0 and OCI 2.184.1+preview.1.455. A separate local
environment now matches Python and all sixty-one shared distributions in the
portable dependency closure, including pypdf 6.20.1 and the private OCI artifact. Its downloaded
bytes match the source-bound native export SHA-256 and retain the SDK license.
The additional `pywin32` package is a declared Windows-specific dependency.
This is target-specific observation, not a
universal Oracle support matrix. The backend development dependencies also
include the PDF library for the regression suite; that backend environment is
not the exactly aligned v2 execution environment.

## Public streaming contract

The actual gateway omits arbitrary message metadata. `v2_wire.py` therefore
places versioned NDJSON envelopes in `AIMessage.content`: protocol
`ai-market-intelligence`, version `2.0`, kinds `event`, `response`, `snapshot` and
transport-only `heartbeat`. Public events include stable IDs/sequences, block
revisions, `source_key`/`source_keys`, `discovery_channel` and `role_key` for UI
icons. References and complete chart datasets accompany the relevant events.
Drafts are emitted after content review; private reasoning is never a packet.

`market_installation/v2_stream.py` is the tested native SSE consumer. It extracts
only public packets from assistant output, ignores internal gateway metadata,
deduplicates cumulative output and rejects conflicting event identities or an
incomplete delivery. Its bounded frames accommodate actual gateway envelopes
larger than the generic plugin test reader's limit. It does not modify the
plugin or turn a truncated generic test receipt into a pass.

The frontend should map the stable keys to its own icons, render Markdown with
HTML disabled, retain source links from the reference manifests and upsert
blocks/artifacts by identity. `draft.corrected` supersedes an earlier block;
`draft.compacted` groups verified drafts at finalisation. A heartbeat conveys
transport liveness only. Reconnection uses the authenticated replay command and
the last event sequence; a disconnect does not cancel a capture.

[`examples/market-v2-stream.mjs`](../examples/market-v2-stream.mjs) is a small
browser-compatible reference reducer for normalized public packets. It preserves
`source_key`, `source_keys` and `role_key`, checks request/event identity, requests
replay on sequence gaps and restores source links from replay manifests. Replay
references and blocks come from one consistent scoped state read. A
`draft.corrected` event with `status: failed` withdraws that block and hides its
provisional charts. The final response's `artifacts` list replaces the preview
set, including when empty. A resumable failure remains provisional. This module
does not render HTML, implement authentication or replace the user's chat UI;
it consumes packets extracted by the backend gateway decoder.

## Validation boundaries

The local observer saves complete model responses before downstream validation.
A transient Windows file lock permits bounded retries of the local write only;
it never resubmits the model request. Persistent failures retain their receipts
for reconciliation. Capture, evaluator failures and answer quality are separate.

The local checks cover source authority, publication before final delivery,
rejected prose, refinement closure, cancellation during collection/model waits,
replay, snapshot isolation, financial datasets, assembly and the metadata
bootstrap. They do not certify the model's semantic accuracy.

Validation includes focused regression tests, a complete local real-model graph
over frozen native documents, actual model/MCP service checks and a native
research delivery with verified drafts, correction, compaction and a partial
final answer. A partial answer preserves its unresolved business question; it
does not establish corpus acceptance. Current candidate checks and failures are
recorded separately in the private evaluation evidence.

The local v2 factory is `tests/market_v2_flow_factory.py`. It uses the same
portable workflow and a hash-bound shared OCI adapter, checks exact runtime and
model bindings, starts fresh evaluation state and cannot execute retrieval tools.
Its source capabilities are those captured for the fixture. It rejects more
than fifty documents per source rather than silently dropping later evidence.
This is G2 reasoning over fixed documents, not G3 retrieval or durable native
checkpoint acceptance. Production installation is still a separate legacy path;
building an active v2 package does not select it automatically.

## Deploy Studio handoff to a new tenant

This is development source with research and client acceptance still pending.
A Git branch alone does not update the image used by Deploy Studio. Its existing
release path requires a commit reachable from `main`, an exact release tag, and
the frozen image and manifest verified by hash. Integrate the coordinated
Governance changes before preparing the combined release; this handoff does not
relax the stable release or updater guards.

The new platform receives its region, AIDP platform, compartment, model and
storage references from the destination Terraform configuration. Retain private
corpora, criteria and uncertain-operation receipts separately, with their original
candidate bindings. Do not copy credentials, service identity hashes, workspace
keys, active generations or SDK artifacts from the previous tenant into GitHub
or silently use them as the new target's configuration.

The Settings frontend currently selects the legacy installer. The administrator
API's explicit `runtime_version: "v2"` creates the separate paused TEST described
above, without the legacy tables. A Settings-based research flow still needs a
consistent v2 selector and a reviewed activation binding for the destination.
The paused probe reports native source and package versions with zero model or
Actor calls; it is setup evidence, not acceptance of research. Observe that
target's actual runtime, align local testing to it, and verify its model, MCP,
credentials and spending authorization before enabling research. Do not inherit
an earlier tenant's runtime observation or semantic approvals.

## Isolated native TEST operations

`scripts/market_v2.py` prepares and manages only the dedicated v2 TEST source
root. It uses the installed AIDP plugin with the existing authenticated project
transport, exact active workspace and existing AI Compute. It cannot publish
PROD, provision compute or create the legacy business tables.

Prepare with the exactly aligned v2 Python environment. Manage the native
connection with the existing backend environment and the reviewed installed
plugin scripts path:

```powershell
python scripts/market_v2.py prepare --runtime-config <private-runtime.json> --output <candidate-folder>
python scripts/market_v2.py sync --plugin-scripts <installed-plugin-scripts> --apply
python scripts/market_v2.py test-refresh --plugin-scripts <installed-plugin-scripts> --apply
python scripts/market_v2.py status --plugin-scripts <installed-plugin-scripts>
python scripts/market_v2.py new-session --label <unique-session> --plugin-scripts <installed-plugin-scripts> --apply
python scripts/market_v2.py invoke --command <private-command.json> --label <unique-study> --plugin-scripts <installed-plugin-scripts> --apply
```

Before invoking, the CLI reconciles and requires the exact local package receipt
to match the active TEST deployments. A pending refresh cannot be bypassed by
calling an older active deployment. Each invocation writes an immutable intent,
public packet log, packet arrival offsets (`arrivals.jsonl`) and delivery result
under the ignored
`.aidp-local/v2/evaluations/` directory. Known local transport failures retain a
stable diagnostic code without storing raw gateway/provider messages; unknown
exceptions retain their type and an unclassified reason. This does not prove
whether an uncertain capture started. Existing study labels cannot be reused.
Use the same request ID to resume or replay; delivery failure does not authorize
a replacement capture. A complete transport result is separate from semantic
or client acceptance.

The original 250-question corpus and seven variants retain their identifiers.
Input integrity, recorded-fixture passes, real-model reasoning, live connectors,
native TEST and client rendering must be recorded separately. No previous
research approvals are inherited by this design.

### Bounded pilot reference pass

The maintained runner binds all six reviewed pilot questions, their exact corpus
and oracle hashes, the runtime configuration and the compiled candidate source.
Admission is immutable. Prepare it before any paid case:

```powershell
python scripts/market_v2.py pilot-prepare --definition <private-pilot-definition.json>
python scripts/market_v2.py pilot-run --plugin-scripts <installed-plugin-scripts>
python scripts/market_v2.py pilot-run --plugin-scripts <installed-plugin-scripts> --apply
python scripts/market_v2.py pilot-prepare --cycle 1 --definition <revised-candidate-definition.json>
python scripts/market_v2.py pilot-run --cycle 1 --plugin-scripts <installed-plugin-scripts> --apply
```

One driver holds an exclusive local lock for the entire pass. Each research case
gets a fresh TEST session; the follow-up reuses its preceding pilot report's
session. Session creation intent is durable before submission. An uncertain
session is reconciled by its exact identity and native binding, without another
creation. A nonterminal delivery stops the pass for receipt reconciliation.
Completed request and effect snapshots are frozen through direct, owner/session/
candidate-bound reads; the freezer neither lists the bucket nor writes history.

The reference pass is admitted once and allows at most two later comparative
calibration cycles, selected with `--cycle 1` or `--cycle 2`. Each admission
requires the previous six cases to be closed, preserves the questions and
oracle/corpus bytes, and binds a new candidate. The runner executes the pass;
semantic approval requires reviewing its evidence and useful answer separately.
Packet arrival offsets measure reception in the native test consumer; event
order or server emission timestamps alone do not establish delivery latency.
They do not prove browser rendering, which requires the deferred client test.
Transport completion, factual fidelity and answer utility remain separate
verdicts. A fixed-document comparison measures analysis over those documents;
it cannot establish a native retrieval, routing or client-rendering improvement.

If a known Actor outlives the bounded poll, inspect its existing run and resume
the same request with `pilot-run --cycle <n> --resume-case <case-id> --apply`.
The runner requires known capture receipts and no unresolved model/capture start.
One resume permit is retained per pending Actor group, with a separate delivery
receipt. It does not create a replacement session or repeat a completed model
decision. An unchanged pending group cannot be resubmitted again by this route.

Each development project keeps its own ignored `node-progress.json`,
`test-campaign.json` and authoritative acceptance plan. Active maps, spending
grants, private question corpora and receipts are not distributed in this public
repository. Preserve them when moving development between tenants; a fresh
checkout does not inherit an earlier tenant's approvals. The installed
plugin's `progress report` derives current node and product coverage; pending
native reviews stay pending. The Python regression receipts and the data-only
consumer checks remain separate evidence, rather than being converted into
native approvals. Source selection is an internal bounded decision in `collect`,
not another autonomous agent or graph node.

Native criterion case handles are versioned for the candidate under review.
The criterion IDs, descriptions and denominator stay fixed. A cached native
delivery can authenticate the current research and response, but replay alone
does not demonstrate new reasoning, clarification, cancellation or an uncertain
start's retry guard. Register a criterion only with its specific reviewed
behavior; a known counterexample keeps the affected criterion open. Keep node
coverage, corpus usefulness and whole-product acceptance as separate measures.

For G1, `market_v2_flow_factory` delegates only the plugin's exact `FixtureModel`
to `market_v2_fixture_factory`. Each decision consumes one registered JSON
completion and uses the supplied in-memory checkpointer. Research, clarification,
missing-report reuse and a schedule proposal cover the twelve nodes. The reuse
fixture checks the absent-report boundary, not successful report reuse. These
synthetic completions exercise mechanics; they neither call OCI/Apify nor prove
reasoning quality. The real-model branch retains its frozen-document, adapter,
candidate and runtime checks.

Replacing or deleting resources requires the authorization and ownership checks
for that specific environment; a previous development session grants none in a
different tenant. V2 does not
need deletion of the legacy framework tables to run; no deletion occurs as an
import, package-build or request side effect. Any cleanup must identify the
actual owned resources and preserve evidence/accounting needed for recovery.
