"""Owner-scoped records and conditional storage for the development agent.

The small operational snapshot is authoritative for admissions and approvals.
Delta tables are queryable projections, never the paid-effect lock.
"""
from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import UUID

from .runtime_core import encoded, digest, json_text, utc_now, Scope, Conflict, ObjectSnapshot
from .schema import definition, identifier, schema_sha256














class ProjectionPending(RuntimeError):
    def __init__(self, reason):
        messages = {'busy': 'Another projection owns this scope; retry delivery, not capture',
                    'changed': 'Records changed during projection; retry delivery without recapture'}
        self.reason = reason
        super().__init__(messages[reason])




class Records:
    def __init__(self, scope: Scope, tables=None):
        self.scope = scope
        self.specs = {table['name']: table for table in definition()['tables']}
        self.tables = copy.deepcopy({name: {} for name in self.specs} if tables is None else tables)
        if set(self.tables) != set(self.specs):
            raise ValueError('Unexpected scoped table set')
        for table, rows in self.tables.items():
            for key, row in rows.items():
                if self._validate(table, row) != key:
                    raise ValueError('Record key differs from its primary identity')
        for table, rows in self.tables.items():
            for row in rows.values():
                self._validate_references(table, row)

    def _validate(self, table, row):
        spec = self.specs[table]
        columns = {column['name']: column for column in spec['columns']}
        if set(row) != set(columns):
            raise ValueError('Record fields differ from the approved model: ' + table)
        if (row['tenant_id'], row['owner_id']) != (self.scope.tenant_id, self.scope.owner_id):
            raise ValueError('Cross-owner record rejected')
        for name, column in columns.items():
            _validate_value(column, row[name])
        primary = next(column['name'] for column in spec['columns'] if column.get('key') == 'pk')
        if not row[primary].startswith(self.scope.generation_id + ':') or row[primary] == self.scope.generation_id + ':':
            raise ValueError('Record identity must belong to the bound generation')
        return row[primary]

    def upsert(self, table, values):
        spec = self.specs[table]
        row = {column['name']: None for column in spec['columns']}
        row.update(tenant_id=self.scope.tenant_id, owner_id=self.scope.owner_id)
        row.update(values)
        key = self._validate(table, row)
        self._validate_references(table, row)
        self.tables[table][key] = copy.deepcopy(row)
        return row

    def _validate_references(self, table, row):
        for column in self.specs[table]['columns']:
            reference = column.get('references')
            if reference and row[column['name']] is not None:
                target, field = reference.split('.')
                if not any(item[field] == row[column['name']] for item in self.tables[target].values()):
                    raise ValueError('Missing scoped reference: ' + reference)

    def get(self, table, key):
        return copy.deepcopy(self.tables[table].get(key))

    def rows(self, table):
        return copy.deepcopy(list(self.tables[table].values()))


def _validate_value(column, value):
    if value is None:
        if not column['nullable']:
            raise ValueError('Required field: ' + column['name'])
        return
    if column['type'] == 'BIGINT':
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise ValueError('Invalid integer: ' + column['name'])
    elif not isinstance(value, str) or len(value.encode('utf-8')) > 262144:
        raise ValueError('Invalid string: ' + column['name'])
    if column['type'] == 'TIMESTAMP':
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
            raise ValueError('Timestamp must be UTC: ' + column['name'])


class Journal:
    def __init__(self, snapshot: ObjectSnapshot, budget_microusd: int):
        if type(budget_microusd) is not int or budget_microusd < 0:
            raise ValueError('A verified nonnegative budget is required')
        self.snapshot, self.scope, self.budget = snapshot, snapshot.scope, budget_microusd

    def read(self):
        value, etag = self.snapshot.read()
        if value is None:
            value = {'version': 1, 'revision': 0, 'schema_sha256': schema_sha256(),
                     'scope': self.scope.__dict__, 'budget_microusd': self.budget,
                     'tables': Records(self.scope).tables, 'operations': {}}
        if (value.get('scope') != self.scope.__dict__ or value.get('schema_sha256') != schema_sha256()
                or value.get('budget_microusd') != self.budget or value.get('version') != 1):
            raise RuntimeError('Scoped journal binding changed')
        Records(self.scope, value['tables'])
        return value, etag

    def transact(self, change):
        for _ in range(5):
            value, etag = self.read()
            records = Records(self.scope, value['tables'])
            result = change(records, value)
            value['tables'] = records.tables
            value['revision'] += 1
            if 'projection_receipts' in value and len(encoded(value)) > getattr(self.snapshot, 'maximum_bytes', ObjectSnapshot.maximum_bytes):
                value.pop('projection_receipts')
            try:
                self.snapshot.write(value, etag)
                return result
            except Conflict:
                continue
        raise Conflict('Concurrent updates prevented this change; no external effect started')

    def recover_projection(self, expected_lease, expected_revision, *, writer_stopped=False):
        """Administrative recovery only after native writer/SQL termination is verified."""
        if writer_stopped is not True or not isinstance(expected_lease, str) or not expected_lease:
            raise ValueError('Verified writer termination and the exact orphan lease are required')
        value, etag = self.read()
        if value['revision'] != expected_revision or value.get('projection_lease') != expected_lease:
            raise Conflict('Projection state changed; do not release another writer')
        value['projection_lease'] = None
        value['revision'] += 1
        self.snapshot.write(value, etag)

    def claim_projection(self, lease):
        def claim(records, state):
            if state.get('projection_lease'):
                raise ProjectionPending('busy')
            state['projection_lease'] = lease
            # Invalidate before SQL so a terminated writer cannot leave reusable receipts.
            return records, state.pop('projection_receipts', [])
        return self.transact(claim)

    def finish_projection(self, lease, projected, receipts=None):
        if projected.scope != self.scope:
            raise ValueError('Projection records belong to another scope')
        def release(current, state):
            if state.get('projection_lease') != lease:
                raise RuntimeError('Projection ownership changed')
            state['projection_lease'] = None
            unchanged = current.tables == projected.tables
            # Receipts describe the verified snapshot, never newer journal rows.
            if receipts is not None:
                candidate = {**state, 'projection_receipts': receipts, 'revision': state['revision'] + 1}
                maximum = getattr(self.snapshot, 'maximum_bytes', ObjectSnapshot.maximum_bytes)
                if len(encoded(candidate)) <= maximum:
                    state['projection_receipts'] = receipts
            return unchanged
        return self.transact(release)


def projection_delta(records: Records, catalog: str, framework_schema: str, compute_key: str, previous):
    """Select changed rows using destination-bound receipts of prior exact readbacks."""
    binding = digest(['projection-rows-v2', records.scope.__dict__, catalog, framework_schema,
                      compute_key, schema_sha256()])
    current, row_keys = {}, {}
    verified = _projection_row_receipts(previous, binding)
    for spec in records.specs.values():
        primary = next(column['name'] for column in spec['columns'] if column.get('key') == 'pk')
        dirty = set()
        for row in records.rows(spec['name']):
            key, fingerprint = digest([spec['name'], row[primary]]), digest(row)
            current[key] = fingerprint
            if verified.get(key) != fingerprint:
                dirty.add(row[primary])
        row_keys[spec['name']] = dirty
    return row_keys, {'version': 2, 'binding': binding, 'rows': current}


def _projection_row_receipts(previous, binding):
    if (not isinstance(previous, dict) or set(previous) != {'version', 'binding', 'rows'}
            or previous['version'] != 2 or previous['binding'] != binding
            or not isinstance(previous['rows'], dict)):
        return {}
    for key, fingerprint in previous['rows'].items():
        if not all(isinstance(value, str) and len(value) == 64 and set(value) <= set('0123456789abcdef')
                   for value in (key, fingerprint)):
            return {}
    return previous['rows']


def _projection_rows(records, spec, row_keys):
    primary = next(column['name'] for column in spec['columns'] if column.get('key') == 'pk')
    rows = records.rows(spec['name'])
    if row_keys is not None:
        rows = [row for row in rows if row[primary] in row_keys.get(spec['name'], set())]
    return primary, rows


def projection_statements(records: Records, catalog: str, framework_schema: str, *, row_keys=None):
    """Typed MERGE statements; no user/model SQL or unscoped overwrites."""
    for table in records.specs.values():
        primary, rows = _projection_rows(records, table, row_keys)
        if not rows:
            continue
        target = '.'.join(map(identifier, (catalog, framework_schema, table['name'])))
        columns = table['columns']
        selects = ['SELECT ' + ', '.join(_sql_value(row[c['name']], c['type']) + ' AS ' + identifier(c['name'])
                                        for c in columns) for row in rows]
        match = ' AND '.join('t.' + identifier(name) + '=s.' + identifier(name)
                             for name in ('tenant_id', 'owner_id', primary))
        yield 'MERGE INTO ' + target + ' t USING (' + ' UNION ALL '.join(selects) + ') s ON ' + match + ' WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *'


def _sql_value(value, sql_type):
    if value is None:
        return 'CAST(NULL AS ' + sql_type + ')'
    if sql_type == 'BIGINT':
        return str(value)
    # UTF-8 hex never introduces quotes, delimiters or executable SQL.
    text = "decode(unhex('" + value.encode('utf-8').hex() + "'),'UTF-8')"
    return 'CAST(' + text + ' AS TIMESTAMP)' if sql_type == 'TIMESTAMP' else text


def projection_checks(records: Records, catalog: str, framework_schema: str):
    """Read back exact scoped values; a successful HTTP/tool envelope is insufficient."""
    for spec in records.specs.values():
        primary, rows = _projection_rows(records, spec, None)
        if not rows:
            continue
        target = '.'.join(map(identifier, (catalog, framework_schema, spec['name'])))
        expected = ['(' + ' AND '.join(identifier(column['name']) + ' <=> ' + _sql_value(row[column['name']], column['type'])
                                      for column in spec['columns']) + ')' for row in rows]
        keys = ','.join(_sql_value(row[primary], 'STRING') for row in rows)
        scope = ' AND '.join(identifier(key) + '=' + _sql_value(getattr(records.scope, key), 'STRING') for key in ('tenant_id', 'owner_id'))
        statement = 'SELECT COUNT(*) AS total_rows, COUNT(DISTINCT ' + identifier(primary) + ') AS distinct_keys, SUM(CASE WHEN ' + ' OR '.join(expected) + ' THEN 1 ELSE 0 END) AS matched_rows FROM ' + target
        statement += ' WHERE ' + scope + ' AND ' + identifier(primary) + ' IN (' + keys + ')'
        yield statement, len(rows)


def projection_batch_check(checks):
    """Batch trusted projection_checks output without cancelling errors across tables."""
    if not isinstance(checks, list) or not 1 <= len(checks) <= 14:
        raise ValueError('A projection batch requires one to fourteen checks')
    parts = []
    for index, check in enumerate(checks):
        if not isinstance(check, (tuple, list)) or len(check) != 2:
            raise ValueError('A projection check requires SQL and its expected row count')
        statement, expected = check
        if not isinstance(statement, str) or not statement.strip() or type(expected) is not int or expected < 1:
            raise ValueError('A projection check requires SQL and a positive integer row count')
        parts.append(f'SELECT {index} AS check_id, CASE WHEN total_rows = {expected}'
                     f' AND distinct_keys = {expected} AND matched_rows = {expected}'
                     f' THEN 1 ELSE 0 END AS check_ok FROM ({statement}) AS ai_mi_check_{index}')
    statement = ('SELECT COUNT(*) AS total_rows, COUNT(DISTINCT check_id) AS distinct_keys, '
                 'COALESCE(SUM(CASE WHEN check_ok = 1 THEN 1 ELSE 0 END), 0) AS matched_rows '
                 'FROM (' + ' UNION ALL '.join(parts) + ') AS ai_mi_projection_checks')
    return statement, len(checks)
