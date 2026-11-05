"""The planner catalogue and executor use the same closed, typed source tools."""

import asyncio

import pytest



from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.actor_tools import actor_tool, actor_tool_specifications



WINDOW={'start':'2026-10-27T10:00:00+00:00','end':'2026-11-03T10:00:00+00:00'}





def test_catalogue_has_distinct_query_destination_and_financial_contracts_without_raw_actor_parameters():

    tools={tool['name']:tool for tool in actor_tool_specifications(['web','x','youtube','facebook','instagram','market_data','world_bank'])}

    assert len(tools)==10

    assert 'query' in tools['facebook_search']['arguments_schema']['properties']

    assert 'urls' in tools['facebook_posts']['arguments_schema']['properties']

    assert 'tickers' in tools['yahoo_history']['arguments_schema']['properties']

    assert 'countries' in tools['world_bank']['arguments_schema']['properties']

    assert tools['instagram_search']['result_kind']=='popular_reel_captions'

    assert tools['website']['arguments_schema']['properties']['max_items']['maximum']==3

    assert tools['web_search_serp']['arguments_schema']['properties']['max_items']['const']==10

    assert tools['x_search']['arguments_schema']['properties']['sort']['const']=='Latest'

    for tool in tools.values():

        assert tool['arguments_schema']['additionalProperties'] is False

        assert not {'actor','build','callOptions','token'}&tool['arguments_schema']['properties'].keys()

    assert {tool['name'] for tool in actor_tool_specifications(['instagram'])}=={'instagram_search','instagram_posts'}





def test_named_tool_executes_its_own_compiler_and_rejects_another_source_arguments_before_execution():

    calls=[]

    async def execute(call):

        calls.append(call)

        return {'receipt':'fixture'}

    tool=actor_tool('facebook_search')

    result=asyncio.run(tool.ainvoke({'query':'Tiendas D1','max_items':3},window=WINDOW,executor=execute,unlimited=True))

    assert result=={'receipt':'fixture'} and calls[0]['actor']=='apify/facebook-search-scraper'

    for invalid in ({'urls':['https://facebook.com/page'],'max_items':3},

                    {'query':'D1','max_items':3,'actor':'other/actor'}, {'query':'D1','max_items':21}):

        with pytest.raises(ValueError):

            asyncio.run(tool.ainvoke(invalid,window=WINDOW,executor=execute,unlimited=True))

    assert len(calls)==1

    with pytest.raises(ValueError):actor_tool('call-actor')





def test_financial_tool_cannot_use_ticker_arguments_for_an_indicator_or_invent_approval():

    arguments={'tickers':['AAPL'],'start_date':'2025-01-02','end_date':'2025-01-09','max_items':10}

    with pytest.raises(ValueError):actor_tool('world_bank').compile(arguments,WINDOW,unlimited=True,approved_finance={'tickers':['AAPL']})

    with pytest.raises(ValueError):actor_tool('yahoo_history').compile(arguments,WINDOW,unlimited=True,approved_finance={'tickers':['MSFT']})





def test_google_tool_schema_and_compiler_agree_on_one_ten_candidate_page():

    tool=actor_tool('web_search_serp')

    with pytest.raises(ValueError):

        tool.compile({'query':'D1','max_items':3},WINDOW,unlimited=True,

                     approved_search_context={'country_code':'CO','language_code':'es'})

    call=tool.compile({'query':'D1','max_items':10,'search_mode':'navigation'},WINDOW,unlimited=True,

                      approved_search_context={'country_code':'CO','language_code':'es'})

    assert call['input']['limit']=='10' and call['input']['include_merged'] is False





def test_graph_source_selection_invokes_the_named_tool_and_its_admitted_executor(monkeypatch):

    from langgraph.checkpoint.memory import InMemorySaver

    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.actor_tools import ActorTool

    from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.workflow import MarketWorkflow

    from tests.modules.ai_market_intelligence.test_market_intelligence_graph import market_fixture, market_run

    from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import market_collection_fixture

    store,model,_,_=market_fixture(('facebook',))

    provider,effects,collector=market_collection_fixture()

    names=[]

    actual=ActorTool.ainvoke

    async def observed(self,arguments,**kwargs):

        names.append(self.name)

        return await actual(self,arguments,**kwargs)

    monkeypatch.setattr(ActorTool,'ainvoke',observed)

    graph=MarketWorkflow(store,model,collector,enabled_sources=('facebook',)).build(InMemorySaver())

    _,state=market_run(graph)

    assert names==['facebook_search'] and len(provider.starts)==1

    assert provider.starts[0]['actor']=='apify/facebook-search-scraper'

    assert state['response']['status']=='complete'
