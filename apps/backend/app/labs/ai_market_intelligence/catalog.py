"""Approved research base and human-reviewed candidates, without Actor creation."""
from __future__ import annotations

import ipaddress
import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .repository import digest, encoded, json_text, utc_now


def public_url(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 2000 or any(ord(c) <= 32 for c in value):
        raise ValueError('A bounded public HTTPS URL is required')
    uri = urlsplit(value)
    host = (uri.hostname or '').encode('idna').decode().lower()
    host = {'twitter.com': 'x.com', 'www.twitter.com': 'x.com', 'www.x.com': 'x.com'}.get(host, host)
    if uri.scheme != 'https' or not host or uri.username or uri.password or uri.port not in (None, 443):
        raise ValueError('Use HTTPS without credentials or custom ports')
    if '.' not in host or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid')):
        raise ValueError('A public hostname is required')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError('Literal IP targets are not permitted')
    query = [(k, v) for k, v in parse_qsl(uri.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in {'fbclid', 'gclid'}]
    return urlunsplit(('https', host, uri.path or '/', urlencode(query), ''))


class Catalog:
    def __init__(self, journal):
        self.journal, self.scope = journal, journal.scope

    def approved_base(self, records, topic_id, permitted_targets=None):
        if records.scope != self.scope:
            raise ValueError('Catalog snapshot belongs to another authenticated scope')
        links = [row for row in records.rows('ai_mi_topic_sources')
                 if row['topic_id'] == topic_id and row['status'] == 'active']
        targets = {row['target_id']: row for row in records.rows('ai_mi_source_targets') if row['status'] == 'active'}
        sources = [row for row in records.rows('ai_mi_sources') if row['status'] == 'active']
        if permitted_targets is not None:
            links = [link for link in links if link['target_id'] in permitted_targets]
            sources = [source for source in sources if any(
                targets[link['target_id']]['source_key'] == source['source_key']
                for link in links if link['target_id'] in targets)]
        return [{'source_key': row['source_key'], 'name': row['name'], 'profile_id': row['connector_revision'],
                 'targets': [targets[link['target_id']] for link in links if link['target_id'] in targets
                             and targets[link['target_id']]['source_key'] == row['source_key']]}
                for row in sources]

    def register_operator_website(self, topic_id, url, user_request_id):
        url = public_url(url)
        if not isinstance(user_request_id, str) or not 1 <= len(user_request_id) <= 128:
            raise ValueError('An explicit authenticated operator decision is required')
        source_id = self.scope.key(digest(['source', 'website']))
        target_id = self.scope.key(digest(['target', source_id, url]))
        def change(records, state):
            existing = records.get('ai_mi_sources', source_id)
            if existing and (existing['connector_revision'] != 'website' or existing['status'] != 'active'):
                raise ValueError('The managed website connector has changed; review it first')
            if not existing:
                records.upsert('ai_mi_sources', {'source_key': source_id, 'name': 'Páginas web', 'platform': 'web',
                    'connector_revision': 'website', 'description': 'Reviewed Website Content Crawler adapter',
                    'allowed_scope_json': json_text({'allow_discovery': False}), 'status': 'active', 'verified_at': utc_now()})
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
            if not source or source['status'] != 'active':
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
            if not source or source['status'] != 'active':
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
