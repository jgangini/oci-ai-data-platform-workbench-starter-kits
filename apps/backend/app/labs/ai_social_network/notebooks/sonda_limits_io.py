"""Protected Source Limits consumption under the existing generation writer lease.

No provider or model calls. At most 25 new revisions are applied per pass. Both
snapshot ancestry and publication-history proof reads are bounded to 64 versions;
larger gaps fail closed until an operator supplies a reviewed paged history index.
An ACK is prepared only after reading its actual Delta publication and row witness.
Those facts and its exact bytes are saved in a private create-only proof journal
before ACK publication, so later cron passes do not depend on old Delta history.
"""
import json
from uuid import UUID

from app.sonda_contract import (
    ConfigMessage, ReceiptMessage, StreamEntry, encoded, message_target, no_duplicate_keys,
    parse, reject_secrets, sha256, stream_prefix,
)
from app.sonda_source_limits import CORE_CONTRACT_SHA256, SourceLimitsSnapshot, parse_source_limits
from app.sonda_source_limits_transport import (
    LIMITS_CAPABILITY_SHA256, SourceLimitsAck, SourceLimitsHead, SourceLimitsRef,
    check_applied_limits_ack, immutable_limits_write, parse_limits_transport,
    source_limits_ack_key, source_limits_head_key, source_limits_prefix,
)
from app.labs.ai_social_network.notebooks.sonda_delta import (
    _find, _scope, apply_source_limits, checked_binding, definitions,
)


class SourceLimitsPending(RuntimeError):
    """A historical core configuration or its published receipt is not ready."""


def _limits_scope(binding):
    return {key: UUID(binding[key]) for key in ('registry_id', 'generation_id')}


def _limits_table(binding, suffix):
    return definitions(binding)['ai_social_' + suffix]['table']


def _limits_where(binding):
    scope = _limits_scope(binding)
    return " AND ".join(key + "='" + str(value) + "'" for key, value in scope.items())


def _limits_latest_row(spark, binding):
    rows = spark.sql('SELECT * FROM ' + _limits_table(binding, 'source_limits') +
        ' WHERE ' + _limits_where(binding) + ' ORDER BY catalog_revision DESC LIMIT 1').collect()
    return rows[0].asDict() if rows else None


def _limits_ref(snapshot, digest):
    value = snapshot.model_dump(mode='json')
    fields = ('registry_id', 'generation_id', 'catalog_revision', 'previous_catalog_sha256',
        'config_revision', 'configuration_payload_sha256')
    return SourceLimitsRef.model_validate({key: value[key] for key in fields} | {
        'source_limits_capability_version': '1.0.0', 'source_limits_capability_sha256': LIMITS_CAPABILITY_SHA256,
        'core_contract_version': '1.0.2', 'core_contract_sha256': CORE_CONTRACT_SHA256,
        'snapshot_sha256': digest})


def _limits_snapshot(raw, binding, revision, digest):
    if not isinstance(raw, bytes) or len(raw) > 1024 * 1024 or sha256(raw) != digest:
        raise ValueError('Source limits snapshot bytes do not match their protected reference')
    snapshot = SourceLimitsSnapshot.model_validate_json(raw)
    if raw != encoded(snapshot.model_dump(mode='json')):
        raise ValueError('Source limits snapshot requires exact canonical bytes')
    if snapshot.catalog_revision != revision or any(
            getattr(snapshot, key) != value for key, value in _limits_scope(binding).items()):
        raise ValueError('Source limits snapshot identity mismatch')
    return snapshot


def _limits_saved_snapshot(row, binding):
    snapshot = _limits_snapshot(row['payload_json'].encode('utf-8'), binding,
        row['catalog_revision'], row['snapshot_sha256'])
    expected = snapshot.model_dump(mode='json')
    fields = ('registry_id', 'generation_id', 'config_revision', 'configuration_payload_sha256')
    if any(row[key] != expected[key] for key in fields):
        raise ValueError('Applied source limits row metadata differs from its snapshot')
    return snapshot


def _limits_previous(spark, binding, snapshot):
    if snapshot.catalog_revision == 1:
        return None
    rows = _find(spark, binding, 'source_limits', catalog_revision=snapshot.catalog_revision - 1)
    if len(rows) != 1:
        raise ValueError('Applied source limits predecessor is unavailable')
    previous = _limits_saved_snapshot(rows[0], binding)
    if sha256(encoded(previous.model_dump(mode='json'))) != snapshot.previous_catalog_sha256:
        raise ValueError('Applied source limits predecessor digest mismatch')
    return previous


def _limits_chain(objects, binding, ref, current):
    anchor_revision = current.catalog_revision if current else 0
    if not 0 <= ref.catalog_revision - anchor_revision <= 64:
        raise ValueError('Source limits ancestry exceeds the bounded history window')
    revision, digest = ref.catalog_revision, ref.snapshot_sha256
    chain = []
    while revision > anchor_revision:
        key = source_limits_prefix(binding['registry_id'], binding['generation_id']) + 'snapshots/' + str(revision) + '-' + digest + '.json'
        blob = objects.get('artifacts', key)
        if blob is None:
            raise ValueError('Expected source limits snapshot is unavailable')
        snapshot = _limits_snapshot(blob.data, binding, revision, digest)
        chain.append((snapshot, blob.data))
        revision, digest = revision - 1, snapshot.previous_catalog_sha256
    anchor_hash = sha256(encoded(current.model_dump(mode='json'))) if current else None
    if digest != anchor_hash:
        raise ValueError('Source limits head does not extend its applied predecessor')
    latest = chain[0][0] if chain else current
    if latest is None or _limits_ref(latest, ref.snapshot_sha256) != ref:
        raise ValueError('Source limits head differs from its exact snapshot')
    return list(reversed(chain))


def _limits_raw_message(objects, binding, kind, identity, direction, source_prefix):
    target = message_target(kind, identity, binding['registry_id'], binding['generation_id'], direction, source_prefix)
    blob = objects.get(target['bucket_role'], target['key'])
    return blob.data if blob else None


def _limits_core_proof(spark, binding, objects, inbound, ref):
    registry = _find(spark, binding, 'registry', config_revision=ref.config_revision)
    if not registry:
        raise SourceLimitsPending('Core configuration is not applied yet')
    if len(registry) != 1:
        raise ValueError('Ambiguous historical core configuration')
    stored = ConfigMessage.model_validate_json(registry[0]['message_json'])
    receipts = _find(spark, binding, 'receipts', message_id=str(stored.message_id))
    if not receipts or receipts[0]['state'] in ('APPLYING', 'READY'):
        raise SourceLimitsPending('Core configuration processing is not applied yet')
    if len(receipts) != 1 or receipts[0]['state'] != 'APPLIED' or receipts[0]['code'] != 'applied':
        raise ValueError('Core configuration was not accepted')
    receipt = receipts[0]
    configuration_raw = _limits_raw_message(objects, binding, 'configuration', stored.message_id,
        'huginn_to_aidp', inbound.source_prefix)
    entry_blob = objects.get('artifacts', inbound._entry(receipt['source_sequence']))
    if configuration_raw is None or entry_blob is None:
        raise ValueError('Protected applied core proof disappeared')
    if sha256(configuration_raw) != receipt['raw_sha256']:
        raise ValueError('Applied core raw digest differs from protected bytes')
    scope = {**_limits_scope(binding), 'contract_sha256': CORE_CONTRACT_SHA256}
    configuration = parse(configuration_raw, **scope)
    if not isinstance(configuration, ConfigMessage) or configuration != stored:
        raise ValueError('Protected historical core configuration differs from Gold')
    receipt_raw = _limits_processing_receipt(spark, binding, objects, inbound, str(stored.message_id))
    proof = {'configuration_raw': configuration_raw, 'entry_raw': entry_blob.data, 'receipt_raw': receipt_raw,
        'configuration': configuration, 'source_sequence': receipt['source_sequence']}
    _limits_check_core_transport(proof, ref)
    return proof


def _limits_check_core_transport(proof, ref):
    scope = {'registry_id': ref.registry_id, 'generation_id': ref.generation_id,
        'contract_sha256': CORE_CONTRACT_SHA256}
    configuration = proof['configuration']
    entry, receipt = [parse(proof[key], **scope) for key in ('entry_raw', 'receipt_raw')]
    if not isinstance(entry, StreamEntry) or not isinstance(receipt, ReceiptMessage):
        raise ValueError('Source limits require protected core stream and receipt messages')
    identity = (entry.direction, entry.message_kind, entry.sequence, entry.message_id,
        entry.message_sha256, entry.message_bytes)
    expected = ('huginn_to_aidp', 'configuration', proof['source_sequence'], configuration.message_id,
        sha256(proof['configuration_raw']), len(proof['configuration_raw']))
    if identity != expected or configuration.payload.revision != ref.config_revision:
        raise ValueError('Protected core stream entry does not bind the exact historical configuration')
    receipt_identity = (receipt.direction, receipt.payload.status,
        receipt.payload.source_message_id, receipt.payload.source_sequence)
    if receipt_identity != ('aidp_to_huginn', 'applied', configuration.message_id, proof['source_sequence']):
        raise ValueError('Protected core receipt does not confirm historical application')


def _limits_processing_receipt(spark, binding, objects, inbound, message_id):
    rows = _find(spark, binding, 'outbox', source_message_id=message_id, kind='processing_receipt')
    if not rows:
        raise SourceLimitsPending('Core processing receipt is not available yet')
    if len(rows) != 1:
        raise ValueError('Ambiguous core processing receipt')
    message = ReceiptMessage.model_validate_json(rows[0]['payload_json'])
    raw = _limits_raw_message(objects, binding, 'processing_receipt', message.message_id,
        'aidp_to_huginn', inbound.source_prefix)
    if raw is None:
        raise SourceLimitsPending('Core processing receipt is not published yet')
    if raw != encoded(message.model_dump(mode='json')):
        raise ValueError('Published core processing receipt differs from its durable outbox')
    return raw


def _limits_publication_facts(spark, binding, ack):
    table = _limits_table(binding, 'publication')
    history = spark.sql('DESCRIBE HISTORY ' + table + ' LIMIT 64').collect()
    for item in history:
        version = item['version']
        if type(version) is not int or version < 0:
            raise ValueError('Invalid Delta publication version')
        rows = spark.sql('SELECT * FROM ' + table + ' VERSION AS OF ' + str(version) +
            ' WHERE ' + _limits_where(binding) + " AND publication_id='" + ack.publication_id + "' LIMIT 2").collect()
        if rows:
            if len(rows) != 1:
                raise ValueError('Ambiguous source limits publication')
            return rows[0].asDict(), _limits_published_row(spark, binding, ack)
    raise ValueError('Source limits ACK publication is outside the bounded history window')


def _limits_published_row(spark, binding, ack):
    version = ack.serving_versions['ai_social_source_limits']
    rows = spark.sql('SELECT * FROM ' + _limits_table(binding, 'source_limits') +
        ' VERSION AS OF ' + str(version) + ' WHERE ' + _limits_where(binding) +
        ' AND catalog_revision=' + str(ack.ref.catalog_revision) + ' LIMIT 2').collect()
    if len(rows) != 1:
        raise ValueError('Source limits ACK lacks its published Delta row')
    actual = rows[0].asDict()
    snapshot = _limits_saved_snapshot(actual, binding)
    if _limits_ref(snapshot, actual['snapshot_sha256']) != ack.ref:
        raise ValueError('Published source limits payload differs from its ACK reference')
    fields = ('registry_id', 'generation_id', 'catalog_revision', 'config_revision',
        'snapshot_sha256', 'configuration_payload_sha256')
    return {key: actual[key] for key in fields} | {'serving_version': version}


def _limits_ack_value(ref, proof, publication):
    receipt = ReceiptMessage.model_validate_json(proof['receipt_raw'])
    return SourceLimitsAck.model_validate({'kind': 'source_limits_ack', 'transport_version': '1.0.0',
        'state': 'applied', 'ref': ref, 'core_configuration_message_id': proof['configuration'].message_id,
        'core_configuration_source_sequence': proof['source_sequence'],
        'core_configuration_raw_sha256': sha256(proof['configuration_raw']),
        'core_configuration_payload_sha256': ref.configuration_payload_sha256,
        'processing_receipt_message_id': receipt.message_id,
        'processing_receipt_raw_sha256': sha256(proof['receipt_raw']),
        'processing_receipt_payload': receipt.payload, 'publication_id': publication['publication_id'],
        'publication_config_revision': publication['config_revision'],
        'serving_versions': json.loads(publication['versions_json'])})


def _limits_check_ack(ack, raw, proof, previous, publication, witness):
    return check_applied_limits_ack(ack, snapshot_raw=raw,
        configuration_raw=proof['configuration_raw'], entry_raw=proof['entry_raw'],
        receipt_raw=proof['receipt_raw'], publication=publication, applied_row=witness,
        previous_snapshot=previous)


def _limits_write_immutable(objects, key, raw):
    try:
        objects.put('artifacts', key, raw, None)
    except Exception:
        saved = objects.get('artifacts', key)
        if saved is None:
            raise
        immutable_limits_write(saved.data, raw)
        return
    saved = objects.get('artifacts', key)
    if saved is None:
        raise RuntimeError('Source limits immutable write is not confirmed')
    immutable_limits_write(saved.data, raw)


def _limits_proof_key(ref):
    return source_limits_prefix(ref.registry_id, ref.generation_id) + 'proofs/' + str(
        ref.catalog_revision) + '-' + ref.snapshot_sha256 + '.json'


def _limits_journal(ack, publication, witness):
    fields = ('registry_id', 'generation_id', 'publication_id', 'config_revision', 'versions_json')
    return {'version': 1, 'ref': ack.ref.model_dump(mode='json'), 'ack': ack.model_dump(mode='json'),
        'publication': {key: publication[key] for key in fields}, 'applied_row': witness}


def _limits_read_journal(blob, ref):
    if not 1 <= len(blob.data) <= 1024 * 1024:
        raise ValueError('Source limits proof journal exceeds its byte bound')
    value = json.loads(blob.data, object_pairs_hook=no_duplicate_keys)
    reject_secrets(value)
    if not isinstance(value, dict) or set(value) != {'version', 'ref', 'ack', 'publication', 'applied_row'}:
        raise ValueError('Unexpected source limits proof journal fields')
    if type(value['version']) is not int or value['version'] != 1 or blob.data != encoded(value):
        raise ValueError('Source limits proof journal requires exact canonical version one bytes')
    if SourceLimitsRef.model_validate(value['ref']) != ref:
        raise ValueError('Source limits proof journal reference mismatch')
    ack = SourceLimitsAck.model_validate(value['ack'])
    if value['ref'] != ref.model_dump(mode='json') or value['ack'] != ack.model_dump(mode='json'):
        raise ValueError('Source limits proof journal typed identities are not canonical')
    if ack.ref != ref or set(value['publication']) != {
            'registry_id', 'generation_id', 'publication_id', 'config_revision', 'versions_json'}:
        raise ValueError('Source limits proof journal identity mismatch')
    return ack, value['publication'], value['applied_row']


def _limits_existing_ack(blob, binding, ref):
    ack = parse_limits_transport(blob.data, expected_sha256=sha256(blob.data), **_limits_scope(binding))
    if not isinstance(ack, SourceLimitsAck) or ack.ref != ref:
        raise ValueError('Existing source limits ACK reference mismatch')
    return ack


def _limits_prepare_ack(spark, binding, objects, snapshot, raw, proof, previous, existing):
    ref = _limits_ref(snapshot, sha256(raw))
    if existing:
        ack = _limits_existing_ack(existing, binding, ref)
    else:
        applied = apply_source_limits(spark, binding, snapshot.model_dump(mode='json'),
            snapshot_sha256=ref.snapshot_sha256, core_message_id=str(proof['configuration'].message_id))
        ack = _limits_ack_value(ref, proof, applied['publication'])
    publication, witness = _limits_publication_facts(spark, binding, ack)
    checked = _limits_check_ack(ack, raw, proof, previous, publication, witness)
    journal = encoded(_limits_journal(checked, publication, witness))
    _limits_write_immutable(objects, _limits_proof_key(ref), journal)
    return checked


def _limits_recover_journal(spark, binding, journal, snapshot, raw, proof, previous):
    ref = _limits_ref(snapshot, sha256(raw))
    rows = _find(spark, binding, 'source_limits', catalog_revision=snapshot.catalog_revision)
    if len(rows) != 1 or _limits_saved_snapshot(rows[0], binding) != snapshot:
        raise ValueError('Prepared source limits proof lacks its durable Delta snapshot')
    actual = tuple(rows[0][key] for key in ('core_configuration_message_id',
        'core_configuration_source_sequence', 'core_configuration_raw_sha256'))
    expected = (str(proof['configuration'].message_id), proof['source_sequence'], sha256(proof['configuration_raw']))
    if actual != expected:
        raise ValueError('Prepared source limits row core identity mismatch')
    ack, publication, witness = _limits_read_journal(journal, ref)
    return _limits_check_ack(ack, raw, proof, previous, publication, witness)


def _limits_apply_one(spark, binding, objects, inbound, snapshot, raw, previous):
    ref = _limits_ref(snapshot, sha256(raw))
    proof = _limits_core_proof(spark, binding, objects, inbound, ref)
    parsed = parse_source_limits(raw, expected_sha256=ref.snapshot_sha256,
        configuration=proof['configuration'].payload, previous=previous, **_limits_scope(binding))
    key = source_limits_ack_key(ref)
    journal = objects.get('artifacts', _limits_proof_key(ref))
    existing = objects.get('artifacts', key)
    if journal:
        checked = _limits_recover_journal(spark, binding, journal, parsed, raw, proof, previous)
    else:
        checked = _limits_prepare_ack(spark, binding, objects, parsed, raw, proof, previous, existing)
    ack_raw = encoded(checked.model_dump(mode='json'))
    if existing:
        _limits_existing_ack(existing, binding, ref)
        immutable_limits_write(existing.data, ack_raw)
    else:
        _limits_write_immutable(objects, key, ack_raw)


def _limits_result(state, count, current, expected):
    return {'state': state, 'applied_revisions': count,
        'catalog_revision': current.catalog_revision if current else 0,
        'expected_catalog_revision': expected}


def _limits_current_state(spark, binding, current, expected):
    if current is None or current.catalog_revision != expected:
        return 'pending'
    rows = spark.sql('SELECT COALESCE(MAX(config_revision),0) AS config_revision FROM ' +
        _limits_table(binding, 'registry') + ' WHERE ' + _limits_where(binding)).collect()
    revision = rows[0]['config_revision'] if len(rows) == 1 else None
    if type(revision) is not int or revision < current.config_revision:
        raise ValueError('Applied source limits lack their historical core revision')
    # Historical ACKs remain valid, but new starts wait for the current core's
    # full policy snapshot rather than treating an older ACK as current approval.
    return 'applied' if revision == current.config_revision else 'pending'


def consume_source_limits(spark, binding, objects, inbound, max_revisions=25):
    """Consume only authenticated protected objects; caller owns the writer lease."""
    checked_binding(binding)
    expected_scope = {**_limits_scope(binding), 'contract_sha256': CORE_CONTRACT_SHA256}
    expected_root = stream_prefix(binding['registry_id'], binding['generation_id'], 'huginn_to_aidp')
    if inbound.scope != expected_scope or inbound.direction != 'huginn_to_aidp' or inbound.root != expected_root:
        raise ValueError('Source limits consumer requires the bound inbound core stream')
    if type(max_revisions) is not int or not 1 <= max_revisions <= 25:
        raise ValueError('Source limits batch must be between one and 25 revisions')
    current_row = _limits_latest_row(spark, binding)
    current = _limits_saved_snapshot(current_row, binding) if current_row else None
    blob = objects.get('artifacts', source_limits_head_key(binding['registry_id'], binding['generation_id']))
    if blob is None:
        if current:
            raise ValueError('Source limits head disappeared after policy application')
        return _limits_result('legacy', 0, None, None)
    head = parse_limits_transport(blob.data, expected_sha256=sha256(blob.data), **_limits_scope(binding))
    if not isinstance(head, SourceLimitsHead):
        raise ValueError('Source limits head object has the wrong document kind')
    chain = _limits_chain(objects, binding, head.ref, current)
    count = 0
    try:
        if current:
            previous = _limits_previous(spark, binding, current)
            _limits_apply_one(spark, binding, objects, inbound, current,
                encoded(current.model_dump(mode='json')), previous)
        for snapshot, raw in chain[:max_revisions]:
            _limits_apply_one(spark, binding, objects, inbound, snapshot, raw, current)
            current, count = snapshot, count + 1
    except SourceLimitsPending:
        return _limits_result('pending', count, current, head.ref.catalog_revision)
    state = _limits_current_state(spark, binding, current, head.ref.catalog_revision)
    return _limits_result(state, count, current, head.ref.catalog_revision)
