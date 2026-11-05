"""Public Facebook/Instagram destinations and attributed post text, not global search."""
from datetime import datetime, timezone
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .runtime_core import public_url, digest
from .media import retrieved_media


SOCIAL_ACTORS={'facebook':'apify/facebook-posts-scraper','instagram':'apify/instagram-post-scraper'}


def social_url(value,platform):
    uri=urlsplit(public_url(value))
    host=(uri.hostname or '').removeprefix('www.')
    expected='facebook.com' if platform=='facebook' else 'instagram.com'
    path=uri.path.rstrip('/')
    if host!=expected or not path or path.split('/')[1].lower() in {
            'accounts','login','dialog','sharer','share','groups','settings','explore','search'}:
        raise ValueError('A reviewed public page/profile or post destination is required')
    query=''
    if uri.query:
        pairs=parse_qsl(uri.query,keep_blank_values=True)
        values=dict(pairs)
        if (platform!='facebook' or path not in {'/permalink.php','/story.php'}
                or len(pairs)!=2 or set(values)!={'story_fbid','id'}
                or any(not re.fullmatch(r'[0-9]{1,40}',value) for value in values.values())):
            raise ValueError('Social targets must not carry unreviewed query parameters')
        query=urlencode(sorted(values.items()))
    return urlunsplit(('https',expected,path,query,''))


def compile_social_input(profile,args,approved_urls,start,end,limit):
    platform='facebook' if profile=='facebook_posts' else 'instagram'
    if args['query'] or args.get('financial') is not None:
        raise ValueError('Social post readers accept approved destinations, not a global keyword search')
    urls=args['urls']
    if not isinstance(urls,list) or not 1<=len(urls)<=3:
        raise ValueError('Select one to three public destinations')
    allowed={social_url(url,platform) for url in approved_urls}
    selected=[social_url(url,platform) for url in urls]
    if len(selected)!=len(set(selected)) or not set(selected).issubset(allowed):
        raise ValueError('Social destinations exceed their explicit authority')
    if len(selected)>limit:
        raise ValueError('The total result limit must cover every selected destination')
    common={'resultsLimit':limit//len(selected),'onlyPostsNewerThan':start.astimezone(timezone.utc).isoformat().replace('+00:00','Z')}
    if platform=='facebook':
        return {**common,'startUrls':[{'url':url} for url in selected],
                'onlyPostsOlderThan':end.astimezone(timezone.utc).isoformat().replace('+00:00','Z'),'captionText':False}
    return {**common,'username':selected,'skipPinnedPosts':False,'includeMusicData':False,'dataDetailLevel':'basicData'}


def social_post_identity(row,platform,allowed):
    if not isinstance(row,dict) or any(row.get(key) for key in ('error','errorDescription','requestErrorMessages','isError')):
        raise ValueError('Social output contains a provider error')
    url=social_url(row.get('url'),platform)
    origin=row.get('inputUrl') or row.get('facebookUrl')
    owner=None
    if platform=='instagram' and isinstance(row.get('ownerUsername'),str):
        if not re.fullmatch(r'[A-Za-z0-9._]{1,30}',row['ownerUsername']):
            raise ValueError('Instagram owner is not a public username')
        owner=social_url('https://instagram.com/'+row['ownerUsername'],platform)
        origin=origin or owner
    requested=url if url in allowed else (social_url(origin,platform) if origin is not None else None)
    relation='owner' if requested==owner else 'requested_destination'
    if requested not in allowed:
        requested=social_collaboration(row,platform,allowed)
        relation='collaboration'
    return {'url':url,'requested_url':requested,'author_url':owner,'source_relation':relation}


def social_collaboration(row,platform,allowed):
    collaborators=row.get('coauthorProducers') if platform=='instagram' else None
    if not isinstance(collaborators,list) or len(collaborators)>100:
        raise ValueError('Post does not belong to an admitted public destination')
    matches=set()
    for member in collaborators:
        username=member.get('username') if isinstance(member,dict) else None
        if isinstance(username,str) and re.fullmatch(r'[A-Za-z0-9._]{1,30}',username):
            matches.add(social_url('https://instagram.com/'+username,'instagram'))
    admitted=matches&allowed
    if not admitted:
        raise ValueError('Post does not belong to an admitted public destination')
    return sorted(admitted)[0]


def social_post_time(row,platform,inputs):
    raw=row.get('time') if platform=='facebook' else row.get('timestamp')
    if not isinstance(raw,str):
        raise ValueError('Social post publication must have an explicit timestamp')
    moment=datetime.fromisoformat(raw.replace('Z','+00:00'))
    if moment.tzinfo is None:
        raise ValueError('Social post publication timezone is unknown')
    start=datetime.fromisoformat(inputs['onlyPostsNewerThan'])
    end=datetime.fromisoformat(inputs['onlyPostsOlderThan']) if platform=='facebook' else None
    return None if moment<start or end is not None and moment>end else moment


def social_post_metrics(row):
    metrics={}
    for source,key in (('likesCount','likes'),('commentsCount','comments'),('sharesCount','shares')):
        number=row.get(source)
        if number is not None and (type(number) is not int or number<0):
            raise ValueError('Engagement must be a nonnegative integer or unknown')
        metrics[key]=number
    return metrics


def social_observations(rows,receipt,call):
    platform=next((key for key,actor in SOCIAL_ACTORS.items() if actor==call['actor']),None)
    if platform is None:
        raise ValueError('Social output requires an admitted Actor')
    inputs=call['input']
    targets=[item['url'] for item in inputs['startUrls']] if platform=='facebook' else inputs['username']
    limit=call['callOptions'].get('maxItems',inputs.get('resultsLimit',0)*len(targets))
    if type(limit) is not int or not 1<=limit<=50 or not isinstance(rows,list) or len(rows)>limit:
        raise ValueError('Social output exceeds the admitted actor or total result limit')
    allowed={social_url(url,platform) for url in targets}
    results=[]
    for row in rows:
        identity=social_post_identity(row,platform,allowed)
        url=identity['url']
        text=row.get('text') if platform=='facebook' else row.get('caption')
        media=retrieved_media(row,platform)
        basis='post_text'
        if (text is None or isinstance(text,str) and not text.strip()) and media['media']:
            text='Publicación sin texto recuperado; contiene medios enlazados cuyo contenido no se ha analizado.'
            basis='media_metadata'
        if not isinstance(text,str) or not text.strip():
            raise ValueError('A text analysis requires retrieved post text')
        moment=social_post_time(row,platform,inputs)
        if moment is None:
            continue
        results.append({'url':url,'text':text[:12000],'published_at':moment.astimezone(timezone.utc).isoformat(),
            'observed_at':datetime.now(timezone.utc).isoformat(),'metrics':social_post_metrics(row),
            'provenance':{'source':call['actor'],'actor_build':call['callOptions']['build'],
                'provider_run_id':receipt['runId'],'request_arguments_hash':digest(call),
                'requested_url':identity['requested_url'],'author_url':identity['author_url'],
                'source_relation':identity['source_relation'],
                **media,'content_basis':basis,'scope':'explicit_public_destinations','location':None,
                'text_truncated':len(text)>12000,'original_character_count':len(text),
                'comments_retrieved':False,'global_topic_search':False}})
    if len({row['url'] for row in results})!=len(results):
        raise ValueError('Social output repeats a post identity')
    return results
