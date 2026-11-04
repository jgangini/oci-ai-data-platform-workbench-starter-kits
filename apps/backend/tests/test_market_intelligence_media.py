"""Media destinations remain provider-bound through drafts, finals and replay."""
import asyncio
import json

import pytest

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.media import MediaAsset, media_url, model_evidence, retrieved_media
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import ApifyProvider, _provider_dataset_arguments
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_contracts import V2Evidence
from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.v2_wire import v2_replay_packet
from test_market_intelligence_v2 import v2_fixture, v2_evidence, v2_run


SIGNED='https://cdn.example.org/video.mp4?sig=a%2Fb%2Bc&token=x+y&fbclid=required&sig=second'


@pytest.mark.parametrize('url',['javascript:alert(1)','http://example.org/a','https://127.0.0.1/a',
    'https://169.254.169.254/a','https://localhost/a','https://internal.local/a','https://[::1]/a',
    'https://user:password@example.org/a','https://example.org:8443/a','https://example.org/a\n',
    'https://example.org\\@evil.org/a','https://example.org/a#fragment'])
def test_media_rejects_unsafe_destinations(url):
    with pytest.raises(ValueError):
        media_url(url)


def test_signed_media_query_is_preserved_and_optional_bad_links_do_not_drop_posts():
    assert media_url(SIGNED)==SIGNED
    result=retrieved_media({'videoUrl':SIGNED,'displayUrl':'https://127.0.0.1/private'},'instagram')
    assert result['media']==[dict(kind='video',url=SIGNED,delivery='direct',role='attachment')]
    assert result['media_omitted_count']==1 and result['media_content_analyzed'] is False
    assert retrieved_media({'media':'not an array'},'facebook')['media']==[]


def test_actor_formats_preserve_media_types_and_choose_native_video_not_dash():
    fb=retrieved_media({'media':[{'__typename':'Video','url':'https://facebook.com/reel/123',
        'videoDeliveryLegacyFields':{'browser_native_hd_url':SIGNED,'browser_native_sd_url':'https://cdn.example.org/sd.mp4'},
        'image':{'uri':'https://cdn.example.org/poster.jpg'},'dash_manifest':'PRIVATE XML'}]},'facebook')
    assert [item['kind'] for item in fb['media']]==['video','image']
    assert fb['media'][0]['url']==SIGNED and fb['media'][1]['role']=='thumbnail'
    x=retrieved_media({'media':['https://pbs.twimg.com/poster.jpg'], 'extendedEntities':{'media':[
        {'type':'video','media_url_https':'https://pbs.twimg.com/poster.jpg','video_info':{'variants':[
            {'content_type':'application/x-mpegURL','url':'https://video.twimg.com/list.m3u8'},
            {'content_type':'video/mp4','url':SIGNED,'bitrate':1000}]}}]}},'x')
    assert len(x['media'])==2 and x['media'][0]['url']==SIGNED and x['media'][1]['role']=='thumbnail'
    yt=retrieved_media({'url':'https://www.youtube.com/watch?v=abcdefghijk','thumbnailUrl':'https://i.ytimg.com/a.jpg'},'youtube')
    assert yt['media'][0]['delivery']=='page'
    web=retrieved_media({'metadata':{'openGraph':[{'property':'og:image','content':'https://example.org/preview.jpg'}]}},'web')
    assert web['media'][0]['role']=='preview'


def test_carousel_is_bounded_deduplicated_and_has_no_model_generated_urls():
    urls=[f'https://cdn.example.org/{index}.jpg' for index in range(21)]
    result=retrieved_media({'displayUrl':urls[0],'images':urls},'instagram')
    assert len(result['media'])==8 and result['media_omitted_count']==13
    payload=model_evidence({'evidence':[dict(v2_evidence(),**result)]})
    assert 'cdn.example.org' not in json.dumps(payload)
    assert payload['evidence'][0]['media_summary']=={'count':8,'kinds':['image']}


def test_x_media_only_post_is_delivered_without_text_inference():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import _x_observations
    row={'url':'https://x.com/brand/status/123','text':'','media':['https://pbs.twimg.com/image.jpg']}
    call={'actor':'apidojo/twitter-scraper-lite','input':{'maxItems':1},'callOptions':{}}
    result=_x_observations([row],{'runId':'run'},call)
    assert result[0]['provenance']['content_basis']=='media_metadata'
    assert result[0]['provenance']['media'][0]['url']==row['media'][0]


def test_shared_provider_media_never_reaches_the_legacy_model_boundary():
    from types import SimpleNamespace
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.contracts import FactualReview
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.workflow import MarketWorkflow
    class Model:
        async def ask(self,name,schema,payload):
            assert SIGNED not in json.dumps(payload)
            assert payload['row']['provenance']['media_summary']['count']==1
            return {'accepted':True,'issues':[]}
    payload={'row':{'provenance':retrieved_media({'videoUrl':SIGNED},'instagram')}}
    result=asyncio.run(MarketWorkflow.ask(SimpleNamespace(model=Model()),'verify',FactualReview,payload))
    assert result['accepted'] is True and payload['row']['provenance']['media'][0]['url']==SIGNED


def test_media_only_social_post_retains_permalink_without_inventing_a_caption():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.social_search import _social_search_observation
    call={'actor':'apify/instagram-search-scraper','callOptions':{'build':'0.1.561'}}
    row={'url':'https://instagram.com/p/ABCDE1','videoUrl':SIGNED}
    result=_social_search_observation(row,call,{'runId':'run'},'instagram',None,
        '2026-11-01T00:00:00Z',{},'instagram_popular_reels')
    assert result['provenance']['content_basis']=='media_metadata'
    assert result['provenance']['media'][0]['url']==SIGNED
    assert 'no se ha analizado' in result['text']


def test_facebook_pages_existing_dataset_with_overflow_detection_and_no_capture():
    class Dataset:
        def __init__(self): self.calls=[]
        async def ainvoke(self,args):
            self.calls.append(args)
            rows=[{'url':f'https://facebook.com/brand/posts/{i}'} for i in range(7)]
            return {'items':rows[args['offset']:args['offset']+args['limit']]}
    dataset=Dataset()
    provider=ApifyProvider({name:dataset for name in ('call-actor','get-actor-run','get-dataset-items')})
    call={'actor':'apify/facebook-search-scraper','callOptions':{'maxItems':6},'input':{'resultsLimit':6}}
    arguments=_provider_dataset_arguments({'datasetId':'dataset'},call)
    rows=asyncio.run(provider.facebook_rows(arguments,call))
    assert len(rows)==7  # Parser must reject the seventh, rather than silently truncate it.
    assert [(a['offset'],a['limit']) for a in dataset.calls]==[(0,3),(3,3),(6,1)]
    assert all(a['datasetId']=='dataset' and 'media' in a['fields'].split(',') for a in dataset.calls)


def test_facebook_unlimited_compiled_call_reads_media_without_billing_maxitems():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.connectors import compile_call
    from test_market_intelligence_payload import provider_for
    call=compile_call('facebook_search',{'query':'Tiendas D1','urls':[],'max_items':20,'sort':'Latest'},
        {'start':'2026-10-01T00:00:00+00:00','end':'2026-11-01T00:00:00+00:00'},None,unlimited=True)
    assert 'maxItems' not in call['callOptions']
    provider,tool=provider_for({'items':[]})
    result=asyncio.run(provider.observations({'runId':'existing','status':'SUCCEEDED','datasetId':'existing'},call))
    assert result.items==[] and len(tool.calls)==1 and tool.calls[0]['datasetId']=='existing'


def test_media_survives_final_and_replay_without_private_evidence_or_model_urls():
    store,model,collector,graph=v2_fixture()
    original=collector.collect
    async def collect(**kwargs):
        value=await original(**kwargs)
        value['evidence'][0].update(retrieved_media({'videoUrl':SIGNED},'instagram'))
        return value
    collector.collect=collect
    ask=model.ask
    async def check(name,schema,payload):
        assert SIGNED not in json.dumps(payload)
        return await ask(name,schema,payload)
    model.ask=check
    _,state=v2_run(graph)
    refs=state['response']['references']
    assert refs[0]['media'][0]['url']==SIGNED
    packet=json.loads(v2_replay_packet(store,'fixture-request',0))
    assert packet['references'][0]['media']==refs[0]['media']
    assert not {'receipt_id','text','owner_id'} & refs[0].keys()
    assert len(collector.calls)==1
    evidence=V2Evidence.model_validate(store.read()[0]['evidence'][refs[0]['evidence_id']])
    assert evidence.media[0].url==SIGNED and evidence.media_content_analyzed is False
    with pytest.raises(ValueError):
        MediaAsset(kind='image',url=SIGNED,role='attachment',delivery='direct',secret='never')


def test_followup_budgets_text_before_media_and_keeps_all_original_assets():
    from test_market_intelligence_v2_followup import saved_fixture,followup_graph,run_followup
    prior=saved_fixture()
    third=v2_evidence('youtube',3)
    prior['evidence'][third['evidence_id']]=third
    prior['response']['references'].append({'evidence_id':third['evidence_id'],'url':third['url']})
    for reference in prior['response']['references']:
        row=prior['evidence'][reference['evidence_id']]
        row['media']=[dict(kind='image',url=f'https://cdn.example.org/{i}?sig='+'a'*3900,
                           delivery='direct',role='attachment') for i in range(8)]
        reference['media']=row['media']
    graph,store,model,collector=followup_graph(prior)
    ask=model.ask
    async def check(name,schema,payload):
        assert 'cdn.example.org' not in json.dumps(payload)
        if name=='v2_summarize':
            assert len(payload['allowed_evidence_ids'])==3
        return await ask(name,schema,payload)
    model.ask=check
    state=run_followup(graph)
    assert state['response']['references']==prior['response']['references'] and collector.calls==[]
    packet=json.loads(v2_replay_packet(store,'followup',0))
    assert len(packet['references'])==3 and all(len(ref['media'])==8 for ref in packet['references'])


def test_youtube_fields_retain_literal_boundaries_through_collector_and_old_evidence_is_compatible():
    from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence.provider import _youtube_observation
    from test_market_intelligence_v2_collector import v2_collection_fixture
    from test_market_intelligence_youtube_provider import video_row
    raw=video_row()
    raw.update(title='Título con\n\nsaltos',text='Descripción propia\n\ncon otros saltos',thumbnailUrl=SIGNED)
    row=_youtube_observation(raw,'streamers/youtube-scraper','receipt')
    _,_,collector=v2_collection_fixture()
    brief={'start':'2026-10-30T00:00:00+00:00','end':'2026-11-02T00:00:00+00:00','retrieval_mode':'conversation'}
    evidence=collector.evidence_row('youtube','youtube',row,{'runId':'receipt'},brief)
    assert evidence['source_title']==raw['title'] and evidence['source_description']==raw['text']
    assert evidence['media'][1]['url']==SIGNED
    old=V2Evidence.model_validate(v2_evidence('youtube'))
    assert old.source_title is None and old.source_description is None
    packet=model_evidence(evidence)
    assert packet['source_title']==raw['title'] and packet['source_description']==raw['text']
    assert SIGNED not in json.dumps(packet)
