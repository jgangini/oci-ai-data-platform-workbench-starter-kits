"""Generic web wording selects only the reviewed, already approved capability."""
import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.catalog import SourceSelectionRequired
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.repository import json_text
from test_market_intelligence_workflow import configured, TOPIC, SCOPE


@pytest.fixture
def selection():
    flow, _, provider, _ = configured()
    key = flow.catalog.ensure_public_web_search()
    topic = flow.research.record_question('web-selection-topic', 'Novedades', TOPIC)[1]
    def choose(question, selected=None):
        records = flow.records()
        return flow.catalog.select_for_question(records, topic, flow.catalog.approved_base(records, topic),
                                                key if selected is None else selected, question)
    return flow, provider, key, topic, choose


@pytest.mark.parametrize('question', [
    'Los últimos siete días. Investiga en la web la supuesta alianza entre Nu y Tiendas D1 en Colombia.',
    'Los últimos siete días. Investiga en la web la supuesta alianza entre Nu y Tiendas D1 en Colombia mencionada en las publicaciones; distingue cuándo ocurrió de cuándo se comenta y explica qué quedó corroborado, qué no y la cobertura de las páginas leídas.',
    'Busca en la web qué se confirma y qué no.',
    'Investiga en internet la alianza de Nu y D1 en Colombia.',
    'Busca por internet las novedades de D1.',
    'No tengo datos. Busca EN LA WEB las novedades de D1.',
])
def test_actual_user_web_phrase_selects_the_managed_active_catalog_source(selection, question):
    flow, provider, key, _, choose = selection
    before = flow.journal.read()[0]
    result = choose(question)
    assert len(result) == 1 and result[0]['source_key'] == key
    assert result[0]['profile_id'] == 'web_search_serp' and result[0]['targets'] == []
    assert flow.journal.read()[0] == before and not provider.starts


@pytest.mark.parametrize('question', ['Investiga las novedades de D1.', 'Consulta un sitio de D1.',
    'No busques en la web; usa otra fuente.', 'Investiga sin buscar en internet.',
    'No quiero investigar en la web las novedades de D1.',
    'No quiero buscar en internet las novedades de D1.',
    'No quiero que investigues en la web las novedades de D1.',
    'Evita investigar en la web.', 'Busca novedades, excepto en la web.',
    'Descarta en internet las consultas de D1.', 'No en la web, solo en X.'])
def test_unmatched_negated_or_excluded_web_wording_does_not_ground_a_model_selection(selection, question):
    _, provider, _, _, choose = selection
    with pytest.raises(SourceSelectionRequired):
        choose(question)
    assert not provider.starts


@pytest.mark.parametrize('change', ['inactive', 'policy', 'profile', 'unapproved'])
def test_alias_never_reactivates_or_admits_a_changed_or_unapproved_capability(selection, change):
    flow, provider, key, topic, choose = selection
    def alter(records, state):
        row = records.get('ai_mi_sources', key)
        changes = {'inactive': {'status': 'inactive'},
                   'policy': {'allowed_scope_json': json_text({'allow_discovery': True, 'runtime_policy': 'unknown'})},
                   'profile': {'connector_revision': 'x_search'},
                   'unapproved': {'allowed_scope_json': json_text({'allow_discovery': False})}}
        records.upsert('ai_mi_sources', {**row, **changes[change]})
    flow.journal.transact(alter)
    before = flow.journal.read()[0]
    with pytest.raises(SourceSelectionRequired):
        choose('Investiga en la web la alianza de Nu y D1.')
    assert flow.journal.read()[0] == before and not provider.starts


@pytest.mark.parametrize('profile', ['web_search', 'web_search_serp', 'x_search', 'youtube_search', 'website'])
def test_generic_alias_does_not_ground_a_custom_source_even_with_a_similar_registered_name(selection, profile):
    flow, _, _, _, choose = selection
    key = SCOPE.key('custom-web-name')
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        'source_key': key, 'name': 'Búsqueda web', 'status': 'active', 'connector_revision': profile,
        'allowed_scope_json': json_text({'allow_discovery': True})}))
    with pytest.raises(SourceSelectionRequired):
        choose('Investiga en la web las novedades de D1.', key)


def test_a_custom_selected_key_cannot_borrow_the_managed_capability_alias(selection):
    flow, _, managed, topic, _ = selection
    custom = SCOPE.key('equivalent-custom-web-search')
    flow.journal.transact(lambda records, state: records.upsert('ai_mi_sources', {
        'source_key': custom, 'name': 'Custom research', 'status': 'active', 'connector_revision': 'web_search',
        'allowed_scope_json': json_text({'allow_discovery': True})}))
    records = flow.records()
    approved = flow.catalog.approved_base(records, topic, set(), {managed})
    assert [source['source_key'] for source in approved] == [managed]
    with pytest.raises(SourceSelectionRequired):
        flow.catalog.select_for_question(records, topic, approved, custom, 'Investiga en la web las novedades.')


@pytest.mark.parametrize('extra', ['X', 'YouTube'])
def test_multiple_explicit_capabilities_require_clarification(selection, extra):
    flow, _, _, _, choose = selection
    flow.catalog.ensure_public_youtube_search()
    flow.catalog.ensure_public_x_search()
    with pytest.raises(SourceSelectionRequired, match='multiple'):
        choose('Investiga en la web y en ' + extra + ' las novedades de D1.')


@pytest.mark.parametrize('url', ['https://example.com/reference', 'https://other.example.org/new',
    'https://127.0.0.1/private', 'http://example.com/reference'])
def test_explicit_page_cannot_be_substituted_by_the_generic_web_alias(selection, url):
    _, _, _, _, choose = selection
    with pytest.raises(SourceSelectionRequired):
        choose('Investiga en la web esta página: ' + url)


def test_existing_exact_name_and_registered_url_paths_are_preserved(selection):
    flow, _, _, _, choose = selection
    assert choose('Consulta Búsqueda web sobre D1.')[0]['profile_id'] == 'web_search_serp'
    source = SCOPE.key('web')
    selected = choose('Consulta https://example.com/reference', source)
    assert selected[0]['source_key'] == source
    assert [target['canonical_url_or_id'] for target in selected[0]['targets']] == ['https://example.com/reference']
