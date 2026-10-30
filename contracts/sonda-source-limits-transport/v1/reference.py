"""Draft Source Limits transport codec; protected reads/writes belong to callers.

All keys are derived inside the installation's bound artifacts bucket. Snapshots
and ACKs use create-only writes. An uncertain write is resolved by reading the
same key: identical bytes are a replay; different bytes are a conflict. Never
retry under a replacement identity. Publish head.json using its previous ETag
(or If-None-Match:* for creation), then read it back before treating it as saved.
On CAS conflict reload and validate the same predecessor; never blind overwrite.

This module does not authenticate an issuer, sign proofs, access OCI, apply Delta
rows or grant execution. A caller must authenticate every supplied raw object and
query the supplied publication and applied-row witness at its pinned version.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import Field, TypeAdapter, model_validator

from sonda_core_1_0_2 import (
    Code, ConfigMessage, Count, Hash, ProcessingReceipt, ReceiptMessage, Record,
    Revision, StreamEntry, encoded, no_duplicate_keys, parse, reject_secrets, sha256,
)
from sonda_source_limits_1_0_0 import CORE_CONTRACT_SHA256, parse_source_limits

LIMITS_CAPABILITY_SHA256 = '1806d6ae7de6c975123106b477728591c594f97f604a27ac5b1d00c526a16528'
LIMITS_TRANSPORT_VERSION = '1.0.0'
LIMITS_TRANSPORT_MAX_BYTES = 1024 * 1024


class SourceLimitsRef(Record):
    source_limits_capability_version: Literal['1.0.0']
    source_limits_capability_sha256: Literal[LIMITS_CAPABILITY_SHA256]
    core_contract_version: Literal['1.0.2']
    core_contract_sha256: Literal[CORE_CONTRACT_SHA256]
    registry_id: UUID
    generation_id: UUID
    catalog_revision: Revision
    snapshot_sha256: Hash
    previous_catalog_sha256: Hash | None
    config_revision: Revision
    configuration_payload_sha256: Hash

    @model_validator(mode='after')
    def predecessor(self):
        if (self.catalog_revision == 1) != (self.previous_catalog_sha256 is None):
            raise ValueError('Only the first catalog reference has no predecessor')
        return self


class SourceLimitsHead(Record):
    kind: Literal['source_limits_head']
    transport_version: Literal['1.0.0']
    ref: SourceLimitsRef


class SourceLimitsAck(Record):
    kind: Literal['source_limits_ack']
    transport_version: Literal['1.0.0']
    state: Literal['applied']
    ref: SourceLimitsRef
    core_configuration_message_id: UUID
    core_configuration_source_sequence: Revision
    core_configuration_raw_sha256: Hash
    core_configuration_payload_sha256: Hash
    processing_receipt_message_id: UUID
    processing_receipt_raw_sha256: Hash
    processing_receipt_payload: ProcessingReceipt
    publication_id: Hash
    publication_config_revision: Revision
    serving_versions: Annotated[dict[Code, Count], Field(min_length=2, max_length=26)]

    @model_validator(mode='after')
    def applied_identity(self):
        receipt = self.processing_receipt_payload
        if receipt.status != 'applied' or receipt.source_message_id != self.core_configuration_message_id or receipt.source_sequence != self.core_configuration_source_sequence:
            raise ValueError('ACK requires its exact applied core processing receipt')
        if self.core_configuration_payload_sha256 != self.ref.configuration_payload_sha256:
            raise ValueError('ACK core payload must match the source limits reference')
        if self.publication_config_revision < self.ref.config_revision:
            raise ValueError('Publication cannot precede the acknowledged configuration')
        _check_publication_hash(self.ref, self.publication_id, self.publication_config_revision, self.serving_versions)
        return self


class AppliedSourceLimitsRow(Record):
    """Factual witness from the writer's exact published Delta table version."""
    registry_id: UUID
    generation_id: UUID
    catalog_revision: Revision
    config_revision: Revision
    snapshot_sha256: Hash
    configuration_payload_sha256: Hash
    serving_version: Count


LimitsTransportDocument = Annotated[Union[SourceLimitsHead, SourceLimitsAck], Field(discriminator='kind')]
LIMITS_TRANSPORT_ADAPTER = TypeAdapter(LimitsTransportDocument)


def source_limits_transport_schema():
    return {'$schema': 'https://json-schema.org/draft/2020-12/schema',
        '$id': 'urn:ai-social-network:sonda:source-limits-transport:1.0.0',
        **LIMITS_TRANSPORT_ADAPTER.json_schema()}


def parse_limits_transport(raw, *, expected_sha256, registry_id, generation_id):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= LIMITS_TRANSPORT_MAX_BYTES:
        raise ValueError('Source limits transport byte bound exceeded')
    if sha256(raw) != expected_sha256:
        raise ValueError('Source limits transport raw digest mismatch')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys)
    reject_secrets(value)
    document = LIMITS_TRANSPORT_ADAPTER.validate_python(value)
    if (document.ref.registry_id, document.ref.generation_id) != (UUID(str(registry_id)), UUID(str(generation_id))):
        raise ValueError('Source limits transport scope mismatch')
    if raw != encoded(document.model_dump(mode='json')):
        raise ValueError('Source limits transport requires exact canonical bytes')
    return document


def source_limits_prefix(registry_id, generation_id):
    values = []
    for value in (registry_id, generation_id):
        if str(UUID(str(value))) != str(value):
            raise ValueError('Source limits keys require canonical UUIDs')
        values.append(str(value))
    return ('ai_social_network/registries/'+values[0]+'/generations/'+values[1]+'/control/source_limits/v1/')


def source_limits_head_key(registry_id, generation_id):
    return source_limits_prefix(registry_id, generation_id)+'head.json'


def source_limits_snapshot_key(ref):
    ref = SourceLimitsRef.model_validate(ref)
    return source_limits_prefix(ref.registry_id, ref.generation_id)+'snapshots/'+str(ref.catalog_revision)+'-'+ref.snapshot_sha256+'.json'


def source_limits_ack_key(ref):
    ref = SourceLimitsRef.model_validate(ref)
    return source_limits_prefix(ref.registry_id, ref.generation_id)+'acks/'+str(ref.catalog_revision)+'-'+ref.snapshot_sha256+'.json'


def immutable_limits_write(existing, proposed):
    """Resolve create-only write outcome at the same derived immutable key."""
    if not isinstance(proposed, bytes) or not 1 <= len(proposed) <= LIMITS_TRANSPORT_MAX_BYTES:
        raise ValueError('Invalid proposed source limits object bytes')
    if existing is None:
        return 'create'
    if not isinstance(existing, bytes):
        raise ValueError('Existing source limits object must be raw bytes')
    if existing != proposed:
        raise ValueError('Conflicting immutable source limits object')
    return 'same'


def check_limits_head_successor(current, proposed):
    """Validate facts before CAS; this does not perform the storage CAS itself."""
    proposed = SourceLimitsHead.model_validate(proposed)
    if current is None:
        if proposed.ref.catalog_revision != 1:
            raise ValueError('A new limits head must start at catalog revision one')
        return 'create'
    current = SourceLimitsHead.model_validate(current)
    if proposed == current:
        return 'same'
    if (proposed.ref.registry_id, proposed.ref.generation_id) != (current.ref.registry_id, current.ref.generation_id):
        raise ValueError('Limits head predecessor scope mismatch')
    if proposed.ref.catalog_revision != current.ref.catalog_revision + 1 or proposed.ref.previous_catalog_sha256 != current.ref.snapshot_sha256:
        raise ValueError('Limits head does not extend its exact predecessor')
    if proposed.ref.config_revision <= current.ref.config_revision:
        raise ValueError('Saving limits requires an advanced core configuration')
    return 'replace'


def _check_publication_hash(ref, publication_id, revision, versions):
    if not {'ai_social_source_limits', 'ai_social_registry'}.issubset(versions):
        raise ValueError('Publication must pin limits and core registry versions')
    if any(not name.startswith('ai_social_') for name in versions):
        raise ValueError('Publication serving table is outside Sonda')
    scope = {'registry_id': str(ref.registry_id), 'generation_id': str(ref.generation_id)}
    if publication_id != sha256(encoded([scope, revision, versions])):
        raise ValueError('Publication hash does not match exact scope/revision/versions')


def _checked_core_proof(ack, configuration_raw, entry_raw, receipt_raw):
    ref = ack.ref
    scope = {'registry_id': ref.registry_id, 'generation_id': ref.generation_id,
        'contract_sha256': CORE_CONTRACT_SHA256}
    configuration, entry, receipt = [parse(raw, **scope) for raw in (configuration_raw, entry_raw, receipt_raw)]
    if not isinstance(configuration, ConfigMessage) or not isinstance(entry, StreamEntry) or not isinstance(receipt, ReceiptMessage):
        raise ValueError('ACK proof requires core configuration, stream entry and receipt')
    if configuration.message_id != ack.core_configuration_message_id or configuration.payload.revision != ref.config_revision:
        raise ValueError('ACK references a different historical core configuration')
    if sha256(configuration_raw) != ack.core_configuration_raw_sha256 or sha256(encoded(configuration.payload.model_dump(mode='json'))) != ref.configuration_payload_sha256:
        raise ValueError('ACK core configuration body/payload digest mismatch')
    if (entry.direction, entry.message_kind, entry.sequence, entry.message_id, entry.message_sha256, entry.message_bytes) != (
        'huginn_to_aidp', 'configuration', ack.core_configuration_source_sequence,
        configuration.message_id, sha256(configuration_raw), len(configuration_raw)):
        raise ValueError('ACK core stream entry does not bind the exact configuration bytes')
    if (receipt.message_id, sha256(receipt_raw), receipt.payload) != (
        ack.processing_receipt_message_id, ack.processing_receipt_raw_sha256, ack.processing_receipt_payload):
        raise ValueError('ACK processing receipt bytes/identity do not match')
    return configuration


def _checked_publication_proof(ack, publication, applied_row):
    ref = ack.ref
    versions = TypeAdapter(dict[Code, Count]).validate_python(
        json.loads(publication['versions_json'], object_pairs_hook=no_duplicate_keys))
    actual = (str(publication['registry_id']), str(publication['generation_id']),
        publication['publication_id'], TypeAdapter(Revision).validate_python(publication['config_revision']), versions)
    expected = (str(ref.registry_id), str(ref.generation_id), ack.publication_id,
        ack.publication_config_revision, ack.serving_versions)
    if actual != expected:
        raise ValueError('ACK differs from the actual writer publication')
    row = AppliedSourceLimitsRow.model_validate(applied_row)
    expected_row = {'registry_id': ref.registry_id, 'generation_id': ref.generation_id,
        'catalog_revision': ref.catalog_revision, 'config_revision': ref.config_revision,
        'snapshot_sha256': ref.snapshot_sha256, 'configuration_payload_sha256': ref.configuration_payload_sha256,
        'serving_version': versions['ai_social_source_limits']}
    if row != AppliedSourceLimitsRow.model_validate(expected_row):
        raise ValueError('ACK lacks its exact applied limits row at the published version')


def check_applied_limits_ack(ack, *, snapshot_raw, configuration_raw, entry_raw,
        receipt_raw, publication, applied_row, previous_snapshot=None):
    """Check protected facts; the caller authenticates objects and Delta reads.

    Historical sidecars are matched to their own historical ConfigMessage, even
    when the current core revision and final publication have already advanced.
    Missing ACK/proof is an error, never legacy unlimited or a pending success.
    """
    ack = SourceLimitsAck.model_validate(ack)
    configuration = _checked_core_proof(ack, configuration_raw, entry_raw, receipt_raw)
    ref = ack.ref
    snapshot = parse_source_limits(snapshot_raw, expected_sha256=ref.snapshot_sha256,
        configuration=configuration.payload, registry_id=ref.registry_id,
        generation_id=ref.generation_id, previous=previous_snapshot)
    if (snapshot.catalog_revision, snapshot.previous_catalog_sha256) != (ref.catalog_revision, ref.previous_catalog_sha256):
        raise ValueError('ACK references a different limits snapshot predecessor')
    _checked_publication_proof(ack, publication, applied_row)
    return ack
