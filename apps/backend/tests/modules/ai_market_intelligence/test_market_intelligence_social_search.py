"""Separate keyword-post and popular-reel contracts, without external calls."""

import copy

import pytest



from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.connectors import compile_call

from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.social_search import facebook_search_observations, instagram_search_observations



WINDOW={'start':'2026-10-27T10:00:00+00:00','end':'2026-11-03T10:00:00+00:00'}

RECEIPT={'runId':'fixture-run'}





def search_call(platform):

    return compile_call(platform+'_search',{'query':'Tiendas D1','urls':[],'max_items':3,'sort':'Latest'},

                        WINDOW,None,unlimited=True)





def facebook_row():

    return {'facebookUrl':'https://www.facebook.com/search/top?q=Tiendas%20D1','searchType':'posts',

        'postId':'123','url':'https://www.facebook.com/publisher/posts/123',

        'text':'Una observación de un cliente sobre D1.','time':'2026-10-29T12:00:00Z',

        'likes':3,'comments':None,'shares':0,'user':{'name':'Observed writer'},

        'collaborators':[{'name':'A city, not a coauthor'}]}





def instagram_row():

    return {'id':'123','type':'Video','productType':'clips','shortCode':'ABC123',

        'url':'https://www.instagram.com/p/ABC123/','inputUrl':'https://www.instagram.com/popular/tiendas-d1',

        'caption':'Una descripción sobre Tiendas D1, sin transcripción.','timestamp':'2026-04-01T12:00:00Z',

        'ownerUsername':'another_creator','likesCount':10,'commentsCount':None,'mentions':['tiendas.d1']}





def test_facebook_keyword_contract_retains_post_date_metrics_and_unknown_author_relationship():

    call=search_call('facebook')

    assert call['actor']=='apify/facebook-search-scraper'

    assert call['input']=={'categories':['Tiendas D1'],'locations':[],'searchType':'posts','resultsLimit':3}

    assert 'maxItems' not in call['callOptions'] and 'maxTotalChargeUsd' not in call['callOptions']

    row=facebook_search_observations([facebook_row()],RECEIPT,call)[0]

    assert row['published_at']=='2026-10-29T12:00:00+00:00'

    assert row['metrics']=={'likes':3,'comments':None,'shares':0}

    assert row['provenance']['source_relation']=='unknown' and row['provenance']['comments_retrieved'] is False





def test_instagram_popular_search_keeps_caption_owner_and_original_date_without_video_or_mention_attribution():

    call=search_call('instagram')

    assert call['actor']=='apify/instagram-search-scraper'

    assert call['input']=={'search':'Tiendas D1','searchType':'popular','searchLimit':3,'liveSearch':True,

                           'enhanceUserSearchWithFacebookPage':False}

    row=instagram_search_observations([instagram_row()],RECEIPT,call)[0]

    assert row['published_at']=='2026-04-01T12:00:00+00:00'

    assert row['provenance']['author_url']=='https://instagram.com/another_creator'

    assert row['provenance']['source_relation']=='unknown'

    assert row['provenance']['scope']=='instagram_popular_reels'

    assert row['provenance']['video_content_analyzed'] is row['provenance']['comments_retrieved'] is False

    assert row['metrics']['comments'] is None





@pytest.mark.parametrize('platform,field,value',[

 ('facebook','facebookUrl','https://facebook.com/search/top?q=other'),

 ('facebook','facebookUrl','https://facebook.com.evil.example/search/top?q=Tiendas%20D1'),

 ('facebook','facebookUrl','https://facebook.com/search/top?q=Tiendas%20D1&q=other'),

 ('facebook','searchType','pages'),('facebook','url','https://facebook.com/a-profile'),

 ('facebook','likes',True),('facebook','comments',-1),('facebook','error','blocked'),

 ('instagram','inputUrl','https://instagram.com/popular/other'),

 ('instagram','inputUrl','https://instagram.com.evil.example/popular/tiendas-d1'),

 ('instagram','shortCode','wrong'),('instagram','type','hashtag'),('instagram','productType','igtv'),

 ('instagram','ownerUsername','not/a/profile'),('instagram','likesCount',True),('instagram','error','blocked')])

def test_search_parsers_reject_wrong_query_type_identity_metrics_and_errors(platform,field,value):

    row=facebook_row() if platform=='facebook' else instagram_row()

    row[field]=value

    parse=facebook_search_observations if platform=='facebook' else instagram_search_observations

    with pytest.raises(ValueError):parse([row],RECEIPT,search_call(platform))





@pytest.mark.parametrize('platform',['facebook','instagram'])

def test_search_parser_detects_duplicate_or_over_limit_rows_and_query_multipliers(platform):

    call=search_call(platform)

    row=facebook_row() if platform=='facebook' else instagram_row()

    parse=facebook_search_observations if platform=='facebook' else instagram_search_observations

    for rows in ([row,row],[copy.deepcopy(row) for _ in range(4)]):

        with pytest.raises(ValueError):parse(rows,RECEIPT,call)

    with pytest.raises(ValueError):

        compile_call(platform+'_search',{'query':'D1, another query','urls':[],'max_items':3,'sort':'Latest'},WINDOW,None,unlimited=True)





@pytest.mark.parametrize('code',['../accounts/login','a/b','%2e%2e%2faccounts','..'])

def test_instagram_shortcode_cannot_escape_the_public_media_route(code):

    row={**instagram_row(),'shortCode':code,'url':'https://instagram.com/p/'+code}

    with pytest.raises(ValueError,match='media identity'):

        instagram_search_observations([row],RECEIPT,search_call('instagram'))





@pytest.mark.parametrize('url',[

    'https://www.facebook.com/groups/public-group/permalink/3293837350804213/',

    'https://www.facebook.com/permalink.php?story_fbid=pfbid02AbCd123&id=61578622795831',

    'https://facebook.com/permalink.php?id=61578622795831&story_fbid=1234',

    'https://www.facebook.com/reel/4571674936442382/',

    'https://www.facebook.com/TiendasD1/videos/226061368702660/',

])

def test_facebook_search_accepts_observed_public_post_routes_without_changing_profile_readers(url):

    row={**facebook_row(),'postId':'795792006927120','url':url}

    result=facebook_search_observations([row],RECEIPT,search_call('facebook'))

    assert len(result)==1 and result[0]['text']==row['text']

    assert result[0]['url'].startswith('https://facebook.com/')

    if '/groups/' in url:

        from app.modules.ai_market_intelligence.aidp.workspaces.default.files.agents.ai_market_intelligence.ai_market_intelligence.social import social_url

        with pytest.raises(ValueError):social_url(url,'facebook')





@pytest.mark.parametrize('url',[

    'https://facebook.com/groups/private-group',

    'https://facebook.com/groups/../permalink/123',

    'https://facebook.com/../posts/123',

    'https://facebook.com/groups/a%2Fb/permalink/123',

    'https://facebook.com.evil.example/groups/public/permalink/123',

    'https://facebook.com/login',

    'https://facebook.com/login/posts/123',

    'https://facebook.com/search/posts/123',

    'https://facebook.com/settings/posts/123',

    'https://facebook.com/permalink.php?story_fbid=pfbid123&id=123&redirect=evil',

    'https://facebook.com/permalink.php?story_fbid=pfbid123&id=123&id=123',

    'https://facebook.com/permalink.php?story_fbid=../login&id=123',

])

def test_facebook_search_rejects_unreviewed_routes_and_permalink_parameters(url):

    with pytest.raises(ValueError):

        facebook_search_observations([{**facebook_row(),'url':url}],RECEIPT,search_call('facebook'))





def test_instagram_known_empty_query_marker_is_empty_data_not_a_failed_capture():

    route='https://www.instagram.com/popular/tiendas-d1'

    row={'inputUrl':route,'url':route,'error':'no_items',

         'errorDescription':'Empty or private data for provided input','requestErrorMessages':[]}

    assert instagram_search_observations([row],RECEIPT,search_call('instagram'))==[]

    for patch in ({'inputUrl':'https://www.instagram.com/popular/another-brand'},

                  {'error':'blocked'},{'requestErrorMessages':['blocked']},{'type':'Video'}):

        with pytest.raises(ValueError):

            instagram_search_observations([{**row,**patch}],RECEIPT,search_call('instagram'))





@pytest.mark.parametrize('platform',['facebook','instagram'])

def test_valid_identity_without_caption_is_omitted_with_visible_coverage(platform):

    import asyncio

    from tests.modules.ai_market_intelligence.test_market_intelligence_payload import provider_for

    from tests.modules.ai_market_intelligence.test_market_intelligence_research_collector import market_collection_fixture

    good=facebook_row() if platform=='facebook' else instagram_row()

    empty=copy.deepcopy(good)

    if platform=='facebook':

        empty.update(postId='456',url='https://facebook.com/publisher/posts/456',text=None)

    else:

        good['timestamp']=empty['timestamp']='2026-10-29T12:00:00Z'

        empty.update(shortCode='XYZ456',url='https://instagram.com/p/XYZ456',caption='')

    provider,_=provider_for([good,empty])

    result=asyncio.run(provider.observations({**RECEIPT,'spent_microusd':500,'status':'SUCCEEDED','datasetId':'fixture-data'},search_call(platform)))

    assert len(result.items)==1 and result.coverage=={'omitted_textless_items':1}

    _,_,collector=market_collection_fixture()

    collection=collector.collection(platform,platform,'search',{'items':result.items,'coverage':result.coverage},RECEIPT,WINDOW)

    assert collection['status']=='partial' and len(collection['evidence'])==1

    assert any('sin texto recuperado' in limit for limit in collection['limitations'])

    invalid={**empty,('facebookUrl' if platform=='facebook' else 'inputUrl'):'https://evil.example/other'}

    parse=facebook_search_observations if platform=='facebook' else instagram_search_observations

    with pytest.raises(ValueError):parse([good,invalid],RECEIPT,search_call(platform))
