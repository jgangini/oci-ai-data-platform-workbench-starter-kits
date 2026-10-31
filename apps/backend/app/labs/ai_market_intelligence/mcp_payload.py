"""Decode MCP protocol envelopes without treating a stub as provider evidence."""
import json


def _protocol_text_blocks(value):
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) and item.get('type') == 'text'
        and set(item).issubset({'type', 'text', 'annotations', '_meta'}) for item in value)


def unpack(value):
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, str):
        try:
            return unpack(json.loads(value))
        except ValueError:
            return value
    if _protocol_text_blocks(value):
        return [unpack(item) for item in value]
    if isinstance(value, dict):
        if value.get('isError') or value.get('error'):
            raise RuntimeError('MCP returned an error envelope')
        if value.get('structuredContent') is not None:
            return value['structuredContent']
        if 'content' in value:
            if not isinstance(value['content'], list):
                raise ValueError('Invalid MCP content envelope')
            parts = [unpack(part) for part in value['content']]
            return parts[0] if len(parts) == 1 else parts
        if value.get('type') == 'text' and 'text' in value:
            return unpack(value['text'])
        return value
    return value


def locate(value, names):
    if isinstance(value, dict):
        for name in names:
            if value.get(name) is not None:
                return value[name]
        for item in value.values():
            found = locate(item, names)
            if found is not None:
                return found
    if isinstance(value, list):
        for item in value:
            found = locate(item, names)
            if found is not None:
                return found
    return None


def dataset_id(receipt):
    direct = locate(receipt, ('defaultDatasetId', 'datasetId'))
    if direct:
        return direct
    storages = locate(receipt, ('storages',))
    if isinstance(storages, dict):
        return storages.get('datasets', {}).get('default', {}).get('id')
    return None
