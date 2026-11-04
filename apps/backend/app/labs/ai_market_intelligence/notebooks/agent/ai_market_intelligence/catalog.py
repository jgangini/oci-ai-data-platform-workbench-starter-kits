"""Approved research base and human-reviewed candidates, without Actor creation."""
from __future__ import annotations

import ipaddress
import json
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .runtime_core import public_url, _require_public_hostname
from .repository import digest, encoded, json_text, utc_now

PUBLIC_SEARCH_POLICIES = {
    'x_search': ('public-x-search-v1', 'X', 'x', 'Búsqueda pública por tema, sin filtrar por cuenta'),
    'web_search': ('public-web-search-v1', 'Búsqueda web', 'web', 'Descubrimiento de páginas públicas y lectura acotada de tres resultados'),
    'web_search_localized': ('public-web-search-v2', 'Búsqueda web', 'web', 'Búsqueda pública con país, idioma y fechas; lectura selectiva de páginas en un paso separado'),
    'web_search_serp': ('public-web-search-v3', 'Búsqueda web', 'web', 'Una página de hasta diez resultados con país, idioma y fechas solicitados; fragmentos y lectura selectiva en pasos separados'),
    'youtube_search': ('public-youtube-search-v1', 'YouTube', 'youtube', 'Búsqueda de títulos y descripciones de tres videos públicos; sin transcripción ni comentarios'),
}
PUBLIC_SEARCH_PROFILES = tuple(PUBLIC_SEARCH_POLICIES)
CURRENT_PUBLIC_SEARCH_PROFILES = ('x_search', 'web_search_serp', 'youtube_search')
WEB_SELECTION_PHRASE = r'\b(?:en\s+la\s+web|en\s+internet|por\s+internet)\b'






def discovery_permitted(policy, topic_id):
    return policy.get('allow_discovery') is True and ('topic_id' not in policy or policy['topic_id'] == topic_id)


def require_operator_decision(user_request_id):
    if not isinstance(user_request_id, str) or not 1 <= len(user_request_id) <= 128:
        raise ValueError('An explicit authenticated operator decision is required')


class SourceSelectionRequired(ValueError):
    pass


def requested_public_urls(question):
    urls = set()
    for raw in re.findall(r'https?://[^\s<>\]"\x27)]+', question, re.I):
        try:
            urls.add(public_url(raw.rstrip('.,;')))
        except ValueError:
            raise SourceSelectionRequired('Requested URL is outside the public approved scope') from None
    return urls


class Catalog:
    def __init__(self, journal):
        self.journal, self.scope = journal, journal.scope

    def public_search_options(self, records):
        """Expose stable capability names to intake without creating catalog rows."""
        existing = {row['source_key'] for row in records.rows('ai_mi_sources')}
        return [{'source_key': self.scope.key(digest(['runtime-source', policy[0]])), 'name': policy[1]}
                for profile, policy in PUBLIC_SEARCH_POLICIES.items() if profile in CURRENT_PUBLIC_SEARCH_PROFILES
                if self.scope.key(digest(['runtime-source', policy[0]])) not in existing]

    def visible_sources(self, records):
        retired = {self.scope.key(digest(['runtime-source', PUBLIC_SEARCH_POLICIES[profile][0]]))
                   for profile in ('web_search', 'web_search_localized')}
        return [row for row in records.rows('ai_mi_sources') if row['status'] == 'active' and row['source_key'] not in retired]

    def approved_base(self, records, topic_id, permitted_targets=None, permitted_sources=None):
        if records.scope != self.scope:
            raise ValueError('Catalog snapshot belongs to another authenticated scope')
        links = [row for row in records.rows('ai_mi_topic_sources')
                 if row['topic_id'] == topic_id and row['status'] == 'active']
        targets = {row['target_id']: row for row in records.rows('ai_mi_source_targets') if row['status'] == 'active'}
        sources = (self.visible_sources(records) if permitted_sources is None else
                   [row for row in records.rows('ai_mi_sources') if row['status'] == 'active'])
        if permitted_targets is not None:
            links = [link for link in links if link['target_id'] in permitted_targets]
            sources = [source for source in sources if source['source_key'] in (permitted_sources or set()) or any(
                targets[link['target_id']]['source_key'] == source['source_key']
                for link in links if link['target_id'] in targets)]
        base = []
        for row in sources:
            selected = [targets[link['target_id']] for link in links if link['target_id'] in targets
                        and targets[link['target_id']]['source_key'] == row['source_key']]
            discovery = discovery_permitted(json.loads(row['allowed_scope_json'] or '{}'), topic_id)
            if selected or discovery and (permitted_sources is None or row['source_key'] in permitted_sources):
                base.append({'source_key': row['source_key'], 'name': row['name'],
                             'profile_id': row['connector_revision'], 'targets': selected})
        return self.unique_public_search(base, sources, topic_id) if permitted_sources is None else base

    @staticmethod
    def unique_public_search(base, sources, topic_id):
        policies = {row['source_key']: json.loads(row['allowed_scope_json'] or '{}') for row in sources}
        for profile in PUBLIC_SEARCH_PROFILES:
            public = [row for row in base if row['profile_id'] == profile and not row['targets']]
            if len(public) > 1:
                preferred = min(public, key=lambda row: (
                    policies[row['source_key']].get('topic_id') != topic_id,
                    policies[row['source_key']].get('runtime_policy') is not None, row['source_key']))
                base = [row for row in base if row not in public or row['source_key'] == preferred['source_key']]
        return base

    def selected_source(self, records, topic_id, catalog, selected):
        source = next((row for row in catalog if row['source_key'] == selected), None)
        requested = records.get('ai_mi_sources', selected)
        if source is None and requested and requested['status'] == 'active' and requested['connector_revision'] in PUBLIC_SEARCH_PROFILES:
            if discovery_permitted(json.loads(requested['allowed_scope_json'] or '{}'), topic_id):
                source = next((row for row in catalog if row['profile_id'] == requested['connector_revision'] and not row['targets']), None)
        return source, requested['name'] if requested else (source or {}).get('name')

    def managed_web_alias(self, records, source, selected, question):
        if not source or source['profile_id'] not in {'web_search', 'web_search_localized', 'web_search_serp'}:
            return False
        profile = source['profile_id']
        revision = PUBLIC_SEARCH_POLICIES[profile][0]
        key = self.scope.key(digest(['runtime-source', revision]))
        if selected != key or source['source_key'] != key:
            return False
        registered = records.get('ai_mi_sources', key)
        if not registered or registered['status'] != 'active' or registered['connector_revision'] != profile:
            return False
        policy = json.loads(registered['allowed_scope_json'] or '{}')
        if policy.get('runtime_policy') != revision:
            return False
        explicit = re.search(WEB_SELECTION_PHRASE, question, re.I)
        excluded = re.search(
            r'\b(?:no|sin|excepto|salvo|excluye|excluir|evita|evitar|omite|omitir|descarta|descartar)\s+'
            r'(?:(?:quiero(?:\s+que)?\s+)?(?:lo\s+)?(?:busques?|buscar|investigues?|investigar|consultes?|consultar|uses?|usar)\s+)?'
            r'(?:en\s+|por\s+)?(?:la\s+web|internet)\b', question, re.I)
        return bool(explicit) and not excluded

    @staticmethod
    def require_single_web_selection(source, catalog, question):
        remaining = re.sub(WEB_SELECTION_PHRASE, '', question, flags=re.I)
        if any(row['source_key'] != source['source_key'] and row['name'] and
               re.search(r'\b' + re.escape(row['name']) + r'\b', remaining, re.I) for row in catalog):
            raise SourceSelectionRequired('The question selects multiple research sources')

    @staticmethod
    def resolve_requested_source(catalog, urls):
        matches = [row for row in catalog if urls.issubset({target['canonical_url_or_id'] for target in row['targets']})]
        if len(matches) != 1:
            raise SourceSelectionRequired('Requested pages have no unique approved source')
        return matches[0]['source_key']

    def select_for_question(self, records, topic_id, catalog, selected, question, *, source_question=None):
        urls = requested_public_urls(question)
        if urls and not selected:
            selected = self.resolve_requested_source(catalog, urls)
        if not selected:
            return catalog
        source, name = self.selected_source(records, topic_id, catalog, selected)
        wording = question if source_question is None else source_question
        named = source and name and re.search(r'\b' + re.escape(name) + r'\b', wording, re.I)
        aliased = self.managed_web_alias(records, source, selected, wording)
        if aliased:
            self.require_single_web_selection(source, catalog, wording)
        linked = source and urls and urls.issubset({target['canonical_url_or_id'] for target in source['targets']})
        if urls and not linked:
            raise SourceSelectionRequired('Requested pages exceed the selected approved source')
        if not (named or linked or aliased):
            raise SourceSelectionRequired('An explicit source selection must be grounded in the user question')
        if linked:
            source = {**source, 'targets': [target for target in source['targets'] if target['canonical_url_or_id'] in urls]}
        return [source]

    def pointed_base(self, records, urls, selected=None):
        """A planning view of user-supplied pages, never a permanent approval."""
        urls = sorted({public_url(url) for url in urls})
        if not 1 <= len(urls) <= 10:
            raise SourceSelectionRequired('A pointed read requires one to ten public pages')
        key = selected or self.scope.key(digest(['source', 'website']))
        source = records.get('ai_mi_sources', key)
        if not source or source['status'] != 'active' or source['connector_revision'] != 'website':
            raise SourceSelectionRequired('The selected page reader is not active')
        return [{'source_key': key, 'name': source['name'], 'profile_id': 'website',
                 'targets': [], 'pointed_urls': urls}]

    @staticmethod
    def bind_plan_target(plan, sources):
        """Bind the planner's selection to the topic's approved catalog targets."""
        source = next((row for row in sources if row['source_key'] == plan['source_key']), None)
        if source is None:
            return None
        if source['profile_id'] != 'website':
            return plan
        if source.get('discovered_urls') and plan['target_id'] is None:
            return Catalog.bind_discovered_plan(plan, source)
        if source.get('pointed_urls'):
            if plan['target_id'] is not None or {public_url(url) for url in (plan['arguments'] or {}).get('urls', [])} != set(source['pointed_urls']):
                return None
            return plan
        return Catalog.bind_registered_plan(plan, source)

    @staticmethod
    def bind_registered_plan(plan, source):
        target_id = plan['target_id']
        if target_id is None:
            try:
                urls = {public_url(url) for url in (plan['arguments'] or {}).get('urls', [])}
            except ValueError:
                return None
            matches = [target for target in source['targets'] if urls == {target['canonical_url_or_id']}]
            target_id = matches[0]['target_id'] if len(matches) == 1 else None
        if target_id not in {target['target_id'] for target in source['targets']}:
            return None
        return {**plan, 'target_id': target_id}

    @staticmethod
    def bind_discovered_plan(plan, source):
        arguments = plan['arguments'] or {}
        try:
            urls = {public_url(url) for url in arguments.get('urls', [])}
        except ValueError:
            return None
        if (arguments.get('query') or not 1 <= len(urls) <= 3 or not urls.issubset(source['discovered_urls'])
                or len(arguments.get('urls', [])) != len(urls) or arguments.get('max_items') != len(urls)):
            return None
        return plan

    def register_operator_x_search(self, topic_id, user_request_id):
        require_operator_decision(user_request_id)
        source_id = self.scope.key(digest(['source', 'x_search', topic_id]))
        def change(records, state):
            if not records.get('ai_mi_topics', topic_id):
                raise ValueError('An owned research topic is required')
            previous = records.get('ai_mi_sources', source_id)
            policy = json.loads((previous or {}).get('allowed_scope_json') or '{}')
            if previous and (previous['connector_revision'] != 'x_search' or policy.get('topic_id') != topic_id):
                raise ValueError('The managed X source changed; review it first')
            if previous and policy.get('approval', {}).get('request_id') == user_request_id:
                return source_id
            records.upsert('ai_mi_sources', {'source_key': source_id, 'name': 'X', 'platform': 'x',
                'connector_revision': 'x_search', 'description': 'Menciones públicas del tema aprobado',
                'status': 'active', 'allowed_scope_json': json_text({'allow_discovery': True, 'topic_id': topic_id,
                    'approval': {'actor_id': self.scope.owner_id, 'request_id': user_request_id, 'at': utc_now()}})})
            return source_id
        return self.journal.transact(change)

    def ensure_public_x_search(self):
        return self._ensure_public_search('x_search')

    def ensure_public_web_search(self):
        self.ensure_website_reader()
        return self._ensure_public_search('web_search_serp')

    def ensure_public_youtube_search(self):
        return self._ensure_public_search('youtube_search')

    def _ensure_public_search(self, profile):
        """Materialize a reviewed runtime capability, never a discovered account."""
        revision, name, platform, description = PUBLIC_SEARCH_POLICIES[profile]
        source_id = self.scope.key(digest(['runtime-source', revision]))
        policy = {'allow_discovery': True, 'runtime_policy': revision}
        def change(records, state):
            previous = records.get('ai_mi_sources', source_id)
            if previous:
                if previous['connector_revision'] != profile or json.loads(previous['allowed_scope_json'] or '{}') != {'version': 1, **policy}:
                    raise ValueError('Managed public search policy changed; review it first')
                # An operator's disabled state must not be reversed by a question.
                return source_id
            records.upsert('ai_mi_sources', {'source_key': source_id, 'name': name, 'platform': platform,
                'connector_revision': profile, 'description': description,
                'status': 'active', 'allowed_scope_json': json_text(policy)})
            return source_id
        return self.journal.transact(change)

    def _website_reader(self, records):
        source_id = self.scope.key(digest(['source', 'website']))
        existing = records.get('ai_mi_sources', source_id)
        if existing and existing['connector_revision'] != 'website':
            raise ValueError('The managed website connector has changed; review it first')
        if not existing:
            records.upsert('ai_mi_sources', {'source_key': source_id, 'name': 'Páginas web', 'platform': 'web',
                'connector_revision': 'website', 'description': 'Reviewed Website Content Crawler adapter',
                'allowed_scope_json': json_text({'allow_discovery': False}), 'status': 'active'})
        return source_id

    def ensure_website_reader(self):
        return self.journal.transact(lambda records, state: self._website_reader(records))

    def register_operator_website(self, topic_id, url, user_request_id):
        url = public_url(url)
        require_operator_decision(user_request_id)
        source_id = self.scope.key(digest(['source', 'website']))
        target_id = self.scope.key(digest(['target', source_id, url]))
        def change(records, state):
            self._website_reader(records)
            if records.get('ai_mi_sources', source_id)['status'] != 'active':
                raise ValueError('The managed website connector has changed; review it first')
            if not records.get('ai_mi_source_targets', target_id):
                records.upsert('ai_mi_source_targets', {'target_id': target_id, 'source_key': source_id, 'kind': 'web',
                    'canonical_url_or_id': url, 'description': 'Página registrada por el operador', 'approval_revision': 1,
                    'allowed_scope_json': json_text({'urls': [url], 'max_depth': 0}), 'status': 'active'})
            link = self.scope.key(digest(['topic-target', topic_id, target_id]))
            records.upsert('ai_mi_topic_sources', {'topic_source_id': link, 'topic_id': topic_id, 'target_id': target_id,
                'role_priority_json': json_text({'role': 'reference', 'priority': 1}),
                'approved_scope_json': json_text({'urls': [url], 'max_depth': 0}),
                'approval_json': json_text({'actor_id': self.scope.owner_id, 'request_id': user_request_id, 'at': utc_now()}), 'status': 'active'})
            return target_id
        return self.journal.transact(change)

    def propose_target(self, topic_id, source_key, url, description, evidence_ids, *, operator_request_id=None):
        url = public_url(url)
        if not isinstance(description, str) or not 1 <= len(description) <= 1000:
            raise ValueError('Describe how the target helps this research')
        direct = isinstance(operator_request_id, str) and 1 <= len(operator_request_id) <= 128
        if not isinstance(evidence_ids, list) or not (0 if direct else 1) <= len(evidence_ids) <= 20:
            raise ValueError('A candidate needs research evidence')
        target = {'source_key': source_key, 'kind': 'web', 'url': url, 'description': description}
        candidate_id = self.scope.key(digest(['candidate', topic_id, source_key, url]))

        def change(records, state):
            source = records.get('ai_mi_sources', source_key)
            if not source or source['status'] != 'active' or source['connector_revision'] != 'website':
                raise ValueError('Only a reviewed active connector can receive a candidate')
            for evidence_id in evidence_ids:
                evidence = records.get('ai_mi_run_evidence', evidence_id)
                run = records.get('ai_mi_research_runs', evidence['run_id']) if evidence else None
                if not run or run['topic_id'] != topic_id:
                    raise ValueError('Candidate evidence is outside the selected topic')
            previous = records.get('ai_mi_source_candidates', candidate_id)
            if previous:
                return candidate_id
            records.upsert('ai_mi_source_candidates', {'candidate_id': candidate_id, 'topic_id': topic_id,
                'proposed_target_json': json_text(target), 'evidence_ids_json': encoded(sorted(set(evidence_ids))).decode(),
                'quality_frequency_json': json_text({'quality': 'requires_review', 'frequency_is_not_quality': True,
                    'operator_request_id': operator_request_id if direct else None}),
                'state': 'pending'})
            return candidate_id

        return self.journal.transact(change)

    def decide_target(self, candidate_id, presented_hash, accept, user_request_id):
        if type(accept) is not bool or not isinstance(user_request_id, str) or not 1 <= len(user_request_id) <= 128:
            raise ValueError('An explicit authenticated user decision is required')

        def change(records, state):
            candidate = records.get('ai_mi_source_candidates', candidate_id)
            if not candidate or candidate['state'] != 'pending':
                raise ValueError('No pending source candidate')
            target = json.loads(candidate['proposed_target_json'])
            if digest(target) != presented_hash:
                raise ValueError('Source decision does not match the presented candidate')
            decision = {'actor_id': self.scope.owner_id, 'request_id': user_request_id,
                        'presented_hash': presented_hash, 'accepted': accept, 'at': utc_now()}
            if not accept:
                records.upsert('ai_mi_source_candidates', {**candidate, 'state': 'rejected', 'decision_json': json_text(decision)})
                return None
            source = records.get('ai_mi_sources', target['source_key'])
            if not source or source['status'] != 'active' or source['connector_revision'] != 'website':
                raise ValueError('The reviewed connector is no longer active')
            target_id = self.scope.key(digest(['target', target['source_key'], target['url']]))
            previous_target = records.get('ai_mi_source_targets', target_id)
            revision = (previous_target['approval_revision'] or 0) + 1 if previous_target else 1
            records.upsert('ai_mi_source_targets', {'target_id': target_id, 'source_key': target['source_key'],
                'kind': target['kind'], 'canonical_url_or_id': public_url(target['url']),
                'description': target['description'], 'approval_revision': revision, 'status': 'active',
                'allowed_scope_json': json_text({'urls': [public_url(target['url'])], 'max_depth': 0})})
            link_id = self.scope.key(digest(['topic-target', candidate['topic_id'], target_id]))
            records.upsert('ai_mi_topic_sources', {'topic_source_id': link_id, 'topic_id': candidate['topic_id'],
                'target_id': target_id, 'role_priority_json': json_text({'role': 'reference', 'priority': 1}),
                'approved_scope_json': json_text({'urls': [target['url']], 'max_depth': 0}),
                'approval_json': json_text(decision), 'status': 'active'})
            records.upsert('ai_mi_source_candidates', {**candidate, 'state': 'approved',
                'decision_json': json_text(decision), 'approved_target_id': target_id})
            return target_id

        return self.journal.transact(change)
