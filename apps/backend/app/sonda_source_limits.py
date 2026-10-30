"""Draft Source Limits 1.0.0 sidecar; no transport or broker activation.

Huginn remains the configuration and reservation authority. Admission is a pure
check: its returned ledger must be committed by the broker's existing lock/CAS
before a provider POST. A failed CAS requires rechecking the current ledger.
"""
from __future__ import annotations

import json
from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, StrictBool, model_validator

from app.sonda_contract import (
    Configuration, Count, Hash, Record, Revision, encoded, no_duplicate_keys,
    reject_secrets, sha256,
)

SOURCE_LIMITS_VERSION = '1.0.0'
SOURCE_LIMITS_MAX_BYTES = 1024 * 1024
CORE_CONTRACT_SHA256 = '6e2613ac0c28e863fdf2fc02d69f92b92dd56003edcc242c6b41bbe218924483'


class SourcePolicy(Record):
    source_id: UUID
    source_revision: Revision
    policy_revision: Revision
    max_items_per_execution: Count | None
    max_cost_per_round_microusd: Count | None
    max_starts_per_round: Count | None


class SourceLimitsSnapshot(Record):
    kind: Literal['source_limits'] = 'source_limits'
    capability_version: Literal['1.0.0'] = SOURCE_LIMITS_VERSION
    core_contract_version: Literal['1.0.2'] = '1.0.2'
    core_contract_sha256: Literal[CORE_CONTRACT_SHA256] = CORE_CONTRACT_SHA256
    registry_id: UUID
    generation_id: UUID
    catalog_revision: Revision
    previous_catalog_sha256: Hash | None
    config_revision: Revision
    configuration_payload_sha256: Hash
    policies: Annotated[list[SourcePolicy], Field(max_length=10000)]

    @model_validator(mode='after')
    def snapshot_identity(self):
        if (self.catalog_revision == 1) != (self.previous_catalog_sha256 is None):
            raise ValueError('Only the first snapshot has no predecessor hash')
        ids = [policy.source_id for policy in self.policies]
        if len(ids) != len(set(ids)):
            raise ValueError('Source policies must be distinct')
        if ids != sorted(ids, key=str):
            raise ValueError('Source policies must use canonical source order')
        return self


def source_limits_digest(snapshot: SourceLimitsSnapshot) -> str:
    return sha256(encoded(snapshot.model_dump(mode='json')))


def check_source_limits(snapshot, configuration, *, registry_id, generation_id, previous=None):
    """Require an exact core snapshot and complete, explicit per-source policies.

    Null fields remove an extra cap. A policy may be omitted only when that source
    is absent from the matching core configuration; omission never means unlimited.
    """
    snapshot = SourceLimitsSnapshot.model_validate(snapshot)
    configuration = Configuration.model_validate(configuration)
    if (snapshot.registry_id, snapshot.generation_id) != (UUID(str(registry_id)), UUID(str(generation_id))):
        raise ValueError('Source limits scope mismatch')
    core = configuration.model_dump(mode='json')
    if snapshot.config_revision != configuration.revision or snapshot.configuration_payload_sha256 != sha256(encoded(core)):
        raise ValueError('Source limits require the exact core configuration')
    sources = {source.source_id: source.revision for source in configuration.sources}
    if {policy.source_id: policy.source_revision for policy in snapshot.policies} != sources:
        raise ValueError('Full snapshot must cover exact current source revisions')
    if previous is not None:
        _check_source_limits_successor(snapshot, SourceLimitsSnapshot.model_validate(previous))
    elif snapshot.catalog_revision != 1:
        raise ValueError('A successor requires its authenticated predecessor')
    return snapshot


def _check_source_limits_successor(snapshot, previous):
    if (snapshot.registry_id, snapshot.generation_id) != (previous.registry_id, previous.generation_id):
        raise ValueError('Source limits predecessor scope mismatch')
    if snapshot.catalog_revision != previous.catalog_revision + 1 or snapshot.previous_catalog_sha256 != source_limits_digest(previous):
        raise ValueError('Source limits do not extend the current catalog')
    if snapshot.config_revision <= previous.config_revision:
        raise ValueError('Saving source policy requires a new core configuration revision')
    old = {policy.source_id: policy for policy in previous.policies}
    for policy in snapshot.policies:
        prior = old.get(policy.source_id)
        if prior and (policy.policy_revision < prior.policy_revision or
            (policy.policy_revision == prior.policy_revision and policy != prior)):
            raise ValueError('Changed source policy requires a new policy revision')


def parse_source_limits(raw: bytes, *, expected_sha256, configuration, registry_id, generation_id, previous=None):
    """Parse canonical protected bytes pinned by their exact server-held digest."""
    if not isinstance(raw, bytes) or len(raw) > SOURCE_LIMITS_MAX_BYTES:
        raise ValueError('Source limits exceed the byte bound')
    if sha256(raw) != expected_sha256:
        raise ValueError('Source limits raw digest mismatch')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=no_duplicate_keys)
    reject_secrets(value)
    snapshot = check_source_limits(value, configuration, registry_id=registry_id,
        generation_id=generation_id, previous=previous)
    if raw != encoded(snapshot.model_dump(mode='json')):
        raise ValueError('Source limits require canonical bytes including explicit defaults')
    return snapshot


def policy_for_source(snapshot, source_id, *, expected_sha256):
    """Legacy absence is allowed only when no sidecar is expected by the broker."""
    if snapshot is None:
        if expected_sha256 is not None:
            raise ValueError('Expected source limits are unavailable')
        return None
    snapshot = SourceLimitsSnapshot.model_validate(snapshot)
    if expected_sha256 is None or source_limits_digest(snapshot) != expected_sha256:
        raise ValueError('Source limits have not been acknowledged at the expected digest')
    policy = next((policy for policy in snapshot.policies if policy.source_id == UUID(str(source_id))), None)
    if policy is None:
        raise ValueError('Expected source policy is unavailable')
    return policy


class SourceRoundLedger(Record):
    """Aggregate for all queries/capture/research in one source/global round.

    Starts include uncertain provider responses; pending_reserved_microusd includes
    their unconfirmed maximum charges. Policy revisions never change this identity.
    """
    model_config = ConfigDict(extra='forbid', frozen=True)
    registry_id: UUID
    generation_id: UUID
    round_id: UUID
    source_id: UUID
    spent_microusd: Count
    pending_reserved_microusd: Count
    starts: Count


class SourceStart(Record):
    items: Annotated[int, Field(strict=True, ge=1, le=100000)]
    reserve_microusd: Count
    verified_free: StrictBool


def source_ledger_key(ledger):
    ledger = SourceRoundLedger.model_validate(ledger)
    return tuple(str(getattr(ledger, field)) for field in ('registry_id', 'generation_id', 'round_id', 'source_id'))


def reserve_source_start(ledger, start, *, policy):
    """Compute a reservation; broker must atomically commit it before a real POST.

    This does not check global/query caps or prices. Those existing checks remain
    mandatory. A None policy is the explicitly verified legacy path only.
    """
    ledger = SourceRoundLedger.model_validate(ledger)
    start = SourceStart.model_validate(start)
    policy = SourcePolicy.model_validate(policy) if policy is not None else None
    if start.verified_free and start.reserve_microusd != 0:
        raise ValueError('A verified-free start cannot reserve a paid charge')
    if policy is not None:
        _check_source_start(ledger, start, policy)
    return SourceRoundLedger.model_validate({**ledger.model_dump(mode='json'),
        'pending_reserved_microusd': ledger.pending_reserved_microusd + start.reserve_microusd,
        'starts': ledger.starts + 1})


def _check_source_start(ledger, start, policy):
    if ledger.source_id != policy.source_id:
        raise ValueError('Reservation source mismatch')
    if policy.max_items_per_execution is not None and start.items > policy.max_items_per_execution:
        raise ValueError('Source execution item limit exceeded')
    if policy.max_starts_per_round is not None and ledger.starts >= policy.max_starts_per_round:
        raise ValueError('Source round start limit exceeded')
    cap = policy.max_cost_per_round_microusd
    if cap == 0 and not start.verified_free:
        raise ValueError('Zero source spend permits only verified-free tools')
    if cap is not None and ledger.spent_microusd + ledger.pending_reserved_microusd + start.reserve_microusd > cap:
        raise ValueError('Source round spend limit exceeded')


def check_source_reconciliation(ledger, *, registry_id, generation_id, round_id, source_id):
    """Existing commitments retain their identity even after caps are lowered.

    Provider execution/reservation identity and settlement are still checked by the
    broker. This validates scope only and cannot authorize a new POST or release.
    """
    expected = tuple(str(UUID(str(value))) for value in (registry_id, generation_id, round_id, source_id))
    if source_ledger_key(ledger) != expected:
        raise ValueError('Source reconciliation ledger scope mismatch')
    return SourceRoundLedger.model_validate(ledger)
