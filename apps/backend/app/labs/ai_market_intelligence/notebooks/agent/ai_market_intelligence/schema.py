"""Read and validate the installed research data contract; never provision it."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

SCHEMA_PATH = Path(__file__).with_name('schema.json')
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,126}\Z')
TYPES = frozenset({'STRING', 'BIGINT', 'TIMESTAMP'})


def identifier(value: str) -> str:
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('Expected a plain SQL identifier')
    return '`' + value + '`'


def literal(value: str) -> str:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        raise ValueError('Invalid metadata literal')
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def _validate_column(column: dict) -> None:
    identifier(column['name'])
    if column['type'] not in TYPES or type(column['nullable']) is not bool:
        raise ValueError('Invalid column definition')
    if not isinstance(column.get('description'), str) or not column['description'].strip():
        raise ValueError('Every column requires a description')
    literal(column['description'])


def _validate_table(table: dict) -> set[str]:
    identifier(table['name'])
    if not table['name'].startswith('ai_mi_') or table['layer'] != 'framework':
        raise ValueError('Table outside the market model')
    literal(table['description'])
    names = [column['name'] for column in table['columns']]
    if len(names) != len(set(names)):
        raise ValueError('Duplicate column')
    if not {'tenant_id', 'owner_id'}.issubset(names):
        raise ValueError('Missing authenticated scope')
    for column in table['columns']:
        _validate_column(column)
    return set(names)


def definition() -> dict:
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    if schema.get('schema_version') != 2 or schema.get('module_id') != 'ai_market_intelligence':
        raise ValueError('Unsupported market schema')
    tables = schema.get('tables', [])
    names = [table['name'] for table in tables]
    if len(names) != 14 or len(set(names)) != 14:
        raise ValueError('The approved model requires fourteen unique tables')
    fields = {table['name']: _validate_table(table) for table in tables}
    for table in tables:
        for column in table['columns']:
            reference = column.get('references')
            if reference:
                target, field = reference.split('.')
                if field not in fields.get(target, set()):
                    raise ValueError('Reference outside the approved model')
    return schema


def schema_sha256() -> str:
    raw = json.dumps(definition(), ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()
    return hashlib.sha256(raw).hexdigest()
