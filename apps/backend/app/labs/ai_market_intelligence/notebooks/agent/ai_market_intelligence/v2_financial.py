"""Quoted financial amounts become series through explicit decimal and scale conversion."""
from decimal import Decimal
import math
import re

from .v2_contracts import V2FinancialFact


def v2_amount_decoration(amount,currency,percent):
    number=r'([+\-\u2212]?\d+(?:[.,\u00a0\u202f]\d+)*)'
    pattern=r'[$€£¥₹₩₽]\s*'+number if currency else number+r'\s*%' if percent else None
    match=re.fullmatch(pattern,amount) if pattern is not None else None
    return (match[1],match.start(1),len(amount)-match.end(1)) if match else (amount,0,0)


def v2_amount_span(text,amount,*,currency=False,percent=False,at=None):
    if not isinstance(amount,str) or not amount:
        raise ValueError('Financial amount must be a literal numeric token')
    token,before,after=v2_amount_decoration(amount,currency,percent)
    for match in re.finditer(r'(?<![\w.,+\-\u2212\u2010-\u2015])[+\-\u2212]?\d+(?:[.,\u00a0\u202f]\d+)*(?!\w)',text):
        prefix=text[:match.start()].rstrip()
        prefix=re.sub(r'(?:[A-Z]{1,3}\s*)?[$€£¥₹₩₽]$','',prefix).rstrip()
        signed_prefix=re.search(r'[-+\u2212\u2010-\u2015]$',prefix)
        accounting=prefix.endswith('(') and re.match(r'\s*%?\s*\)',text[match.end():]) is not None
        literal=text[max(0,match.start()-before):match.end()+after]==amount
        if match[0]==token and literal and not signed_prefix and not accounting and (at is None or match.start()==at):
            return match.span()
    raise ValueError('Financial amount must appear as a complete numeric token in the cited excerpt')


def v2_reported_amount_span(text,amount):
    start,end=v2_reported_paragraph_span(text,amount)
    offset=re.search(r'(?<!\w)'+re.escape(amount)+r'(?!\w)',text).start()
    for token in re.finditer(r'(?<!\w)[+\-\u2212]?\d+(?:[.,\u00a0\u202f]\d+)*(?!\w)',amount):
        v2_amount_span(text,token[0],at=offset+token.start())
    return start,end


def v2_reported_paragraph_span(text,amount):
    if not isinstance(amount,str) or not amount:
        raise ValueError('Reported financial text requires a literal numeric anchor')
    match=re.search(r'(?<!\w)'+re.escape(amount)+r'(?!\w)',text)
    if match is None:
        raise ValueError('Reported financial text must appear literally and completely in its document')
    tokens=list(re.finditer(r'(?<!\w)[+\-\u2212]?\d+(?:[.,\u00a0\u202f]\d+)*(?!\w)',amount))
    if not tokens:
        raise ValueError('Reported financial text requires a complete numeric token')
    boundary=text.rfind('\n\n',0,match.start())
    start=boundary+2 if boundary>=0 else 0
    end=text.find('\n\n',match.end())
    end=len(text) if end<0 else end
    paragraph=text[start:end]
    start+=len(paragraph)-len(paragraph.lstrip())
    end=start+len(paragraph.strip())
    if end-start>1000:
        raise ValueError('Reported financial paragraph exceeds its complete-text bound')
    return start,end


def v2_bind_financial_facts(result,evidence):
    result=dict(result)
    admitted={row['evidence_id']:row for row in evidence if row['source']=='financial_document'}
    facts=[]
    for value in result.get('financial_facts',[]):
        if not isinstance(value,dict) or not isinstance(value.get('evidence_id'),str):
            raise ValueError('Financial extraction requires a document evidence identity')
        row=admitted.get(value['evidence_id'])
        if row is None:
            raise ValueError('Financial extraction escaped the included document pages')
        text=row['text']
        start,end=(v2_reported_amount_span(text,value.get('amount_text')) if value.get('value_kind')=='reported_text'
                   else v2_amount_span(text,value.get('amount_text'),currency=value.get('unit')=='currency',percent=value.get('unit')=='percent'))
        # Keep one contiguous source excerpt; the reviewer also receives the original page.
        left=start if value.get('value_kind')=='reported_text' else max(text.rfind('\n',0,start)+1,start-100)
        newline=text.find('\n',end)
        right=end if value.get('value_kind')=='reported_text' else min(len(text) if newline<0 else newline,end+100)
        facts.append({**value,'amount_text':text[start:end],'quoted_text':text[left:right]})
    result['financial_facts']=facts
    return result


def v2_financial_rows(facts,evidence):
    rows=[]
    for value in facts:
        fact=V2FinancialFact.model_validate(value).model_dump(mode='json')
        row=evidence.get(fact['evidence_id'])
        if row is None or row['source']!='financial_document' or fact['quoted_text'] not in row['text']:
            raise ValueError('Financial extraction requires an exact excerpt from its document')
        amount=fact['amount_text']
        if fact['value_kind']=='reported_text':
            start,end=v2_reported_paragraph_span(row['text'],amount)
            if row['text'][start:end]!=amount:
                raise ValueError('Reported financial text must retain the complete source paragraph')
            continue
        v2_amount_span(fact['quoted_text'],amount)
        separator=fact['decimal_separator']
        thousands=',' if separator=='.' else '.'
        pattern=r'[-\u2212]?(?:\d+|\d{1,3}(?:'+re.escape(thousands)+r'\d{3})+)(?:'+re.escape(separator)+r'\d+)?'
        if not re.fullmatch(pattern,amount):
            raise ValueError('Financial amount has an ambiguous or unsupported numeric format')
        number=float(Decimal(amount.replace('\u2212','-').replace(thousands,'').replace(separator,'.'))*fact['scale'])
        if not math.isfinite(number):
            raise ValueError('Financial amount must remain finite after scaling')
        unit={'currency':fact['currency'] or 'unidades monetarias', 'count':'unidades',
              'percent':'%', 'ratio':'ratio'}[fact['unit']]
        rows.append({**row,'financial':{'metric':'value','indicator':fact['metric'],
            'entity':fact['entity']+' · '+fact['basis'],'unit':unit,'currency':fact['currency'],
            'points':[{'period':fact['period'],'value':number}]}})
    return rows


def v2_numeric_change(current,previous):
    if current is None or previous is None:
        return None,None
    now,before=Decimal(str(current)),Decimal(str(previous))
    absolute=float(now-before)
    percent=float((now/before-1)*100) if before>0 else None
    return (absolute if math.isfinite(absolute) else None,
            round(percent,6) if percent is not None and math.isfinite(percent) else None)


def v2_period_changes(points):
    rows=[dict(point) for point in points]
    if len({len(point['period']) for point in rows})>1:
        return rows
    previous=None
    for point in rows:
        absolute,percent=v2_numeric_change(point['value'],previous['value']) if previous is not None else (None,None)
        references=sorted(set(point['evidence_ids']+previous['evidence_ids'])) if previous is not None else []
        point.update(previous_period=previous['period'] if previous else None,
                     change_absolute=absolute,change_percent=percent,change_evidence_ids=references)
        previous=point
    return rows
