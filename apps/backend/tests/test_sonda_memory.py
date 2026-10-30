import asyncio
from uuid import UUID

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.labs.ai_social_network.notebooks.agent.sonda_memory import GenerationMemory, root_graph_config


def test_oracle_default_namespace_routes_to_root_without_changing_authenticated_context():
    original={'configurable':{'thread_id':'authenticated-session','checkpoint_ns':'default',
        'agent_id':'authenticated-agent'},'metadata':{'checkpoint_ns':'default','request':'retained'}}
    config=root_graph_config(original)
    assert config['configurable']=={'thread_id':'authenticated-session','checkpoint_ns':'',
        'agent_id':'authenticated-agent'}
    assert config['metadata']=={'checkpoint_ns':'','request':'retained'}
    assert original['configurable']['checkpoint_ns']=='default'
    assert original['metadata']['checkpoint_ns']=='default'
    async def check():
        graph=StateGraph(dict)
        graph.add_node('echo',lambda state:state)
        graph.add_edge(START,'echo')
        graph.add_edge('echo',END)
        compiled=graph.compile(checkpointer=GenerationMemory(InMemorySaver(),UUID(int=1)))
        assert not (await compiled.aget_state(config)).values
        await compiled.ainvoke({'text':'native context'},config)
        assert (await compiled.aget_state(config)).values['text']=='native context'
    asyncio.run(check())


def test_external_subgraph_route_is_rejected():
    with pytest.raises(ValueError,match='external subgraph'):
        root_graph_config({'configurable':{'thread_id':'authenticated-session','checkpoint_ns':'other'}})


def test_real_langgraph_namespaces_are_isolated_and_resume_without_exposing_internal_prefix():
    async def check():
        delegate=InMemorySaver()
        a=GenerationMemory(delegate,UUID(int=1))
        b=GenerationMemory(delegate,UUID(int=2))
        graph=StateGraph(dict)
        graph.add_node('echo',lambda state:state)
        graph.add_edge(START,'echo')
        graph.add_edge('echo',END)
        one,two=graph.compile(checkpointer=a),graph.compile(checkpointer=b)
        config={'configurable':{'thread_id':'same-authenticated-session'}}
        await one.ainvoke({'text':'first generation'},config)
        assert (await one.aget_state(config)).values['text']=='first generation'
        assert not (await two.aget_state(config)).values
        await two.ainvoke({'text':'second generation'},config)
        assert (await one.aget_state(config)).values['text']=='first generation'
        assert (await two.aget_state(config)).values['text']=='second generation'
        rows=[row async for row in a.alist(config,limit=1)]
        assert rows[0].config['configurable']['checkpoint_ns']==''
        assert 'checkpoint_ns' not in config['configurable']
    asyncio.run(check())


def test_cross_generation_checkpoint_metadata_cannot_be_recast_as_current():
    memory=GenerationMemory(InMemorySaver(),UUID(int=1))
    with pytest.raises(RuntimeError,match='another generation'):
        memory.namespaced({'configurable':{'checkpoint_ns':'sonda/'+str(UUID(int=2))+'/'}},reverse=True)
