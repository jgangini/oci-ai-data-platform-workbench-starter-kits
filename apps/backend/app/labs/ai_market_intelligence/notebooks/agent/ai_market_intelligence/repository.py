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

from .schema import definition, identifier, schema_sha256


def encoded(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value) -> str:
    return hashlib.sha256(encoded(value)).hexdigest()


def json_text(value) -> str:
    return encoded({'version': 1, **value}).decode()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class Scope:
    tenant_id: str
    owner_id: str
    generation_id: str

    def __post_init__(self):
        for value in (self.tenant_id, self.owner_id):
            if not isinstance(value, str) or not 1 <= len(value) <= 256 or any(ord(c) < 32 for c in value):
                raise ValueError('Authenticated tenant and owner are required')
        if str(UUID(self.generation_id)) != self.generation_id:
            raise ValueError('A canonical generation UUID is required')

    @property
    def prefix(self):
        owner = digest([self.tenant_id, self.owner_id])
        return f'ai_market_intelligence/generations/{self.generation_id}/owners/{owner}/'

    def key(self, local_id: str) -> str:
        if not isinstance(local_id, str) or not 1 <= len(local_id) <= 256 or any(ord(c) < 32 for c in local_id):
            raise ValueError('A bounded record identity is required')
        return self.generation_id + ':' + local_id


class Conflict(RuntimeError):
    pass


class ObjectSnapshot:
    """One conditional object per scope; deliberately bounded for this pilot."""
    maximum_bytes = 4 * 1024 * 1024

    def __init__(self, storage, namespace: str, bucket: str, scope: Scope):
        self.storage, self.namespace, self.bucket, self.scope = storage, namespace, bucket, scope
        self.key = scope.prefix + 'state.json'

    def read(self):
        try:
            response = self.storage.get_object(self.namespace, self.bucket, self.key)
        except Exception as error:
            if getattr(error, 'status', None) == 404:
                return None, None
            raise
        try:
            raw = response.data.raw.read(self.maximum_bytes + 1)
        finally:
            response.data.close()
        if len(raw) > self.maximum_bytes:
            raise RuntimeError('Operational snapshot capacity exceeded')
        return json.loads(raw), response.headers['etag']

    def write(self, value, etag):
        raw = encoded(value)
        if len(raw) > self.maximum_bytes:
            raise RuntimeError('Operational snapshot capacity exceeded; no paid start admitted')
        condition = {'if_match': etag} if etag else {'if_none_match': '*'}
        try:
            self.storage.put_object(self.namespace, self.bucket, self.key, raw,
                                    content_type='application/json', **condition)
        except Exception as error:
            if getattr(error, 'status', None) == 412:
                raise Conflict('The scoped state changed concurrently') from None
            raise


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


def projection_statements(records: Records, catalog: str, framework_schema: str):
    """Typed MERGE statements; no user/model SQL or unscoped overwrites."""
    for table in records.specs.values():
        rows = records.rows(table['name'])
        if not rows:
            continue
        target = '.'.join(map(identifier, (catalog, framework_schema, table['name'])))
        columns = table['columns']
        primary = next(c['name'] for c in columns if c.get('key') == 'pk')
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
        rows = records.rows(spec['name'])
        if not rows:
            continue
        primary = next(column['name'] for column in spec['columns'] if column.get('key') == 'pk')
        target = '.'.join(map(identifier, (catalog, framework_schema, spec['name'])))
        expected = ['(' + ' AND '.join(identifier(column['name']) + ' <=> ' + _sql_value(row[column['name']], column['type'])
                                      for column in spec['columns']) + ')' for row in rows]
        keys = ','.join(_sql_value(row[primary], 'STRING') for row in rows)
        scope = ' AND '.join(identifier(key) + '=' + _sql_value(getattr(records.scope, key), 'STRING') for key in ('tenant_id', 'owner_id'))
        statement = 'SELECT COUNT(*) AS total_rows, COUNT(DISTINCT ' + identifier(primary) + ') AS distinct_keys, SUM(CASE WHEN ' + ' OR '.join(expected) + ' THEN 1 ELSE 0 END) AS matched_rows FROM ' + target
        statement += ' WHERE ' + scope + ' AND ' + identifier(primary) + ' IN (' + keys + ')'
        yield statement, len(rows)
