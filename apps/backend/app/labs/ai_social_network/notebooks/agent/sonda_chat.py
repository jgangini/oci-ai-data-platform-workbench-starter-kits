"""Read-only Sonda chat helpers; native setup, query execution and memory are adapters.

Readers bind a published Delta version map and an approved configuration snapshot.
Discovery EXISTS filters preserve exact observation provenance without multiplying
documents found by several queries. No function invokes SQL, a model or a provider.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import UUID, uuid5

from app.sonda_contract import (ChatInput, ConfigMessage, canonical_utc, encoded,
    no_duplicate_keys, public_reference, sha256)
from app.labs.ai_social_network.notebooks.agent.social_agent import (TOOLS, NETWORKS, PROMPT,
    literal, canonical_platform, verified_plan, plan_for_question, build_visualization,
    resolved_args, checked_plan)

TABLES = ('registry', 'sources', 'queries', 'document_current', 'observations',
          'discoveries', 'evaluation_results', 'reports', 'daily')
METRICS = {'posts':None, 'comments':'comments', 'likes':'likes', 'shares':'shares', 'views':'views'}
SONDA_PROMPT = PROMPT + '\nSonda v2: los filtros de fuentes/consultas aprobadas se aplican siempre al SQL. '
SONDA_PROMPT += ('document_type=post cuenta publicaciones; comment consulta comentarios capturados; page e indicator son documentos web. '
    'No confundas el contador de comentarios de una publicación con textos de comentarios capturados. '
    'Las cifras y referencias pertenecen a una sola publicación Gold y generación; no reutilices resultados de otra generación.')


@dataclass(frozen=True)
class BoundChat:
    question: str
    registry_id: str
    generation_id: str
    config_revision: int
    source_ids: tuple
    query_ids: tuple
    selected_source_ids: tuple
    selected_query_ids: tuple

    def search_context(self):
        return {'registry_id':self.registry_id, 'generation_id':self.generation_id,
            'config_revision':self.config_revision, 'source_ids':list(self.selected_source_ids),
            'query_ids':list(self.selected_query_ids)}


def checked_binding(binding):
    for key in ('registry_id','generation_id'):
        if not isinstance(binding.get(key),str) or str(UUID(binding[key]))!=binding[key]:
            raise ValueError('Binding requires canonical UUIDs')
    for key in ('catalog','gold_schema','control_schema'):
        if not isinstance(binding.get(key),str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,254}',binding[key]):
            raise ValueError('Invalid bound serving identifier')
    if binding['gold_schema']==binding['control_schema'] or not isinstance(binding.get('contract_sha256'),str) or not re.fullmatch('[0-9a-f]{64}',binding['contract_sha256']):
        raise ValueError('A bound control namespace and pinned contract are required')


def table_name(binding,name):
    checked_binding(binding)
    if name not in (*TABLES,'publication'):
        raise ValueError('Unknown fixed Sonda table')
    schema=binding['control_schema'] if name=='publication' else binding['gold_schema']
    return '.'.join((binding['catalog'],schema,'ai_social_'+name))


def scope_sql(binding, alias):
    if alias not in ('p','c','d','s'):
        raise ValueError('Unknown fixed SQL alias')
    return alias + '.registry_id=' + literal(binding['registry_id']) + ' AND ' + alias + '.generation_id=' + literal(binding['generation_id'])


def relation(binding, snapshot, name):
    checked_binding(binding)
    if any(snapshot.get(key)!=binding[key] for key in ('registry_id','generation_id')):
        raise ValueError('Serving snapshot belongs to another generation')
    if name not in TABLES:
        raise ValueError('Unknown Sonda serving table')
    version = snapshot.get('versions', {}).get('ai_social_' + name)
    if type(version) is not int or version < 0:
        raise ValueError('Serving table has no committed Delta version')
    return table_name(binding,name) + ' VERSION AS OF ' + str(version)


def snapshot_sql(binding):
    table = table_name(binding,'publication')
    return 'SELECT to_json(struct(p.*)) AS payload FROM ' + table + ' p WHERE ' + scope_sql(binding,'p') + ' LIMIT 2'


def snapshot_from_row(binding, row):
    checked_binding(binding)
    if hasattr(row,'asDict'):
        row = row.asDict(recursive=True)
    if not isinstance(row,dict) or any(row.get(key) != binding[key] for key in ('registry_id','generation_id')):
        raise ValueError('Publication belongs to another registry/generation')
    revision = row.get('config_revision')
    if type(revision) is not int or revision < 1:
        raise ValueError('Publication has no configuration revision')
    versions = row.get('versions_json')
    if not isinstance(versions,str) or len(versions.encode('utf-8')) > 65536:
        raise ValueError('Invalid publication version map')
    versions = json.loads(versions,object_pairs_hook=no_duplicate_keys)
    if (not isinstance(versions,dict) or not {'ai_social_'+key for key in TABLES} <= set(versions)
            or any(not re.fullmatch(r'ai_social_[a-z_]+',key) for key in versions)
            or any(type(value) is not int or value < 0 for value in versions.values())):
        raise ValueError('Publication lacks exact known serving snapshots')
    expected = sha256(encoded([{key:binding[key] for key in ('registry_id','generation_id')},revision,versions]))
    if row.get('publication_id') != expected:
        raise ValueError('Publication identity does not bind its committed versions')
    timestamp = row.get('published_at')
    if isinstance(timestamp,datetime):
        # Spark TIMESTAMP is returned naive in the runner's required UTC session.
        timestamp = timestamp.replace(tzinfo=timezone.utc) if timestamp.tzinfo is None else timestamp.astimezone(timezone.utc)
        timestamp = timestamp.isoformat()
    if not isinstance(timestamp,str):
        raise ValueError('Publication requires its real publication time')
    timestamp = canonical_utc(timestamp)
    metadata = {'module_id':'ai_social_network', 'publication_id':expected, 'published_at':timestamp,
        'registry_id':binding['registry_id'], 'generation_id':binding['generation_id'], 'config_revision':revision}
    for field in ('pending_files','failed_files'):
        value = row.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 9007199254740991):
            raise ValueError('Invalid publication operational counter')
        metadata[field] = value
    return {**metadata,'versions':versions,
        'current_version':versions.get('ai_social_document_current'),
        'history_version':versions.get('ai_social_observations'),
        'daily_version':versions.get('ai_social_daily')}


def configuration_sql(binding, snapshot):
    if type(snapshot.get('config_revision')) is not int or snapshot['config_revision']<1:
        raise ValueError('A committed integer configuration revision is required')
    return ('SELECT c.message_json AS payload FROM ' + relation(binding,snapshot,'registry')
        + ' c WHERE ' + scope_sql(binding,'c') + ' AND c.config_revision=' + str(snapshot['config_revision']) + ' LIMIT 2')


def validate_input(value, binding, snapshot, configuration):
    checked_binding(binding)
    if isinstance(value,str):
        if len(value.encode('utf-8')) > 131072:
            raise ValueError('Chat input exceeds its bounded envelope')
        value = json.loads(value,object_pairs_hook=no_duplicate_keys)
    if not isinstance(value,dict) or type(value.get('version')) is not int:
        raise ValueError('Sonda requires exact version 2 input')
    parsed = ChatInput.model_validate(value).model_dump(mode='json')
    context = parsed['search_context']
    if not parsed['question'].strip() or any(context[key] != binding[key] or snapshot.get(key) != binding[key]
            for key in ('registry_id','generation_id')):
        raise ValueError('Chat is outside the bound generation')
    if context['config_revision'] != snapshot['config_revision']:
        raise ValueError('Refresh the stale search configuration before querying')
    config = ConfigMessage.model_validate(configuration).model_dump(mode='json')
    if (config['contract_sha256'] != binding['contract_sha256'] or any(config[key] != binding[key] for key in ('registry_id','generation_id'))
            or config['payload']['revision'] != context['config_revision']):
        raise ValueError('Gold configuration does not match the selected publication')
    current = config['payload']
    approved = lambda row:row['status'] in ('active','paused') and row['approval']['status']=='approved'
    sources = {row['source_id']:row for row in current['sources'] if approved(row)}
    queries = {row['query_id']:row for row in current['queries'] if approved(row) and row['source_id'] in sources}
    selected_sources, selected_queries = context['source_ids'], context['query_ids']
    if (len(set(selected_sources)) != len(selected_sources) or len(set(selected_queries)) != len(selected_queries)
            or not set(selected_sources)<=sources.keys() or not set(selected_queries)<=queries.keys()):
        raise ValueError('Search references unknown or unapproved sources/queries')
    if selected_sources and any(queries[key]['source_id'] not in selected_sources for key in selected_queries):
        raise ValueError('Selected queries do not belong to the selected sources')
    allowed_sources = set(selected_sources) if selected_sources else set(sources)
    allowed_queries = [key for key,row in queries.items() if key in (selected_queries or queries) and row['source_id'] in allowed_sources]
    return BoundChat(parsed['question'],binding['registry_id'],binding['generation_id'],context['config_revision'],
        tuple(sorted(allowed_sources)),tuple(sorted(allowed_queries)),tuple(selected_sources),tuple(selected_queries))


def session_key(binding, native_session):
    checked_binding(binding)
    if not isinstance(native_session,str) or not 1 <= len(native_session) <= 500:
        raise ValueError('A native conversation session is required')
    return str(uuid5(UUID(binding['generation_id']), encoded(['sonda-chat-v2',binding['registry_id'],native_session]).decode('utf-8')))


def resolved_args_v2(snapshot, name, args):
    if not isinstance(args,dict):
        raise ValueError('Invalid Sonda filters')
    document_type = args.get('document_type','comment' if name=='comments' else 'post')
    if document_type not in ('post','comment','page','indicator') or name=='comments' and document_type!='comment':
        raise ValueError('Invalid document type for the selected tool')
    if name=='trend' and document_type not in ('post','comment'):
        raise ValueError('Network series require social posts or captured comments, not unnetworked web documents')
    values = resolved_args(snapshot,name,{key:value for key,value in args.items() if key!='document_type'})
    return {**values,'document_type':document_type}


def checked_plan_v2(plan, queries, snapshot):
    if not isinstance(plan,dict) or not isinstance(plan.get('args'),dict):
        raise ValueError('Invalid Sonda plan')
    # Reuse the existing structural/finish validation, retaining document-type identity.
    checked_plan({**plan,'args':{k:v for k,v in plan['args'].items() if k!='document_type'}},queries)
    if plan['tool']=='finish':
        return plan
    args=resolved_args_v2(snapshot,plan['tool'],plan['args'])
    if len(queries)>=4 or any(item['tool']==plan['tool'] and resolved_args_v2(snapshot,item['tool'],item['args'])==args for item in queries):
        return {'tool':'finish','args':{}}
    return {**plan,'args':args}


def checked_scope(binding, snapshot, chat):
    if not isinstance(chat,BoundChat) or any(getattr(chat,key) != binding[key] or snapshot.get(key) != binding[key]
            for key in ('registry_id','generation_id')) or chat.config_revision != snapshot['config_revision']:
        raise ValueError('Query context is not bound to the published configuration')


def document_conditions(binding, snapshot, chat, name, args):
    checks = [scope_sql(binding,'d'), 'd.document_type=' + literal(args['document_type'])]
    discovery = [scope_sql(binding,'s'), 's.observation_key=d.observation_key', 's.config_revision<='+str(chat.config_revision)]
    if not chat.source_ids or not chat.query_ids:
        checks.append('FALSE')
    else:
        # Read compact approved revision tables rather than expanding 10k IDs into SQL.
        approved="c.config_revision="+str(chat.config_revision)+" AND c.status IN ('active','paused') AND c.approval_status='approved'"
        discovery.extend(['s.source_id IN (SELECT c.record_id FROM '+relation(binding,snapshot,'sources')+' c WHERE '+scope_sql(binding,'c')+' AND '+approved+')',
            'EXISTS (SELECT 1 FROM '+relation(binding,snapshot,'queries')+' c WHERE '+scope_sql(binding,'c')+' AND '+approved
                +" AND c.record_id=s.query_id AND get_json_object(c.payload_json,'$.source_id')=s.source_id)"])
        if chat.selected_source_ids:
            discovery.append('s.source_id IN ('+','.join(literal(value) for value in chat.selected_source_ids)+')')
        if chat.selected_query_ids:
            discovery.append('s.query_id IN ('+','.join(literal(value) for value in chat.selected_query_ids)+')')
        checks.append('EXISTS (SELECT 1 FROM ' + relation(binding,snapshot,'discoveries') + ' s WHERE ' + ' AND '.join(discovery) + ')')
    platform = args.get('platform')
    if platform:
        checks.append('d.platform='+literal(platform))
    if args.get('platforms'):
        checks.append('d.platform IN (' + ','.join(literal(value) for value in args['platforms']) + ')')
    if args.get('post_key'):
        if name in ('comments','history'):
            # Public post_key identifies an immutable observed version; resolve its document.
            reference='SELECT p.document_key FROM '+relation(binding,snapshot,'observations')+' p WHERE '+scope_sql(binding,'p')+' AND p.observation_key='+literal(args['post_key'])
            checks.append('d.'+('parent_document_key' if name=='comments' else 'document_key')+' IN ('+reference+')')
        else:
            checks.append('d.observation_key='+literal(args['post_key']))
    if args.get('text'):
        checks.append('instr(lower(d.text),lower('+literal(args['text'])+'))>0')
    if args.get('entity'):
        entity, mode = args['entity'],args.get('entity_scope','any')
        if mode=='author':
            checks.append("lower(regexp_replace(coalesce(d.author,''),'^@',''))=lower("+literal(entity.lstrip('@'))+')')
        else:
            field = 'd.text' if mode=='mentions' else "concat_ws(' ',d.author,d.text)" if name=='comments' else "concat_ws(' ',d.author,d.text,d.url)"
            checks.append('instr(lower('+field+'),lower('+literal(entity)+'))>0')
    if args['period']=='range':
        start,end = args['date_from'],args['date_to']
        if name=='history':
            last = datetime.fromisoformat(end)+timedelta(days=1)
            checks.extend([f"d.observation_month BETWEEN '{start[:7]}' AND '{end[:7]}'",
                f"d.observed_at >= TIMESTAMP '{start} 00:00:00'", f"d.observed_at < TIMESTAMP '{last.date()} 00:00:00'"])
        else:
            date_check = f"d.published_date BETWEEN DATE '{start}' AND DATE '{end}'"
            checks.append('('+date_check+' OR d.published_date IS NULL)' if name=='trend' else date_check)
    return checks


def sonda_trend_sql(base, args):
    networks = args.get('platforms') or ([args['platform']] if args.get('platform') else NETWORKS)
    metric = METRICS[args['metric']]
    aggregate = 'COUNT(*)' if metric is None else 'SUM('+metric+')'
    reported = 'COUNT(*)' if metric is None else 'COUNT('+metric+')'
    fields,counts,coverage = [],[],[]
    for network in networks:
        match = 'platform='+literal(network)
        value = 'MAX(CASE WHEN daily.'+match+' THEN daily.value END)'
        fields.append(('COALESCE('+value+',0)' if metric is None else value)+' AS '+network)
        counts.append('COUNT(CASE WHEN '+match+' THEN 1 END) AS '+network)
        coverage.extend(["'"+network+"'", "named_struct('post_count',COALESCE(MAX(CASE WHEN daily."+match+" THEN daily.post_count END),0),'reported_count',COALESCE(MAX(CASE WHEN daily."+match+" THEN daily.reported_count END),0),'undated_count',MAX(undated."+network+'))'])
    start,end = args['date_from'],args['date_to']
    return ('SELECT to_json(struct(*)) AS payload FROM (WITH scoped AS ('+base+'), daily AS (SELECT platform,published_date,'
        +aggregate+' AS value,COUNT(*) AS post_count,'+reported+" AS reported_count FROM scoped WHERE published_date IS NOT NULL GROUP BY platform,published_date), "
        +'undated AS (SELECT '+','.join(counts)+' FROM scoped WHERE published_date IS NULL) SELECT CAST(calendar.day AS STRING) AS period,'
        +','.join(fields)+',named_struct('+','.join(coverage)+') AS coverage FROM '
        +f"(SELECT explode(sequence(DATE '{start}',DATE '{end}',INTERVAL 1 DAY)) AS day) calendar "
        +'LEFT JOIN daily ON daily.published_date=calendar.day CROSS JOIN undated GROUP BY calendar.day ORDER BY calendar.day LIMIT 50)')


def tool_sql(binding, snapshot, chat, name, args):
    checked_scope(binding,snapshot,chat)
    args = resolved_args_v2(snapshot,name,args)
    target = relation(binding,snapshot,'observations' if name in ('history','details') else 'document_current')
    base = 'SELECT d.* FROM '+target+' d WHERE '+' AND '.join(document_conditions(binding,snapshot,chat,name,args))
    if name=='trend':
        return sonda_trend_sql(base,args)
    if name=='summary':
        metric = METRICS[args['metric']]
        aggregate = 'COUNT(*)' if metric is None else 'SUM('+metric+')'
        reported = 'COUNT(*)' if metric is None else 'COUNT('+metric+')'
        platform=literal(args['platform'])+' AS platform' if args.get('platform') else 'platform'
        columns = platform+','+aggregate+' AS value,COUNT(*) AS post_count,'+reported+' AS reported_count,COUNT(CASE WHEN published_date IS NULL THEN 1 END) AS undated_count'
        grouping='' if args.get('platform') else ' GROUP BY platform'
        return 'SELECT to_json(struct(*)) AS payload FROM (SELECT '+columns+' FROM ('+base+')'+grouping+' ORDER BY platform LIMIT 50)'
    if name=='comments':
        columns = ('observation_key AS comment_key,observation_key AS post_key,document_key,parent_document_key,platform,provider_id AS comment_id,'
            'substring(text,1,2400) AS text,author,url,likes AS like_count,published_at AS commented_at,observed_at,capture_scope,document_type')
    else:
        columns = ('observation_key AS post_key,document_key,parent_document_key,platform,provider_id AS post_id,content_sha256 AS version_hash,published_at,observed_at,'
            'substring(text,1,2400) AS text,url,author,likes AS like_count,comments AS comment_count,shares AS share_count,views AS view_count,document_type,capture_scope')
    if name=='ranking':
        metric = METRICS[args['metric']]
        ranked = 'SELECT '+columns+','+metric+' AS value,COUNT(*) OVER() AS scope_posts,COUNT('+metric+') OVER() AS reported_count FROM ('+base+')'
        return 'SELECT to_json(struct(*)) AS payload FROM (SELECT * FROM ('+ranked+') WHERE value IS NOT NULL ORDER BY value DESC NULLS LAST,post_key LIMIT '+str(args.get('limit',10))+')'
    return 'SELECT to_json(struct(*)) AS payload FROM (SELECT '+columns+' FROM ('+base+') ORDER BY observed_at DESC,'+('comment_key' if name=='comments' else 'post_key')+' LIMIT '+str(args.get('limit',50))+')'


def query_evidence(snapshot, chat, name, args, rows):
    if not isinstance(chat,BoundChat) or any(snapshot.get(key)!=getattr(chat,key) for key in ('registry_id','generation_id','config_revision')):
        raise ValueError('Evidence context belongs to another publication scope')
    if not isinstance(rows,list) or len(rows)>50 or any(not isinstance(row,dict) for row in rows):
        raise RuntimeError('Invalid Gold rows; this is not an empty result')
    args = resolved_args_v2(snapshot,name,args)
    return {'tool':name,'args':args,'rows':rows,'sample_limit':args.get('limit',10 if name=='ranking' else 50),
        'period_basis':'observation' if name=='history' else 'comment' if name=='comments' else 'publication',
        'publication_id':snapshot['publication_id'],'document_type':args['document_type'],
        'search_context':chat.search_context(),'capture_scopes':sorted({row['capture_scope'] for row in rows if row.get('capture_scope')})}


def public_response(snapshot, chat, answer, queries, presentation='auto', post_keys=()):
    if not isinstance(chat,BoundChat) or any(snapshot.get(key)!=getattr(chat,key) for key in ('registry_id','generation_id','config_revision')):
        raise ValueError('Answer context belongs to another publication scope')
    if not isinstance(answer,str) or not 1<=len(answer.strip())<=1800 or not isinstance(post_keys,(list,tuple)) or len(post_keys)>8:
        raise ValueError('Invalid bounded answer')
    if any(query.get('publication_id')!=snapshot['publication_id'] or query.get('search_context')!=chat.search_context() for query in queries):
        raise ValueError('Answer evidence belongs to another publication/search scope')
    refs = {(row.get('comment_key') or row['post_key']):row for query in queries for row in query['rows'] if row.get('post_key')}
    if any(key not in refs for key in post_keys):
        raise ValueError('Answer referenced unqueried evidence')
    sources=[]
    for key in dict.fromkeys(post_keys):
        row=refs[key]
        public_reference(row['url'])
        sources.append({field:row.get(field) for field in ('post_key','comment_key','document_key','parent_document_key','platform','url','published_at','commented_at','observed_at','document_type','capture_scope')})
    publication={key:value for key,value in snapshot.items() if key!='versions'}
    result={'answer':answer,'sources':sources,'publication':publication,'search_context':chat.search_context(),
        'query_context':[{'tool':query['tool'],'args':query['args']} for query in queries]}
    try:
        result['visualization']=build_visualization(presentation,queries)
        if result['visualization'] and queries:
            document_type=queries[-1]['args']['document_type']
            if document_type!='post':
                label={'comment':'Comentarios capturados','page':'Páginas web','indicator':'Documentos de indicadores'}[document_type]
                visual=result['visualization']
                visual['caption']=visual['caption'].replace('publicaciones','comentarios capturados' if document_type=='comment' else 'documentos')
                visual['caption']=(visual['caption']+' Tipo de documento: '+label+'.')[:700]
                if queries[-1]['args'].get('metric','posts')=='posts':
                    visual['title']=label+(' por fecha' if queries[-1]['tool']=='trend' else '')
                    if queries[-1]['tool']=='summary' and len(visual['columns'])==2:
                        visual['columns'][1]=label
    except (RuntimeError,ValueError,TypeError,OverflowError):
        result.update(visualization=None,visualization_error='La visualización no puede representar fielmente estos valores; consulta la respuesta y las fuentes.')
    return result
