"""Keyword-search contracts, separate from readers of explicitly selected profiles."""
from datetime import datetime, timezone
import re
import unicodedata
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from .runtime_core import digest, public_url
from .social import social_url
from .media import retrieved_media


SOCIAL_SEARCH_ACTORS={'facebook':'apify/facebook-search-scraper','instagram':'apify/instagram-search-scraper'}


def _social_search_rows(rows,call,field):
    limit=call['input'][field]
    if type(limit) is not int or not 1<=limit<=20 or not isinstance(rows,list) or len(rows)>limit:
        raise ValueError('Social search exceeds its admitted result limit')
    if any(not isinstance(row,dict) or any(row.get(key) for key in
           ('error','errorDescription','requestErrorMessages','isError')) for row in rows):
        raise ValueError('Social search contains a provider error')
    return rows


def _social_search_date(value):
    if not isinstance(value,str):
        raise ValueError('Social search requires an explicit publication timestamp')
    moment=datetime.fromisoformat(value.replace('Z','+00:00'))
    if moment.tzinfo is None:
        raise ValueError('Social search publication timezone is unknown')
    return moment.astimezone(timezone.utc).isoformat()


def _social_search_metrics(row,mapping):
    values={target:row.get(source) for source,target in mapping.items()}
    if any(number is not None and (type(number) is not int or number<0) for number in values.values()):
        raise ValueError('Search engagement must be nonnegative or unknown')
    return values


def _facebook_search_post_url(row):
    uri=urlsplit(public_url(row.get('url')))
    path=uri.path.rstrip('/')
    if (uri.hostname not in {'facebook.com','www.facebook.com'} or not isinstance(row.get('postId'),str)
            or not re.fullmatch(r'[0-9]{1,40}',row['postId']) or any(part in {'.','..'} for part in path.split('/'))):
        raise ValueError('Facebook search requires a public post identity')
    if uri.query:
        pairs=parse_qsl(uri.query,keep_blank_values=True)
        values=dict(pairs)
        if (path!='/permalink.php' or len(pairs)!=2 or set(values)!={'story_fbid','id'}
                or not re.fullmatch(r'[0-9]{1,40}',values['id'])
                or not re.fullmatch(r'(?:[0-9]{1,40}|pfbid[A-Za-z0-9]{1,200})',values['story_fbid'])):
            raise ValueError('Facebook search requires a reviewed permalink identity')
        return urlunsplit(('https','facebook.com',path,urlencode(sorted(values.items())),''))
    if not re.fullmatch(r'/(?:[A-Za-z0-9_.-]+/(?:posts/[A-Za-z0-9_]+|videos/[0-9]{1,40})|reel/[0-9]{1,40}|groups/[A-Za-z0-9_-]+/permalink/[0-9]{1,40})',path):
        raise ValueError('Facebook search requires a reviewed post route')
    return urlunsplit(('https','facebook.com',path,'','')) if '/permalink/' in path else social_url(row['url'],'facebook')


def _social_search_observation(row,call,receipt,platform,text,date,metrics,mode):
    url=_facebook_search_post_url(row) if platform=='facebook' else social_url(row.get('url'),platform)
    published=_social_search_date(date)
    media=retrieved_media(row,platform)
    if text is None or isinstance(text,str) and not text.strip():
        if not media['media']:
            return None
        text='Publicación sin texto recuperado; contiene medios enlazados cuyo contenido no se ha analizado.'
        basis='media_metadata'
    else:
        basis='post_text'
    if not isinstance(text,str):
        raise ValueError('Social search text analysis requires a retrieved caption or post body')
    return {'url':url,'text':text[:12000],'published_at':published,
        'observed_at':datetime.now(timezone.utc).isoformat(),'metrics':metrics,
        'provenance':{'source':call['actor'],'actor_build':call['callOptions']['build'],
            'provider_run_id':receipt['runId'],'request_arguments_hash':digest(call),
            **media,'content_basis':basis,'scope':mode,'search_mode':mode,'source_relation':'unknown',
            'location':None,'comments_retrieved':False,'video_content_analyzed':False,
            'transcript_retrieved':False,'text_truncated':len(text)>12000,'original_character_count':len(text)}}


def facebook_search_observations(rows,receipt,call):
    inputs=call['input']
    if (call['actor']!=SOCIAL_SEARCH_ACTORS['facebook'] or inputs['searchType']!='posts'
            or len(inputs['categories'])!=1):
        raise ValueError('Facebook search requires its reviewed post-search contract')
    expected=' '.join([inputs['categories'][0],*inputs['locations']])
    results=[]
    for row in _social_search_rows(rows,call,'resultsLimit'):
        origin=urlsplit(public_url(row.get('facebookUrl')))
        if (origin.hostname not in {'facebook.com','www.facebook.com'} or origin.path not in {'/search/top','/search/posts'}
                or parse_qs(origin.query)!={'q':[expected]} or row.get('searchType')!='posts'):
            raise ValueError('Facebook result belongs to another query or search type')
        metrics=_social_search_metrics(row,{'likes':'likes','comments':'comments','shares':'shares'})
        observation=_social_search_observation(row,call,receipt,'facebook',row.get('text'),row.get('time'),metrics,
            'facebook_keyword_posts')
        if observation is not None:
            results.append(observation)
    if len({row['url'] for row in results})!=len(results):
        raise ValueError('Facebook search repeats a post identity')
    return results


def instagram_search_slug(query):
    text=unicodedata.normalize('NFKD',query).encode('ascii','ignore').decode().lower()
    return re.sub(r'[^a-z0-9]+','-',text).strip('-')


def _instagram_search_identity(row,expected):
    origin=urlsplit(public_url(row.get('inputUrl')))
    if (origin.hostname not in {'instagram.com','www.instagram.com'} or origin.path.rstrip('/')!='/popular/'+expected
            or origin.query or row.get('type')!='Video' or row.get('productType')!='clips'):
        raise ValueError('Instagram result belongs to another query or is not a retrieved reel')
    url=social_url(row.get('url'),'instagram')
    code=row.get('shortCode')
    if (not isinstance(code,str) or not re.fullmatch(r'[A-Za-z0-9_-]{5,64}',code)
            or urlsplit(url).path.rstrip('/') not in {'/p/'+code,'/reel/'+code}):
        raise ValueError('Instagram search requires a matching public media identity')
    owner=row.get('ownerUsername')
    if not isinstance(owner,str) or not re.fullmatch(r'[A-Za-z0-9._]{1,30}',owner):
        raise ValueError('Instagram search requires the observed public owner identity')
    return social_url('https://instagram.com/'+owner,'instagram')


def _instagram_search_rows(rows,call,expected):
    if isinstance(rows,list) and len(rows)==1 and isinstance(rows[0],dict) and rows[0].get('error')=='no_items':
        row=rows[0]
        route='https://www.instagram.com/popular/'+expected
        if (set(row)-{'inputUrl','url','error','errorDescription','requestErrorMessages'}
                or row.get('inputUrl')!=route or row.get('url')!=route
                or row.get('errorDescription')!='Empty or private data for provided input'
                or row.get('requestErrorMessages')!=[]):
            raise ValueError('Instagram empty result does not match its admitted query')
        return []
    return _social_search_rows(rows,call,'searchLimit')


def instagram_search_observations(rows,receipt,call):
    inputs=call['input']
    if (call['actor']!=SOCIAL_SEARCH_ACTORS['instagram'] or inputs['searchType']!='popular'
            or inputs['liveSearch'] is not True or inputs['enhanceUserSearchWithFacebookPage'] is not False):
        raise ValueError('Instagram search requires its reviewed popular-reels contract')
    expected=instagram_search_slug(inputs['search'])
    if not expected:
        raise ValueError('The Instagram search phrase has no supported route identity')
    results=[]
    for row in _instagram_search_rows(rows,call,expected):
        author_url=_instagram_search_identity(row,expected)
        metrics=_social_search_metrics(row,{'likesCount':'likes','commentsCount':'comments'})
        observation=_social_search_observation(row,call,receipt,'instagram',row.get('caption'),row.get('timestamp'),metrics,
            'instagram_popular_reels')
        if observation is not None:
            observation['provenance']['author_url']=author_url
            results.append(observation)
    if len({row['url'] for row in results})!=len(results):
        raise ValueError('Instagram search repeats a media identity')
    return results
