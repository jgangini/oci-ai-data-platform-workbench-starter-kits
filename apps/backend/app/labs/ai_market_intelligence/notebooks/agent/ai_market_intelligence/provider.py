"""MCP transport is reachable only through the admitted operation ledger."""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
import json
import re
from urllib.parse import parse_qs, urlsplit

from .mcp_payload import unpack, unpack_run, locate, dataset_id, _protocol_text_blocks
from .runtime_core import public_url
from .finance import ACTORS, normalize_finance
from .runtime_core import ProviderReceiptUnverified, ProviderStartRejected, ProviderStartResponseError, ProviderStatusUnavailable
from .serp import SERP_ACTOR, serp_observations
from .social import SOCIAL_ACTORS, social_observations
from .social_search import SOCIAL_SEARCH_ACTORS, facebook_search_observations, instagram_search_observations
from .media import retrieved_media, media_post_text


class ProviderMCPError(RuntimeError):
    pass


class _ReceiptAccountingError(ValueError):
    pass


def _start_response_diagnostic(value):
    diagnostic={'response_type':type(value).__name__}
    prefix="MCP tool 'call-actor' invocation failed: "
    if not isinstance(value,str) or not value.startswith(prefix):
        return diagnostic
    message=value[len(prefix):][:2000]
    if re.search(r'(?i)token|authorization|credential|password|private.key|cookie|secret',message):
        return diagnostic
    message=re.sub(r'https?://\S+','[url]',message)
    diagnostic['provider_validation_message']=re.sub(r'[A-Za-z0-9_+/=-]{20,}','[redacted]',message)
    return diagnostic


def _provider_dataset_arguments(receipt,call):
    arguments={'datasetId':receipt['datasetId'],'offset':0,'limit':50,'clean':True}
    if call['actor']=='apify/website-content-crawler':
        arguments['fields']='url,crawl,markdown,text,fullText,metadata,createdAt,publishedAt,error,isError'
    elif call['actor'] in SOCIAL_ACTORS.values():
        arguments['fields']='url,inputUrl,facebookUrl,ownerUsername,coauthorProducers,caption,text,time,timestamp,likesCount,commentsCount,sharesCount,type,media,displayUrl,videoUrl,images,childPosts,error,errorDescription,requestErrorMessages,isError'
    elif call['actor']==SOCIAL_SEARCH_ACTORS['facebook']:
        arguments['fields']='url,facebookUrl,searchType,postId,text,time,timestamp,likes,comments,shares,media,error,errorDescription,requestErrorMessages,isError'
    elif call['actor']==SOCIAL_SEARCH_ACTORS['instagram']:
        arguments['fields']='id,type,productType,shortCode,url,inputUrl,ownerUsername,caption,timestamp,likesCount,commentsCount,displayUrl,videoUrl,images,childPosts,error,errorDescription,requestErrorMessages,isError'
    return arguments


def _build_rejection_message(message, call):
    actor = call.get('actor')
    options = call.get('callOptions')
    build = options.get('build') if isinstance(options, dict) else None
    if not all(isinstance(value, str) for value in (message, actor, build)):
        return False
    expected = (f"Failed to call Actor '{actor}': Build with number \"{build}\" was not found.\n"
                'Please verify the Actor name, input parameters, and ensure the Actor exists.')
    return message == expected


def _input_rejection_message(message,call):
    actor=call.get('actor')
    return (isinstance(actor,str) and isinstance(message,str)
        and message.startswith(f"Failed to call Actor '{actor}': Input is not valid: Field input.")
        and '\nPlease ensure the input is correct and matches the Actor\'s input schema.\nInput schema:\n```json\n' in message)


def _root_start_rejection(raw, call):
    if type(raw) is dict and set(raw) == {'error'}:
        return _build_rejection_message(raw['error'], call) or _input_rejection_message(raw['error'],call)
    prefix = "MCP tool 'call-actor' invocation failed: "
    return type(raw) is str and raw.startswith(prefix) and (
        _build_rejection_message(raw[len(prefix):], call) or _input_rejection_message(raw[len(prefix):],call))


def _rejected_start_diagnostic(raw,call):
    # Persist classifications and field names, never rejected values or the echoed schema.
    message=raw['error'] if type(raw) is dict else str(raw)
    prefix="MCP tool 'call-actor' invocation failed: "
    if message.startswith(prefix):
        message=message[len(prefix):]
    if _build_rejection_message(message,call):
        return {'reason':'build_not_found'}
    diagnostic={'reason':'invalid_input'}
    field=re.search(r'Input is not valid: Field input\.([A-Za-z_][A-Za-z0-9_.]{0,119})(?=[ :\[])',message)
    if field and not re.search(r'(?i)token|authorization|credential|password|private|cookie|secret',field[1]):
        diagnostic['input_field']=field[1]
    return diagnostic


def _check_mcp_error(value):
    if getattr(value, 'isError', False):
        raise ProviderMCPError('MCP returned a provider error')
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, dict) and (value.get('isError') or value.get('error') is not None):
        raise ProviderMCPError('MCP returned a provider error')


def _check_mcp_protocol(value):
    """Inspect protocol containers before decoding discards their block boundaries."""
    if hasattr(value, 'model_dump'):
        value = value.model_dump(mode='json')
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return
    _check_mcp_error(value)
    if isinstance(value, dict):
        if value.get('structuredContent') is not None:
            _check_mcp_protocol(value['structuredContent'])
        if 'content' in value and isinstance(value['content'], list):
            _check_mcp_protocol(value['content'])
        if value.get('type') == 'text' and 'text' in value:
            _check_mcp_protocol(value['text'])
    elif isinstance(value, list):
        for part in value:
            if _protocol_text_blocks([part]):
                _check_mcp_protocol(part)


@dataclass(frozen=True)
class ObservationResult:
    items: list
    cost: int | None
    coverage: dict | None = None


def _run_records(value):
    """A receipt must bind run identity and status in the same provider object."""
    if isinstance(value, dict):
        identity = value.get('runId') or value.get('actorRunId') or value.get('id')
        if (isinstance(identity, str) and identity and isinstance(value.get('status'), str)
                and value['status'] in {'READY', 'RUNNING', 'SUCCEEDED', 'FAILED', 'ABORTED', 'TIMED-OUT'}):
            yield value
            return
        for child in value.values():
            yield from _run_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _run_records(child)


class ApifyProvider:
    def __init__(self, tools, *, website_text_bytes=None):
        required = {'call-actor', 'get-actor-run', 'get-dataset-items'}
        if not required.issubset(tools):
            raise ValueError('Required MCP capabilities are missing')
        self.tools = tools
        if website_text_bytes is not None and (type(website_text_bytes) is not int or not 12000<=website_text_bytes<=600000):
            raise ValueError('Website text retention requires a bounded byte allowance')
        self.website_text_bytes=website_text_bytes

    async def invoke(self, name, arguments):
        raw = await self.tools[name].ainvoke(arguments)
        # The installed SDK can render ToolException as a prefixed root string.
        if name == 'call-actor' and _root_start_rejection(raw, arguments):
            raise ProviderStartRejected(_rejected_start_diagnostic(raw,arguments))
        _check_mcp_protocol(raw)
        try:
            value = unpack_run(raw) if name in {'call-actor', 'get-actor-run'} else unpack(raw)
        except RuntimeError:
            # The local decoder raises RuntimeError only for protocol error envelopes.
            raise ProviderMCPError('MCP returned an error envelope') from None
        _check_mcp_error(value)
        if isinstance(value, dict) and '_meta' in value and 'payload' in value:
            _check_mcp_error(value['payload'])
        return value

    @staticmethod
    def receipt(value, expected_id=None, *, verify_cost=True):
        meta = value.get('_meta', {}) if isinstance(value, dict) else {}
        usage = meta.get('com.apify/ActorRun') if isinstance(meta, dict) else None
        payload = value['payload'] if isinstance(value, dict) and '_meta' in value and 'payload' in value else value
        records = list(_run_records(payload))
        matches = [item for item in records
                   if expected_id is None or (item.get('runId') or item.get('actorRunId') or item['id']) == expected_id]
        if len(matches) != 1:
            raise ValueError('Provider run identity and status could not be verified together')
        run = matches[0]
        identity = run.get('runId') or run.get('actorRunId') or run['id']
        try:
            cost = _run_cost(run, usage, identity, len(records),settled=verify_cost)
        except ValueError as error:
            raise _ReceiptAccountingError(str(error)) from None
        if cost is not None:
            cost = int((cost * 1000000).to_integral_value(rounding=ROUND_CEILING))
        return {'runId': identity,
                'status': run['status'], 'datasetId': dataset_id(run), 'spent_microusd': cost}

    async def start(self, call):
        try:
            value=await self.invoke('call-actor', call)
            try:
                receipt=self.receipt(value,verify_cost=False)
            except ValueError:
                raise ProviderStartResponseError(_start_response_diagnostic(value)) from None
            # Completion can precede accounting; retain the reservation until a later read.
            return {**receipt, 'spent_microusd': None}
        except Exception as error:
            if type(error).__name__ != 'ToolException':
                raise
            from langchain_core.tools import ToolException
            if not isinstance(error, ToolException):
                raise
            if _build_rejection_message(str(error), call) or _input_rejection_message(str(error),call):
                raise ProviderStartRejected(_rejected_start_diagnostic(str(error),call)) from None
            raise

    async def read(self, run_id):
        try:
            value = await self.invoke('get-actor-run', {'runId': run_id, 'waitSecs': 0})
            receipt = self.receipt(value, run_id,verify_cost=False)
            if receipt['status'] not in {'READY', 'RUNNING'}:
                delay = _cost_settlement_delay(value, run_id)
                if delay:
                    import asyncio
                    await asyncio.sleep(delay)
                    value = await self.invoke('get-actor-run', {'runId': run_id, 'waitSecs': 0})
                    receipt = self.receipt(value, run_id,verify_cost=False)
                    if receipt['status'] in {'READY','RUNNING'}:
                        return receipt
                    remaining = _cost_settlement_delay(value, run_id)
                    if remaining and _run_completion(value, run_id) is not None:
                        raise ProviderReceiptUnverified('Provider completion changed during accounting verification; resume the same capture')
                receipt=self.receipt(value,run_id)
                if receipt.get('spent_microusd') is None:
                    raise ProviderReceiptUnverified('The existing capture has no verified cost; its reservation remains held')
        except _ReceiptAccountingError:
            raise ProviderReceiptUnverified('The known capture accounting is not yet consistent; resume the same capture') from None
        except ProviderMCPError:
            raise ProviderStatusUnavailable('MCP returned an error envelope; provider status is unavailable and the existing operation remains pending') from None
        return receipt

    async def observations(self, receipt, call, *, allow_partial_pages=False):
        if type(allow_partial_pages) is not bool:
            raise ValueError('Partial page retrieval must be selected by the runtime')
        if not receipt.get('datasetId'):
            receipt = await self.read(receipt['runId'])
        if receipt['status'] != 'SUCCEEDED' or not receipt.get('datasetId'):
            raise RuntimeError('The successful run has no verified dataset')
        rows = await self.dataset_rows(receipt,call)
        if call['actor'] in {*SOCIAL_ACTORS.values(),*SOCIAL_SEARCH_ACTORS.values(),'apidojo/twitter-scraper-lite'}:
            return _social_result(rows,receipt,call)
        if call['actor'] == SERP_ACTOR:
            return ObservationResult(serp_observations(rows, receipt['runId'], call), receipt.get('spent_microusd'))
        if call['actor'] == 'apify/web-search':
            return ObservationResult(_localized_search_observations(rows, receipt['runId'], call), receipt.get('spent_microusd'))
        if call['actor'] == 'apify/website-content-crawler':
            return _website_result(rows, receipt, call, allow_partial_pages, self.website_text_bytes)
        if call['actor'] in {'apify/rag-web-browser', 'streamers/youtube-scraper'}:
            limit = call['callOptions']['maxItems']
            if type(limit) is not int or not 1 <= limit <= 50 or len(rows) > limit:
                raise ValueError('Provider dataset exceeds the admitted row limit')
            if call['actor'] == 'apify/rag-web-browser':
                items, coverage = _web_search_observations(rows, receipt['runId'])
                return ObservationResult(items, receipt.get('spent_microusd'), coverage)
            return _youtube_result(rows,receipt,call)
        if call['actor'] in ACTORS.values():
            return _financial_result(rows, receipt, call)
        return ObservationResult([_observation(row, call['actor'], receipt['runId']) for row in rows[:50]],
                                 receipt.get('spent_microusd'))

    async def dataset_rows(self,receipt,call):
        arguments=_provider_dataset_arguments(receipt,call)
        if call['actor'] in {SOCIAL_ACTORS['facebook'],SOCIAL_SEARCH_ACTORS['facebook']}:
            return await self.facebook_rows(arguments,call)
        return _items(await self.invoke('get-dataset-items',arguments))

    async def facebook_rows(self,arguments,call):
        # Native video metadata is large. Page the same dataset, without starting captures.
        maximum=call['callOptions'].get('maxItems',call['input'].get('resultsLimit'))
        if type(maximum) is not int or not 1<=maximum<=50:
            raise ValueError('Facebook requires a bounded dataset read')
        rows=[]
        while len(rows)<=maximum:
            limit=min(3,maximum+1-len(rows))
            page=_items(await self.invoke('get-dataset-items',{**arguments,'offset':len(rows),'limit':limit}))
            rows.extend(page)
            if len(page)!=limit:
                break
        return rows


def _social_result(rows,receipt,call):
    if call['actor'] in SOCIAL_ACTORS.values():
        items=social_observations(rows,receipt,call)
    elif call['actor']==SOCIAL_SEARCH_ACTORS['facebook']:
        items=facebook_search_observations(rows,receipt,call)
    elif call['actor']==SOCIAL_SEARCH_ACTORS['instagram']:
        items=instagram_search_observations(rows,receipt,call)
    else:
        items=_x_observations(rows,receipt,call)
    coverage=None
    if call['actor'] in SOCIAL_SEARCH_ACTORS.values() and not any(row.get('error')=='no_items' for row in rows):
        omitted=len(rows)-len(items)
        if omitted:
            coverage={'omitted_textless_items':omitted}
    return ObservationResult(items,receipt.get('spent_microusd'),coverage)


def _x_observations(rows,receipt,call):
    limit=call.get('input',{}).get('maxItems',call['callOptions'].get('maxItems'))
    if type(limit) is not int or not 1<=limit<=50 or len(rows)>limit:
        raise ValueError('X search exceeds the admitted post limit')
    results=[]
    for row in rows:
        if not isinstance(row,dict) or row.get('error') or row.get('isError'):
            raise ValueError('X search contains a provider error')
        media=retrieved_media(row,'x')
        text,basis=media_post_text(row.get('text') or row.get('fullText'),media)
        item=_observation({**row,'text':text},call['actor'],receipt['runId'])
        uri=urlsplit(item['url'])
        if uri.hostname!='x.com' or not re.fullmatch(r'/[A-Za-z0-9_]{1,15}/status/[0-9]+',uri.path):
            raise ValueError('X search requires a public post identity')
        if row.get('twitterUrl') and public_url(row['twitterUrl'])!=item['url']:
            raise ValueError('X post identity disagrees with its alternate URL')
        item['provenance'].update(media,content_basis=basis,scope='x_keyword_posts',comments_retrieved=False)
        results.append(item)
    if len({row['url'] for row in results})!=len(results):
        raise ValueError('X search repeats a post identity')
    return results


def _run_completion(value, run_id):
    payload = value['payload'] if isinstance(value, dict) and '_meta' in value and 'payload' in value else value
    run = next(item for item in _run_records(payload)
               if (item.get('runId') or item.get('actorRunId') or item.get('id')) == run_id)
    return run.get('finishedAt')


def _cost_settlement_delay(value, run_id):
    """Apify documents preliminary completion costs and a read about ten seconds later."""
    finished = _run_completion(value, run_id)
    if finished is None:
        return 10
    if not isinstance(finished, str):
        raise ProviderReceiptUnverified('Provider completion time must be an explicit timestamp')
    try:
        moment = datetime.fromisoformat(finished.replace('Z', '+00:00'))
    except ValueError:
        raise ProviderReceiptUnverified('Provider completion time is not a valid timestamp') from None
    if moment.tzinfo is None:
        raise ProviderReceiptUnverified('Provider completion time must include its timezone')
    age = (datetime.now(timezone.utc) - moment).total_seconds()
    if age < 0:
        raise ProviderReceiptUnverified('Provider completion time is in the future')
    return max(0, 10 - age)


def _website_result(rows, receipt, call, allow_partial_pages, text_bytes=None):
    if allow_partial_pages:
        items, coverage = _discovered_page_observations(rows, receipt['runId'], call, text_bytes)
        return ObservationResult(items, receipt.get('spent_microusd'), coverage)
    items = _website_observations(rows, receipt['runId'], call, text_bytes)
    return ObservationResult(items, receipt.get('spent_microusd'),
        {'version': 1, 'total_rows': len(rows), 'accepted_rows': len(items), 'failed_pages': []})


def _financial_result(rows, receipt, call):
    limit=call['callOptions'].get('maxItems',call.get('input',{}).get('maxResults'))
    if type(limit) is not int or not 1<=limit<=50 or len(rows) > limit:
        raise ValueError('Financial dataset exceeds the admitted row limit')
    observed_call = {**call, 'observed_at': datetime.now(timezone.utc).isoformat()}
    observations = [item for row in rows for item in normalize_finance(row, call['actor'], receipt['runId'], observed_call)]
    if len(observations) > 50:
        raise ValueError('Financial evidence exceeds the bounded observation limit')
    return ObservationResult(observations, receipt.get('spent_microusd'))


def _run_cost(run, usage, identity, run_count, *, settled=True):
    """Reconcile total-cost channels only after the payload run is identified."""
    if usage is not None and (not isinstance(usage, dict) or run_count != 1):
        raise ValueError('Provider usage metadata has no unique run')
    if usage is not None and any(usage[key] != identity for key in ('runId', 'actorRunId', 'id') if key in usage):
        raise ValueError('Provider usage metadata belongs to another run')
    if not settled:
        return None
    cost = _cost_amount(run.get('usageTotalUsd'))
    if usage is None:
        return cost
    metadata_cost = _cost_amount(usage.get('usageTotalUsd'))
    if metadata_cost is None:
        return cost
    if cost not in (None, metadata_cost):
        raise ValueError('Provider run cost disagrees with its usage metadata')
    return metadata_cost


def _cost_amount(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError('Provider cost is not a finite nonnegative amount')
    try:
        amount = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('Provider cost is not a finite nonnegative amount') from exc
    if not amount.is_finite() or amount < 0:
        raise ValueError('Provider cost is not a finite nonnegative amount')
    return amount


def _dataset_notice_payload(value):
    envelopes=[row for row in value if isinstance(row,dict) and 'datasetId' in row and 'items' in row]
    if not envelopes:
        return None
    payload=envelopes[0]
    notice='\n'.join(payload[key] for key in ('summary','nextStep') if isinstance(payload.get(key),str) and payload[key])
    if len(envelopes)==1 and (len(value)==1 or len(value)==2 and notice and notice in value):
        return payload
    raise ValueError('Dataset response contains conflicting or unsupported envelopes')


def _items(value):
    if isinstance(value, list):
        payload=_dataset_notice_payload(value)
        if payload is not None:
            return _items(payload)
        if len(value) == 1 and isinstance(value[0], dict) and any(key in value[0] for key in ('items', 'data', 'datasetItems')):
            return _items(value[0])
        if not all(isinstance(row, dict) for row in value):
            raise ValueError('Dataset items must be objects')
        return value
    if isinstance(value, dict):
        for name in ('items', 'data', 'datasetItems'):
            if name in value:
                return _items(value[name])
    raise ValueError('Dataset response has no verified item list')


def _observation(row, actor, run_id, text_bytes=None):
    url = public_url(row.get('url') or row.get('twitterUrl'))
    text = row.get('markdown') or row.get('text') or row.get('fullText')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('Provider item has no textual evidence')
    published = row.get('createdAt') or row.get('publishedAt')
    if published:
        try:
            moment = datetime.fromisoformat(published.replace('Z', '+00:00'))
        except ValueError:
            moment = datetime.strptime(published, '%a %b %d %H:%M:%S %z %Y')
        if moment.tzinfo is None:
            raise ValueError('Provider date lacks a timezone')
        published = moment.astimezone(timezone.utc).isoformat()
    retained=text[:12000] if text_bytes is None else text.encode('utf-8')[:text_bytes].decode('utf-8',errors='ignore')
    return {'url': url, 'text': retained, 'published_at': published,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'metrics': {},
            'provenance': {'source': actor, 'provider_run_id': run_id, 'scope': 'bounded_dataset_sample', 'location': None,
                           'text_truncated': len(retained)<len(text), 'original_character_count': len(text)}}


def _website_observations(rows, run_id, call, text_bytes=None):
    requested = {public_url(item['url']) for item in call['input']['startUrls']}
    limit = call['callOptions']['maxItems']
    if type(limit) is not int or not 1 <= limit <= 50 or len(rows) > min(limit, len(requested)):
        raise ValueError('Website dataset exceeds the admitted row limit')
    seen, observations = set(), []
    for row in rows:
        queued = public_url(row.get('url'))
        if queued not in requested or queued in seen:
            raise ValueError('Website dataset contains an unrequested or duplicate page')
        loaded = _website_destination(row)
        if urlsplit(loaded).netloc != urlsplit(queued).netloc:
            raise ValueError('Website redirect changes the authorized origin')
        item = _observation({**row, 'url': loaded}, 'apify/website-content-crawler', run_id, text_bytes)
        if _access_challenge(item['text']):
            raise ValueError('Website returned an access challenge instead of the requested content')
        item['provenance'].update(scope='requested_pages', requested_url=queued,
                                  redirected=loaded != queued, **retrieved_media(row,'web'))
        observations.append(item)
        seen.add(queued)
    return observations


def _website_destination(row):
    crawl = row.get('crawl')
    if (not isinstance(crawl, dict) or type(crawl.get('depth')) is not int or crawl['depth'] != 0
            or row.get('error') or row.get('isError') or crawl.get('error')):
        raise ValueError('Website item lacks a successful depth-zero crawl')
    if 'httpStatusCode' in crawl:
        status = crawl['httpStatusCode']
        if type(status) is not int or not 200 <= status < 300:
            raise ValueError('Website page was not successfully retrieved')
    return public_url(crawl.get('loadedUrl'))


def _discovered_crawl(row):
    crawl = row.get('crawl')
    if not isinstance(crawl, dict) or type(crawl.get('depth')) is not int or crawl['depth'] != 0:
        raise ValueError('Discovered page lacks a depth-zero crawl')
    status = crawl.get('httpStatusCode')
    if type(status) is not int or not (200 <= status < 300 or 400 <= status < 600):
        raise ValueError('Discovered page has unverified HTTP status')
    if (row.get('error') is not None or crawl.get('error') is not None or row.get('isError')
            or 'isError' in row and type(row['isError']) is not bool):
        raise ValueError('Discovered page has an ambiguous provider error')
    return crawl, status


def _discovered_page_status(row, requested, seen):
    queued = public_url(row.get('url'))
    if queued not in requested or queued in seen:
        raise ValueError('Website dataset contains an unrequested or duplicate page')
    crawl, status = _discovered_crawl(row)
    origin_changed=False
    if 'loadedUrl' in crawl:
        loaded = public_url(crawl['loadedUrl'])
        origin_changed=urlsplit(loaded).netloc != urlsplit(queued).netloc
    seen.add(queued)
    return queued, status, origin_changed


def _discovered_page_text(row):
    for key in ('markdown', 'text', 'fullText'):
        if row.get(key) is not None and not isinstance(row[key], str):
            raise ValueError('Discovered page text has an invalid type')
    return row.get('markdown') or row.get('text') or row.get('fullText')


def _access_challenge(text):
    if not isinstance(text,str) or len(text)>2000:
        return False
    heading=text.lstrip().split('\n',1)[0].lstrip('# ').strip().lower()
    return (heading in {'bot detection','just a moment...','access denied','verify you are human'}
            and any(marker in text.lower() for marker in ('computing proof of work','enable javascript and cookies',
                                                         'verify you are human','checking your browser')))


def _discovered_page_observations(rows, run_id, call, text_bytes=None):
    requested = {public_url(item['url']) for item in call['input']['startUrls']}
    limit = call['callOptions']['maxItems']
    if type(limit) is not int or not 1 <= limit <= 50 or len(rows) > min(limit, len(requested)):
        raise ValueError('Website dataset exceeds the admitted row limit')
    seen, items, failures = set(), [], []
    for row in rows:
        queued, status, origin_changed = _discovered_page_status(row, requested, seen)
        if origin_changed:
            failures.append({'url':queued,'reason':'origin_changed' if status<400 else 'http_error','http_status':status})
            continue
        text = _discovered_page_text(row)
        if _access_challenge(text):
            failures.append({'url':queued,'reason':'access_challenge','http_status':status})
        elif status >= 400 or text is None or not text.strip():
            failures.append({'url': queued, 'reason': 'http_error' if status >= 400 else 'empty_content',
                             'http_status': status})
        else:
            items.extend(_website_observations([row], run_id, call, text_bytes))
    return items, {'version': 1, 'total_rows': len(rows), 'accepted_rows': len(items), 'failed_pages': failures}


def _web_search_page(row):
    crawl, metadata = row.get('crawl'), row.get('metadata')
    if not isinstance(crawl, dict) or not isinstance(metadata, dict):
        raise ValueError('Web search item lacks verified crawl metadata')
    status = crawl.get('httpStatusCode')
    request_status = crawl.get('requestStatus', 'handled')
    if (type(status) is not int or not (200 <= status < 300 or 400 <= status < 600)
            or not isinstance(request_status, str) or request_status not in {'handled', 'failed'}
            or row.get('error') or row.get('isError') or crawl.get('error')):
        raise ValueError('Web search page has unverified retrieval metadata')
    url = public_url(metadata.get('url'))
    text = row.get('markdown')
    if text is not None and not isinstance(text, str):
        raise ValueError('Web search item has no extracted Markdown evidence')
    if status < 300 and request_status != 'handled':
        raise ValueError('Web search page has inconsistent retrieval metadata')
    return url, status, text


def _web_search_observations(rows, run_id):
    items, failures = [], []
    for row in rows:
        url, status, text = _web_search_page(row)
        if status >= 400 or text is None or not text.strip():
            failures.append({'url': url, 'reason': 'http_error' if status >= 400 else 'empty_content',
                             'http_status': status})
        else:
            items.append(_web_search_observation(url, text, run_id))
    return items, {'version': 1, 'total_rows': len(rows), 'accepted_rows': len(items), 'failed_pages': failures}


def _web_search_observation(url, text, run_id):
    return {'url': url, 'text': text[:12000], 'published_at': None,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'metrics': {},
            'provenance': {'source': 'apify/rag-web-browser', 'provider_run_id': run_id,
                'scope': 'bounded_web_search_results', 'location': None,
                'temporal_basis': 'reference_capture', 'text_truncated': len(text) > 12000,
                'original_character_count': len(text)}}


def _search_day(value):
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Search date must be an exact calendar day')
    datetime.strptime(value, '%Y-%m-%d')
    return value


def _search_hostname(value):
    if not isinstance(value, str) or not value or any(char in value for char in '/?#:@'):
        raise ValueError('Search domain must be a public hostname')
    return urlsplit(public_url('https://' + value)).hostname


def _localized_search_context(row, call):
    if not isinstance(row, dict) or set(row) != {'query', 'search', 'results'} or row['query'] != call['input']['query']:
        raise ValueError('Search response must bind exactly one query, search metadata and results')
    search = row['search']
    required = {'provider', 'searchedAt', 'durationSecs', 'correctedQuery', 'suggestedQuery', 'totalResults',
                'countryCode', 'languageCode', 'location', 'providerDomain', 'dateFrom', 'dateTo', 'checkUrl'}
    if not isinstance(search, dict) or not required.issubset(search):
        raise ValueError('Search response has incomplete localization metadata')
    _check_mcp_error(search)
    keys = ('provider', 'countryCode', 'languageCode', 'dateFrom', 'dateTo')
    requested = {key: call['input'].get(key) for key in keys}
    if search['provider'] != 'google' or any(search[key] != requested[key] for key in keys):
        raise ValueError('Search response differs from its approved provider, locale or dates')
    _search_day(search['dateFrom'])
    _search_day(search['dateTo'])
    return {**_search_response_metadata(search), 'query': row['query'], 'requested_search': requested,
            'effective_search': {key: search[key] for key in keys}, 'actor_build': call['callOptions']['build']}


def _search_response_metadata(search):
    moment = search['searchedAt']
    if not isinstance(moment, str):
        raise ValueError('Search clock requires an explicit timezone')
    moment = datetime.fromisoformat(moment.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('Search clock requires an explicit timezone')
    duration, estimate = search['durationSecs'], search['totalResults']
    if type(duration) not in (int, float) or not Decimal(str(duration)).is_finite() or duration < 0:
        raise ValueError('Search duration must be finite and nonnegative')
    if estimate is not None and (type(estimate) is not int or estimate < 0):
        raise ValueError('Search total is an optional nonnegative estimate')
    if search['location'] is not None:
        raise ValueError('Search returned an unrequested location')
    return {'provider_searched_at': moment.astimezone(timezone.utc).isoformat(),
            'provider_duration_seconds': duration, 'provider_domain': _search_hostname(search['providerDomain']),
            'search_check_url': public_url(search['checkUrl']), 'total_results_estimate': estimate,
            'corrected_query': _search_query_correction(search['correctedQuery']),
            'suggested_query': _search_query_correction(search['suggestedQuery'])}


def _search_query_correction(value):
    if value is not None and (not isinstance(value, str) or len(value) > 500):
        raise ValueError('Search query correction must be a bounded optional string')
    return value


def _localized_search_observations(rows, run_id, call):
    if len(rows) != 1:
        raise ValueError('Localized search must return exactly one dataset item')
    metadata = _localized_search_context(rows[0], call)
    results, limit = rows[0]['results'], call['callOptions']['maxItems']
    if (type(limit) is not int or not 1 <= limit <= 3 or type(call['input']['maxResults']) is not int or call['input']['maxResults'] != limit
            or not isinstance(results, list) or len(results) > limit):
        raise ValueError('Localized search exceeds its admitted result limit')
    positions = [row.get('position') for row in results if isinstance(row, dict)]
    if any(type(position) is not int for position in positions) or positions != list(range(1, len(results) + 1)):
        raise ValueError('Search result positions must be dense unique integers')
    observed_at = datetime.now(timezone.utc).isoformat()
    return [_localized_search_observation(row, run_id, metadata, observed_at, len(results)) for row in results]


def _localized_search_observation(row, run_id, metadata, observed_at, result_count):
    _check_mcp_error(row)
    url = public_url(row.get('url'))
    if _search_hostname(row.get('domain')) != urlsplit(url).hostname:
        raise ValueError('Search result domain differs from its destination URL')
    title, snippet = row.get('title'), row.get('snippet')
    if not isinstance(title, str) or not title.strip() or len(title) > 1000 or 'snippet' not in row:
        raise ValueError('Search result needs a bounded title and optional snippet')
    if snippet is not None and (not isinstance(snippet, str) or len(snippet) > 12000):
        raise ValueError('Search snippet exceeds its bounded metadata contract')
    day = _search_day(row.get('date'))
    text = title + ('\n\n' + snippet if snippet else '')
    return {'url': url, 'text': text[:12000], 'published_at': None, 'observed_at': observed_at, 'metrics': {},
        'provenance': {**metadata, 'source': 'apify/web-search', 'provider_run_id': run_id,
            'scope': 'bounded_search_results', 'location': None, 'content_basis': 'search_title_and_snippet',
            'temporal_basis': 'search_metadata', 'publication_date_raw': day,
            'publication_date_precision': 'day' if day else 'unknown', 'position': row['position'],
            'returned_results_count': result_count, 'text_truncated': len(text) > 12000,
            'original_character_count': len(text)}}


def _youtube_url(row):
    url = urlsplit(public_url(row.get('url')))
    video_id = row.get('id')
    if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
        raise ValueError('YouTube metadata requires a valid video identity')
    if (url.hostname not in {'youtube.com', 'www.youtube.com', 'm.youtube.com'}
            or url.path != '/watch' or parse_qs(url.query, keep_blank_values=True).get('v') != [video_id]):
        raise ValueError('YouTube watch URL and video identity do not match')
    return 'https://www.youtube.com/watch?v=' + video_id


def _youtube_date(value):
    if value is None:
        return None, 'unknown'
    if not isinstance(value, str) or not 1 <= len(value) <= 100 or any(ord(char) < 32 for char in value):
        raise ValueError('YouTube publication date must be bounded text or unknown')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        datetime.strptime(value, '%Y-%m-%d')
        return None, 'day'
    try:
        moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None, 'relative_or_unknown'
    if moment.tzinfo is None:
        return None, 'datetime_without_timezone'
    return moment.astimezone(timezone.utc).isoformat(), 'timestamp'


def _youtube_result(rows,receipt,call):
    items=[] if _youtube_empty_search(rows,call) else [
        _youtube_observation(row,call['actor'],receipt['runId']) for row in rows]
    return ObservationResult(items,receipt.get('spent_microusd'))


def _youtube_empty_search(rows,call):
    if len(rows)!=1 or rows[0].get('error')!='NO_VIDEOS':
        return False
    row=rows[0]
    query=_youtube_empty_query(row,call)
    parsed=urlsplit(public_url(row['url']))
    parameters=parse_qs(parsed.query,keep_blank_values=True)
    if (parsed.hostname not in {'www.youtube.com','youtube.com'} or parsed.path!='/results'
            or set(parameters)!={'search_query','sp'} or parameters['search_query']!=[query]
            or parameters['sp']!=['CAISBAgDEAE=']
            or call['input'].get('dateFilter')!='week' or call['input'].get('sortingOrder')!='date'):
        raise ValueError('Empty YouTube search URL escaped its admitted source and filters')
    return True


def _youtube_empty_query(row,call):
    query=row.get('input')
    if (set(row)!={'url','input','error','note'} or not isinstance(query,str) or not query
            or row['note']!='No videos found on the page.'
            or call.get('input',{}).get('searchQueries')!=[query]
            or call['input'].get('startUrls')!=[]):
        raise ValueError('Empty YouTube search must match its admitted query')
    return query


def _youtube_observation(row, actor, run_id):
    if row.get('error') or row.get('isError') or row.get('type', 'video') != 'video':
        raise ValueError('YouTube item is not verified regular-video metadata')
    url = _youtube_url(row)
    title, description = row.get('title'), row.get('text')
    if not isinstance(title, str) or not title.strip():
        raise ValueError('YouTube video metadata lacks a title')
    if description is not None and not isinstance(description, str):
        raise ValueError('YouTube video description must be text or unknown')
    metrics = {key: row.get(key) for key in ('viewCount', 'likes', 'commentsCount')}
    if any(value is not None and (type(value) is not int or value < 0) for value in metrics.values()):
        raise ValueError('YouTube counters must be nonnegative integers or unknown')
    channel = {key: row.get(key) for key in ('channelName', 'channelUrl')}
    if any(value is not None and not isinstance(value, str) for value in channel.values()):
        raise ValueError('YouTube channel metadata must be text or unknown')
    published, precision = _youtube_date(row.get('date'))
    text = title + ('\n\n' + description if description else '')
    return {'url': url, 'text': text[:12000], 'published_at': published,
            'observed_at': datetime.now(timezone.utc).isoformat(), 'metrics': metrics,
            'provenance': {**retrieved_media(row,'youtube'), 'source': actor, 'provider_run_id': run_id,
                'scope': 'bounded_youtube_search_results', 'location': None,
                'content_basis': 'title_and_description', 'video_content_analyzed': False,
                'transcript_retrieved': False, 'comments_retrieved': False,
                'publication_date_raw': row.get('date'), 'publication_date_precision': precision,
                'channel_name': channel['channelName'], 'channel_url': channel['channelUrl'],
                'source_title': title[:1000],
                'source_description': description[:max(0,12000-len(title)-2)] if description is not None else None,
                'text_truncated': len(text) > 12000, 'original_character_count': len(text)}}
