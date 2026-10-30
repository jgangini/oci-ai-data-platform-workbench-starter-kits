"""Evidence-bound X research targets; pure checks, never a provider operation.

The caller supplies observations, evidence and relevance decisions from one
authenticated case. Normalization is for matching/deduplication only: it does not
rewrite the Actor argument. Other source capabilities remain denied by default.
"""
import re
import unicodedata
from typing import Annotated
from uuid import UUID

from pydantic import Field, TypeAdapter

from app.sonda_contract import Evidence, Hash, Observation, Record
from app.labs.ai_social_network.notebooks.sonda_tools import x_research_capable


class CaseResearchObservation(Record):
    source_id: UUID
    query_id: UUID
    observation: Observation


def x_research_normalized_target(target):
    """Match Huginn's NFC/lower/strip semantics without removing accents."""
    if not isinstance(target, str):
        raise TypeError('X research targets must be strings')
    return unicodedata.normalize('NFC', target).lower().strip()


def x_research_phrase_allowed(target):
    if not isinstance(target, str) or not 2 <= len(target) <= 120:
        return False
    if target != unicodedata.normalize('NFC', target) or target != target.strip():
        return False
    text = unicodedata.normalize('NFC', target).strip()
    if len(text) < 2:
        return False
    if any(not (unicodedata.category(char)[0] in ('L', 'N') or char in ' #_-') for char in text):
        return False
    if not any(unicodedata.category(char)[0] in ('L', 'N') for char in text):
        return False
    tokens = text.lower().split(' ')
    return not any(token in ('or', 'and', 'not') or token.startswith('-') for token in tokens)


def x_research_checked_context(observations, evidence, relevant_keys):
    """Revalidate typed/native JSON so model_construct cannot bypass fingerprints."""
    rows = TypeAdapter(Annotated[list[CaseResearchObservation], Field(max_length=100)]).validate_python(observations)
    rows = [CaseResearchObservation.model_validate(row.model_dump(mode='json')) for row in rows]
    proofs = [item.model_dump(mode='json') if isinstance(item, Evidence) else item for item in evidence]
    proofs = TypeAdapter(Annotated[list[Evidence], Field(max_length=100)]).validate_python(proofs)
    keys = TypeAdapter(Annotated[list[Hash], Field(max_length=100)]).validate_python(list(relevant_keys))
    return rows, proofs, set(keys)


def x_research_matching_observations(source, query, observations, evidence, relevant_keys):
    rows, proofs, keys = x_research_checked_context(observations, evidence, relevant_keys)
    for row in rows:
        observation = row.observation
        if str(row.source_id) != str(source['source_id']) or str(row.query_id) != str(query['query_id']):
            continue
        if observation.platform != 'x' or observation.observation_key not in keys:
            continue
        for proof in proofs:
            if (proof.observation_key == observation.observation_key and proof.document_key == observation.document_key
                and proof.url == observation.url and proof.quote.strip()
                and any(proof.quote in text for text in (observation.text, observation.title or ''))):
                yield observation
                break


def x_research_observation_matches(normalized, handle, observation):
    if handle:
        author = observation.author
        return (isinstance(author, str) and re.fullmatch(r'@?[A-Za-z0-9_]{1,15}', author) is not None
            and normalized.removeprefix('@') == x_research_normalized_target(author).removeprefix('@'))
    return any(normalized in x_research_normalized_target(text) for text in (observation.text, observation.title or ''))


def x_research_checked_target(target):
    """Classify canonical dynamic syntax separately from its evidence authority."""
    if not isinstance(target, str) or not 1 <= len(target) <= 120:
        return None
    if target != unicodedata.normalize('NFC', target) or target != target.strip():
        return None
    normalized = x_research_normalized_target(target)
    handle = re.fullmatch(r'@[A-Za-z0-9_]{1,15}', normalized) is not None
    if not handle and not x_research_phrase_allowed(target):
        return None
    return normalized, handle


def x_research_target_allowed(target, source, query, connector, observations=None,
        evidence=None, relevant_keys=None):
    """Allow reviewed X expansion only from relevant evidence of the same source/query.

    None and the exact configured target retain their existing fixed-target path.
    Dynamic author handles must be present as canonical handle/@handle metadata;
    phrases must occur literally after NFC/lower normalization in text or title.
    Missing or invalid context is a denial, never an unbounded search grant.
    """
    if target is None or (isinstance(target, str) and target == query.get('target')):
        return True
    if not x_research_capable(source, query, connector):
        return False
    checked = x_research_checked_target(target)
    if checked is None:
        return False
    normalized, handle = checked
    if observations is None or evidence is None or relevant_keys is None:
        return False
    try:
        return any(x_research_observation_matches(normalized, handle, observation)
            for observation in x_research_matching_observations(source, query, observations, evidence, relevant_keys))
    except (ValueError, TypeError, KeyError):
        return False
