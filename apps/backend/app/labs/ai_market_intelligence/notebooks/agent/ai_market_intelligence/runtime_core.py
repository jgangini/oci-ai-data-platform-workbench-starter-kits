"""Shared identity, URL, conditional storage and provider errors; no business schema."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

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
            raise RuntimeError('Operational snapshot capacity exceeded; this write was not saved. Reconcile any in-flight operation before retrying.')
        condition = {'if_match': etag} if etag else {'if_none_match': '*'}
        try:
            self.storage.put_object(self.namespace, self.bucket, self.key, raw,
                                    content_type='application/json', **condition)
        except Exception as error:
            if getattr(error, 'status', None) == 412:
                raise Conflict('The scoped state changed concurrently') from None
            raise


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(ord(c) <= 32 for c in value):
        raise ValueError('A bounded public HTTPS URL is required')
    uri = urlsplit(value)
    host = (uri.hostname or '').encode('idna').decode().lower().removesuffix('.')
    host = {'twitter.com': 'x.com', 'www.twitter.com': 'x.com', 'www.x.com': 'x.com'}.get(host, host)
    if uri.scheme != 'https' or not host or uri.username or uri.password or uri.port not in (None, 443):
        raise ValueError('Use HTTPS without credentials or custom ports')
    _require_public_hostname(host)
    query = [(k, v) for k, v in parse_qsl(uri.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit(('https', host, uri.path or '/', urlencode(query), ''))


def _require_public_hostname(host):
    if ('.' not in host or len(host) > 253 or host == 'home.arpa'
            or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.'))
            or host.endswith(('.localhost', '.local', '.localdomain', '.internal', '.home.arpa', '.onion', '.test', '.invalid'))
            or re.fullmatch(r'(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+)){0,3}', host, re.I)):
        raise ValueError('A public hostname is required')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Literal IP targets are not permitted')


class ConnectorUnavailable(RuntimeError):
    pass


class ProviderResultUnavailable(ConnectorUnavailable):
    code = 'serp_page_error'

    def __init__(self):
        super().__init__('El buscador informó un error de recuperación; no se obtuvieron resultados verificados. Eso no demuestra ausencia de información pública.')


def research_window(value):
    if not isinstance(value,dict) or set(value)!={'start','end'}:
        raise ValueError('An explicit UTC research window is required')
    start,end=(datetime.fromisoformat(value[key].replace('Z','+00:00')) for key in ('start','end'))
    if any(moment.tzinfo is None or moment.utcoffset()!=timedelta(0) for moment in (start,end)):
        raise ValueError('Research window must be UTC')
    if not timedelta(0)<end-start<=timedelta(days=31):
        raise ValueError('Research window must span at most thirty-one days')
    return start,end


class ProviderPending(RuntimeError):
    pass


class ProviderStatusUnavailable(ProviderPending):
    pass


class ProviderReceiptUnverified(ProviderPending):
    pass


class ProviderStartRejected(ConnectorUnavailable):
    code = 'build_not_found'

    def __init__(self, diagnostic=None):
        super().__init__('La fuente seleccionada no está disponible con su configuración actual. No se inició esa búsqueda; hace falta revisar la conexión.')
        self.diagnostic=diagnostic


class ProviderQueryRejected(ValueError):
    pass


class ProviderStartResponseError(ValueError):
    def __init__(self,diagnostic):
        super().__init__('The provider start response has no verified run receipt')
        self.diagnostic=diagnostic

