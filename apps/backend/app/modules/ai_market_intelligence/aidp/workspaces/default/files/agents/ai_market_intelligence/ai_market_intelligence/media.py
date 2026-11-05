"""Bounded media references from retrieved Actor fields; never download media."""
from typing import Annotated, Literal
from urllib.parse import urlsplit
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime_core import public_url


def media_url(value):
    # Query re-encoding breaks CDN signatures. Validate the origin, retain exact bytes.
    if (not isinstance(value, str) or not 1 <= len(value) <= 4096
            or re.search(r'[\s\\\x00-\x1f\x7f]', value)):
        raise ValueError('Invalid media URL')
    uri = urlsplit(value)
    if uri.scheme != 'https' or uri.fragment:
        raise ValueError('Media requires a public HTTPS URL without a fragment')
    public_url('https://' + uri.netloc + '/')
    return value


class MediaAsset(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['image', 'video']
    url: Annotated[str, Field(min_length=1, max_length=4096)]
    delivery: Literal['direct', 'page']
    role: Literal['attachment', 'thumbnail', 'preview']

    @field_validator('url')
    @classmethod
    def public_media(cls, value):
        return media_url(value)


def _path(row, *keys):
    for key in keys:
        row = row.get(key) if isinstance(row, dict) else None
    return row


def _array(value):
    return value if isinstance(value, list) else []


def media_post_text(text, media):
    if text is not None and not isinstance(text, str):
        raise ValueError('Post text must be a retrieved string or unknown')
    if isinstance(text, str) and text.strip():
        return text, 'post_text'
    if media['media']:
        return 'Publicación sin texto recuperado; contiene medios enlazados cuyo contenido no se ha analizado.', 'media_metadata'
    return None, 'post_text'


def media_limitations(provenance):
    limits = []
    if provenance.get('media'):
        limits.append('Los medios se entregan como enlaces recuperados; no se analizó su contenido visual o audiovisual y las URLs del proveedor pueden caducar.')
    if provenance.get('media_omitted_count'):
        limits.append('Se omitieron enlaces de medios inválidos o que exceden el límite de ocho por publicación.')
    return limits


def _instagram_media(row, add):
    for post in [row, *_array(row.get('childPosts'))]:
        if not isinstance(post, dict):
            continue
        video = post.get('videoUrl')
        add('video', video)
        add('image', post.get('displayUrl'), 'thumbnail' if video or post.get('type') == 'Video' else 'attachment')
    for url in _array(row.get('images')):
        add('image', url)


def _facebook_video(item, add):
    direct = (_path(item, 'videoDeliveryLegacyFields', 'browser_native_hd_url')
              or _path(item, 'videoDeliveryLegacyFields', 'browser_native_sd_url'))
    add('video', direct or item.get('permalink_url') or item.get('url'), delivery='direct' if direct else 'page')


def _facebook_media(row, add):
    for entry in _array(row.get('media')):
        if not isinstance(entry, dict):
            continue
        nested = _path(entry, 'video_grid_renderer', 'video')
        item = nested if isinstance(nested, dict) else entry
        video = item.get('__typename') == 'Video'
        if video:
            _facebook_video(item, add)
        image = (_path(item, 'image', 'uri') or _path(item, 'thumbnailImage', 'uri')
                 or _path(item, 'preferred_thumbnail', 'image', 'uri') or item.get('thumbnail'))
        add('image', image, 'thumbnail' if video else 'attachment')


def _x_video(item, add):
    variants = _array(_path(item, 'video_info', 'variants'))
    mp4 = [v for v in variants if isinstance(v, dict) and v.get('content_type') == 'video/mp4']
    best = max(mp4, key=lambda v: v.get('bitrate') if type(v.get('bitrate')) is int else 0, default={})
    direct = best.get('url')
    add('video', direct or item.get('expanded_url'), delivery='direct' if direct else 'page')


def _x_media(row, add):
    entries = _array(_path(row, 'extendedEntities', 'media')) or _array(_path(row, 'entities', 'media'))
    for item in entries:
        if not isinstance(item, dict):
            continue
        video = item.get('type') in {'video', 'animated_gif'}
        if video:
            _x_video(item, add)
        add('image', item.get('media_url_https'), 'thumbnail' if video else 'attachment')
    # Use the flat fallback only when typed entries are absent (video posters are thumbnails).
    if not entries:
        for url in _array(row.get('media')):
            add('image', url)


def _youtube_media(row, add):
    add('video', row.get('url'), delivery='page')
    add('image', row.get('thumbnailUrl'), 'thumbnail')


def _website_media(row, add):
    kinds = {'og:image': 'image', 'og:image:url': 'image', 'og:image:secure_url': 'image',
             'og:video': 'video', 'og:video:url': 'video', 'og:video:secure_url': 'video'}
    for item in _array(_path(row, 'metadata', 'openGraph')):
        if not isinstance(item, dict):
            continue
        kind = kinds.get(item.get('property'))
        if kind:
            add(kind, item.get('content'), 'preview', 'direct' if kind == 'image' else 'page')


def retrieved_media(row, source):
    candidates = []

    def add(kind, url, role='attachment', delivery='direct'):
        if url is not None:
            candidates.append(dict(kind=kind, url=url, role=role, delivery=delivery))

    extract = {'instagram': _instagram_media, 'facebook': _facebook_media, 'x': _x_media,
               'youtube': _youtube_media, 'web': _website_media}.get(source)
    if extract:
        extract(row, add)
    assets, seen, omitted = [], set(), 0
    for candidate in candidates:
        try:
            asset = MediaAsset.model_validate(candidate).model_dump(mode='json')
        except ValueError:
            omitted += 1
            continue
        identity = (asset['kind'], asset['url'])
        if identity in seen:
            continue
        seen.add(identity)
        if len(assets) == 8:
            omitted += 1
        else:
            assets.append(asset)
    return {'media': assets, 'media_omitted_count': omitted, 'media_content_analyzed': False}


def model_evidence(value):
    """Models cite evidence IDs. Signed media destinations stay in the public manifest."""
    if isinstance(value, list):
        return [model_evidence(item) for item in value]
    if isinstance(value, dict):
        result = {key: model_evidence(item) for key, item in value.items() if key != 'media'}
        if 'media' in value:
            result['media_summary'] = {'count': len(value['media']),
                                       'kinds': sorted({item['kind'] for item in value['media']})}
        return result
    return value
