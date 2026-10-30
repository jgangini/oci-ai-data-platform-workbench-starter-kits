"""Read-only query-tool projection of the approved Sonda configuration.

The catalog is metadata, not an invocation grant. The broker still checks current
approval, target resolution, criterion permissions, price and available budget.
"""
from urllib.parse import urlsplit
from uuid import UUID
import hashlib
import json
import re


TOOL_CATALOG_COLUMNS = (
    'snapshot_key STRING NOT NULL, config_revision BIGINT NOT NULL, '
    'tool_id STRING NOT NULL, tool_name STRING NOT NULL, '
    'source_id STRING NOT NULL, source_revision BIGINT NOT NULL, '
    'query_id STRING NOT NULL, query_revision BIGINT NOT NULL, '
    'connector_id STRING NOT NULL, connector_revision BIGINT NOT NULL, '
    'provider_kind STRING NOT NULL, operation STRING NOT NULL, category STRING NOT NULL, '
    'platform STRING, description STRING NOT NULL, target STRING NOT NULL, '
    'source_status STRING NOT NULL, query_status STRING NOT NULL, '
    'source_approval_status STRING NOT NULL, query_approval_status STRING NOT NULL, '
    'configured_eligible BOOLEAN NOT NULL, research_allowed BOOLEAN NOT NULL, '
    'max_items BIGINT NOT NULL, max_cost_microusd BIGINT NOT NULL, '
    'max_cost_microusd_per_item BIGINT, '
    'allowed_domains_json STRING NOT NULL, input_schema_json STRING NOT NULL, '
    'metadata_sha256 STRING NOT NULL, payload_json STRING NOT NULL'
)


def tool_catalog_definition():
    """Table-local fields; the writer supplies authenticated scope and location."""
    return {'columns': TOOL_CATALOG_COLUMNS, 'partitions': (), 'layer': 'gold',
        'description': 'Versioned reviewed query tools, eligibility and bounded invocation schemas; not execution authority.'}


def _tool_catalog_encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), allow_nan=False) + '\n').encode('utf-8')


def _tool_catalog_hash(value):
    return hashlib.sha256(_tool_catalog_encoded(value)).hexdigest()


def _tool_catalog_configuration(value, validated):
    if validated:
        if not isinstance(value, dict):
            raise TypeError('Prevalidated configuration must be JSON data')
        return value['payload'] if value.get('kind') == 'configuration' else value
    from app.sonda_contract import ConfigMessage, Configuration, reject_secrets
    if isinstance(value, (ConfigMessage, Configuration)):
        value = value.model_dump(mode='json')
    reject_secrets(value)
    if isinstance(value, dict) and value.get('kind') == 'configuration':
        return ConfigMessage.model_validate(value).payload.model_dump(mode='json')
    return Configuration.model_validate(value).model_dump(mode='json')


def x_research_capable(source, query, connector):
    return (connector['connector_id'] == source['connector_id'] == 'x'
        and source['connector_revision'] == connector['revision']
        and source['platform'] == 'x' and query['source_id'] == source['source_id']
        and query['mode'] == 'posts' and connector['operation'] == 'search_posts'
        and 'target' in connector['research_fields'])


def x_research_override_schema():
    return {'anyOf':[{'type':'string','minLength':2,'maxLength':120,
        'pattern':r'^(?:@[A-Za-z0-9_]{1,15}|[\p{L}\p{N} #_-]{2,120})$',
        'description':'A public @handle or literal phrase from relevant case evidence; no operators or URLs.'},
        {'type':'null'}],'default':None}


def _tool_catalog_input_schema(query, source, connector, max_items, include_x_targets):
    properties = {
        'purpose': {'type': 'string', 'minLength': 1, 'maxLength': 2000},
        'max_items': {'type': 'integer', 'minimum': 1, 'maximum': max_items},
    }
    if include_x_targets and x_research_capable(source, query, connector):
        properties['target_override'] = x_research_override_schema()
    if query['mode'] in ('pages', 'indicators') and 'target' in connector['research_fields']:
        domains = '|'.join(re.escape(domain) for domain in sorted(source['allowed_domains']))
        if domains:
            target_schema = {'type': 'string', 'format': 'uri', 'maxLength': 2000,
                'pattern': r'^https?://(?:' + domains + r')(?::(?:80|443))?(?:[/?][^#\s]*)?$',
                'description': 'Optional public target on an exact reviewed domain; broker DNS and redirect checks still apply.'}
            properties['target_override'] = {'anyOf': [target_schema, {'type': 'null'}], 'default': None}
    return {'type': 'object', 'additionalProperties': False,
        'required': ['purpose', 'max_items'], 'properties': properties}


def _tool_catalog_row(configuration, query, source, connector, include_x_targets):
    # Core parameters may contain provider inputs. They never become tool arguments
    # or replicated payloads; only these explicitly projected fields are served.
    target_url = urlsplit(query['target'])
    if target_url.username is not None or target_url.password is not None:
        raise ValueError('Tool metadata cannot contain URL credentials')
    tool_id = _tool_catalog_hash([str(source['source_id']), str(query['query_id'])])
    # These are conservative static per-execution ceilings, never a reservation or
    # the remaining per-round allowance. Runtime still accounts for prior work.
    max_items = min(query['max_items'], configuration['policy']['max_items_per_round'])
    max_cost = min(query['max_charge_microusd'], configuration['policy']['round_budget_microusd'])
    configured_eligible = (source['status'] == query['status'] == 'active'
        and source['approval']['status'] == query['approval']['status'] == 'approved'
        and source['connector_revision'] == connector['revision'])
    metadata = {
        'snapshot_key': _tool_catalog_hash([configuration['revision'], tool_id]),
        'config_revision': configuration['revision'], 'tool_id': tool_id,
        'tool_name': 'sonda_query_' + UUID(query['query_id']).hex,
        'source_id': str(source['source_id']), 'source_revision': source['revision'],
        'query_id': str(query['query_id']), 'query_revision': query['revision'],
        'connector_id': connector['connector_id'], 'connector_revision': connector['revision'],
        'provider_kind': 'apify', 'operation': connector['operation'],
        'category': source['category'], 'platform': source['platform'],
        'description': query['label'] + ' · ' + source['label'] + ' (' + connector['operation'] + ')',
        'target': query['target'], 'source_status': source['status'], 'query_status': query['status'],
        'source_approval_status': source['approval']['status'], 'query_approval_status': query['approval']['status'],
        'configured_eligible': configured_eligible, 'research_allowed': query['research_allowed'],
        'max_items': max_items, 'max_cost_microusd': max_cost,
        'max_cost_microusd_per_item': connector['max_cost_microusd_per_item'],
        'allowed_domains_json': _tool_catalog_encoded(sorted(source['allowed_domains'])).decode('utf-8'),
        'input_schema_json': _tool_catalog_encoded(_tool_catalog_input_schema(query, source, connector, max_items, include_x_targets)).decode('utf-8'),
    }
    payload = _tool_catalog_encoded({**metadata, 'reviewed_connector': connector})
    return {**metadata, 'metadata_sha256': hashlib.sha256(payload).hexdigest(), 'payload_json': payload.decode('utf-8')}


def tool_catalog_rows(configuration, *, validated=False, include_x_targets=True):
    """One deterministic row per query/revision, including ineligible metadata.

    Accept a Configuration or ConfigMessage dict/DTO. Envelope validation here is
    structural; the caller must authenticate the installation pin and scope before
    injecting them into these rows. Spark may pass validated=True only with JSON
    already parsed by the pinned core contract. No network, provider or model is called.
    include_x_targets=False reproduces the immutable pre-X projection for recovery
    only; new configuration revisions use the current projection by default.
    """
    configuration = _tool_catalog_configuration(configuration, validated)
    sources = {source['source_id']: source for source in configuration['sources']}
    connectors = {connector['connector_id']: connector for connector in configuration['connectors']}
    return [_tool_catalog_row(configuration, query, sources[query['source_id']],
        connectors[sources[query['source_id']]['connector_id']], include_x_targets)
        for query in sorted(configuration['queries'], key=lambda item: UUID(item['query_id']).hex)]
