"""Pure datasets for presentation; missing or conflicting values stay explicit."""
from collections import Counter, defaultdict

from .runtime_core import digest
from .research_contracts import MarketArtifact, MarketEvidence
from .financial import market_period_changes


def market_artifact_data(artifacts):
    """Publication status belongs to the UI, not to the statistical observations."""
    return [{key:value for key,value in artifact.items() if key!='status'} for artifact in artifacts]


def artifacts(observations, *, status='final'):
    evidence=[MarketEvidence.model_validate(row).model_dump(mode='json') for row in observations]
    artifacts=[]
    if not evidence:
        return artifacts
    distinct={row['evidence_id']:row for row in evidence}
    counts=Counter(source for source,url in {(row['source'],row['url']) for row in distinct.values()})
    identities=list(distinct)
    if len(counts)>1:
        artifacts.append(MarketArtifact.model_validate({'artifact_id':digest('source-counts'),
            'kind':'bar','title':'Registros citados por fuente','x_label':'Fuente',
            'y_label':'Enlaces únicos','unit':'enlaces','currency':None,
            'rows':[{'source':source,'value':count} for source,count in sorted(counts.items())],
            'evidence_ids':identities,'caption':'Enlaces únicos de la evidencia citada; no mide el volumen total de conversación ni cuota de mercado.',
            'status':status}).model_dump(mode='json'))
    groups=defaultdict(list)
    for row in evidence:
        metrics=row['financial']
        if metrics:
            key=tuple(metrics[field] for field in ('metric','entity','indicator','unit','currency'))
            groups[key].append(row)
    for key,rows in groups.items():
        artifact=market_financial_artifact(key,rows,status)
        if artifact is not None:
            artifacts.append(artifact)
    return artifacts


def market_financial_artifact(key, rows, status):
    points={}
    conflict=False
    for row in rows:
        for point in row['financial']['points']:
            period=point['period']
            if period in points and points[period]['value']!=point['value']:
                conflict=True
            points.setdefault(period,{'period':period,'value':point['value'],'evidence_ids':[]})['evidence_ids'].append(row['evidence_id'])
    if conflict or len(points)>50 or not points:
        return None
    return MarketArtifact.model_validate({'artifact_id':digest(['financial',key]),
        'kind':'line' if len(points)>1 else 'table','title':key[1]+' · '+(key[2] or key[0]),
        'x_label':'Periodo','y_label':key[2] or key[0],'unit':key[3],'currency':key[4],
        'rows':market_period_changes([points[period] for period in sorted(points)]),'evidence_ids':[row['evidence_id'] for row in rows],
        'caption':'Variación respecto del periodo anterior mostrado: actual − anterior; % = (actual / anterior − 1) × 100, sólo con base positiva. No anualizada; sin imputar valores ausentes.',
        'status':status}).model_dump(mode='json')
