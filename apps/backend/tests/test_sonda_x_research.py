"""Synthetic evidence-bound X target checks; no model, Actor or network calls."""
import copy
import unicodedata
from uuid import UUID

import pytest

from app.sonda_contract import Evidence, Observation, encoded, observation_fingerprints, sha256
from app.labs.ai_social_network.notebooks.agent import sonda_x_research as x


def source_query_connector():
    source = {'source_id': str(UUID(int=1)), 'platform': 'x', 'category': 'social',
        'connector_id': 'x', 'connector_revision': 1}
    query = {'query_id': str(UUID(int=2)), 'source_id': source['source_id'],
        'mode': 'posts', 'target': 'Oracle'}
    connector = {'connector_id': 'x', 'revision': 1, 'operation': 'search_posts',
        'modes': ['posts'], 'research_fields': ['target']}
    return source, query, connector


def observation(**changes):
    value = {'identity_version': 1, 'document_type': 'post', 'platform': 'x',
        'provider_id': 'synthetic-x-post', 'parent_document_key': None,
        'url': 'https://x.com/CocaCola/status/123', 'title': 'Opiniones sobre Coca-Cola',
        'text': 'Coca-Cola cambia el precio. Café 2026 y #Mercado_2; @OtroAutor sólo se menciona.',
        'author': '@CocaCola', 'published_at': '2026-01-01T00:00:00Z',
        'observed_at': '2026-01-01T00:00:00Z',
        'metrics': {'likes': None, 'comments': None, 'shares': None, 'views': None},
        'indicators': [], 'capture_scope': 'requested_results', **changes}
    return Observation.model_validate({**value, **observation_fingerprints(value)}).model_dump(mode='json')


def context(item=None, *, quote=None):
    item = item or observation()
    source, query, _ = source_query_connector()
    quote = quote if quote is not None else item['text'][:24]
    evidence = {'evidence_id': sha256(encoded([item['observation_key'], quote])),
        'observation_key': item['observation_key'], 'document_key': item['document_key'],
        'url': item['url'], 'quote': quote, 'captured_at': item['observed_at']}
    return {'observations': [{'source_id': source['source_id'], 'query_id': query['query_id'], 'observation': item}],
        'evidence': [evidence], 'relevant_keys': {item['observation_key']}}


def allowed(target, ctx=None):
    return x.x_research_target_allowed(target, *source_query_connector(), **(ctx if ctx is not None else context()))


@pytest.mark.parametrize('target', ['Coca-Cola', 'coca-cola', 'COCA-COLA', 'Café 2026',
    '#Mercado_2', '@CocaCola', '@COCACOLA'])
def test_relevant_same_case_source_query_literal_phrase_or_canonical_author_allowed(target):
    assert allowed(target)


def test_none_or_exact_fixed_target_preserves_legacy_without_context():
    source, query, connector = source_query_connector()
    connector['research_fields'] = []
    assert x.x_research_target_allowed(None, source, query, connector)
    assert x.x_research_target_allowed(query['target'], source, query, connector)
    assert not x.x_research_target_allowed('Coca-Cola', source, query, connector)


@pytest.mark.parametrize('domain,change', [
    ('source', {'platform': 'instagram'}), ('source', {'connector_id': 'other'}),
    ('source', {'connector_revision': 2}), ('connector', {'connector_id': 'other'}),
    ('connector', {'operation': 'account_posts'}), ('connector', {'research_fields': []}),
    ('query', {'mode': 'comments'}), ('query', {'source_id': str(UUID(int=99))}),
])
def test_only_exact_reviewed_x_search_capability_can_expand(domain, change):
    source, query, connector = source_query_connector()
    {'source': source, 'query': query, 'connector': connector}[domain].update(change)
    assert not x.x_research_target_allowed('Coca-Cola', source, query, connector, **context())


@pytest.mark.parametrize('field', ['observations', 'evidence', 'relevant_keys'])
@pytest.mark.parametrize('missing', [None, []])
def test_missing_context_is_a_denial(field, missing):
    ctx = context()
    ctx[field] = missing
    assert not allowed('Coca-Cola', ctx)


@pytest.mark.parametrize('mutate', [
    lambda ctx: ctx['observations'][0].update(source_id=str(UUID(int=99))),
    lambda ctx: ctx['observations'][0].update(query_id=str(UUID(int=99))),
    lambda ctx: ctx.update(relevant_keys={'0'*64}),
    lambda ctx: ctx['evidence'][0].update(observation_key='0'*64),
    lambda ctx: ctx['evidence'][0].update(document_key='0'*64),
    lambda ctx: ctx['evidence'][0].update(url='https://x.com/other/status/999'),
])
def test_foreign_query_source_relevance_or_evidence_cannot_authorize(mutate):
    ctx = context()
    mutate(ctx)
    assert not allowed('Coca-Cola', ctx)


def test_other_platform_fingerprint_valid_observation_cannot_authorize_x():
    assert not allowed('Coca-Cola', context(observation(platform='instagram')))


@pytest.mark.parametrize('target', ['@OtroAutor', '@unknown', '@', '@1234567890123456', '@bad-name', '@bad.name',
    '@with space', '@café', '@a:from', 'https://x.com/CocaCola', 'Coca-Cola OR Pepsi',
    'Coca-Cola and Pepsi', 'Coca-Cola NOT Pepsi', 'Coca-Cola -Pepsi', 'from:CocaCola',
    '(Coca-Cola)', '"Coca-Cola"', 'Coca-Cola|Pepsi', 'Coca-Cola\nPepsi', 'Coca-Cola\tPepsi',
    'Coca-Cola\x00Pepsi', 'Coca-Cola\u200b', '##', '__', '--', '  ', 'x', 'x'*121])
def test_unknown_author_invalid_handles_operators_urls_and_nonphrase_characters_denied(target):
    # Put each input literally into text to prove character/operator guards are
    # independent of substring matching and do not become valid through evidence.
    item = observation(text='Se observó '+target+' en la publicación.')
    assert not allowed(target, context(item))


@pytest.mark.parametrize('author', ['Coca Cola', 'https://x.com/CocaCola', '@Coca-Cola', '@Coca.Cola', '@CocaCola ', '@@CocaCola'])
def test_display_names_invalid_handles_and_profile_urls_do_not_become_authors(author):
    assert not allowed('@CocaCola', context(observation(author=author)))


@pytest.mark.parametrize('author', ['@a', '@_', '@123456789012345', 'a', '_', '123456789012345', 'CocaCola'])
def test_ascii_author_handle_length_boundaries(author):
    target = author if author.startswith('@') else '@'+author
    assert allowed(target, context(observation(author=author)))


@pytest.mark.parametrize('phrase', ['٢٠٢٦', 'Ⅳ', '½', '２３', '#Mercado_2', 'Coca-Cola'])
def test_unicode_number_categories_and_allowed_phrase_characters(phrase):
    # A phrase still has the common minimum of two characters.
    ctx = context(observation(text='Datos '+phrase+' disponibles.'))
    assert allowed(phrase, ctx) is (len(phrase) >= 2)


def test_nfc_lower_matching_preserves_accents_and_does_not_use_casefold():
    ctx = context(observation(text=unicodedata.normalize('NFD', 'CAFÉ 2026 Straße')))
    assert allowed('café 2026', ctx)
    assert not allowed('cafe 2026', ctx)
    assert not allowed('STRASSE', ctx)
    assert x.x_research_normalized_target('  CAFÉ Straße  ') == 'café straße'
    assert x.x_research_normalized_target(unicodedata.normalize('NFD', 'CAFÉ')) == 'café'


@pytest.mark.parametrize('target', [' Café 2026', 'Café 2026 ', ' @CocaCola', '@CocaCola ', unicodedata.normalize('NFD', 'Café 2026')])
def test_dynamic_target_must_already_be_nfc_without_edge_whitespace(target):
    assert not allowed(target)


def test_bare_author_normalization_uses_only_one_optional_at_sign():
    ctx = context(observation(author='qa_handle'))
    assert allowed('@QA_handle', ctx)
    assert not allowed('@@qa_handle', ctx)
    ctx = context(observation(author='@@qa_handle'))
    assert not allowed('@qa_handle', ctx)


def test_phrase_can_come_from_title_but_cannot_span_title_text_boundary():
    item = observation(text='Texto sin marca.', title='Coca-Cola en el mercado')
    assert allowed('Coca-Cola', context(item, quote='Texto sin marca.'))
    assert not allowed('marca Coca-Cola', context(item, quote='Texto sin marca.'))


@pytest.mark.parametrize('corrupt', ['observation_key', 'content_sha256', 'document_key'])
def test_observation_fingerprints_cannot_be_forged(corrupt):
    ctx = context()
    ctx['observations'][0]['observation'][corrupt] = '0'*64
    assert not allowed('Coca-Cola', ctx)


def test_evidence_quote_must_be_literal_in_one_observation_field_with_valid_fingerprint():
    ctx = context()
    proof = ctx['evidence'][0]
    proof['quote'] = 'Invented quotation'
    proof['evidence_id'] = sha256(encoded([proof['observation_key'], proof['quote']]))
    assert not allowed('Coca-Cola', ctx)
    proof['quote'] = ctx['observations'][0]['observation']['text']+'\n'+ctx['observations'][0]['observation']['title']
    proof['evidence_id'] = sha256(encoded([proof['observation_key'], proof['quote']]))
    assert not allowed('Coca-Cola', ctx)


def test_validated_model_context_supported_but_model_construct_cannot_bypass_revalidation():
    ctx = context()
    ctx['observations'] = [x.CaseResearchObservation.model_validate(ctx['observations'][0])]
    ctx['evidence'] = [Evidence.model_validate(ctx['evidence'][0])]
    assert allowed('@CocaCola', ctx)
    broken = ctx['observations'][0].model_copy(deep=True)
    broken.observation.content_sha256 = '0'*64
    ctx['observations'] = [broken]
    assert not allowed('@CocaCola', ctx)


def test_input_is_not_mutated_and_context_is_bounded():
    ctx = context()
    before = copy.deepcopy(ctx)
    assert allowed('Coca-Cola', ctx)
    assert ctx == before
    ctx['observations'] *= 101
    assert not allowed('Coca-Cola', ctx)


def test_case_research_observation_forbids_untrusted_extra_fields():
    value = context()['observations'][0]
    with pytest.raises(ValueError):
        x.CaseResearchObservation.model_validate({**value, 'decision': 'relevant'})


@pytest.mark.parametrize('text', ['Coca-Cola es excelente.', 'Coca-Cola es decepcionante.', 'Coca-Cola publicó su informe.'])
def test_sentiment_does_not_change_an_already_relevant_case_authorization(text):
    ctx = context(observation(text=text))
    assert allowed('Coca-Cola', ctx)
    ctx['relevant_keys'] = set()
    assert not allowed('Coca-Cola', ctx)
