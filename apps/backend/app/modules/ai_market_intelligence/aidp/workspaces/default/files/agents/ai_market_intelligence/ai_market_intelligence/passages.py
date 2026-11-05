"""Select literal, located document passages without changing capture authority."""
from hashlib import sha256
import re


Market_DOCUMENT_TEXT_BYTES=600000
Market_PASSAGE_CHARACTERS=12000
Market_OUTLINE_CANDIDATES=120


def market_text_prefix(text,maximum_bytes):
    return text.encode('utf-8')[:maximum_bytes].decode('utf-8',errors='ignore')


def market_document_headings(text):
    pattern=r'(?m)^#{1,6}[^\S\n]+[^\n]+$'
    count=sum(1 for _ in re.finditer(pattern,text))
    selected={i*(count-1)//(Market_OUTLINE_CANDIDATES-1) for i in range(Market_OUTLINE_CANDIDATES)} if count>Market_OUTLINE_CANDIDATES else set(range(count))
    return {match.start():match[0] for i,match in enumerate(re.finditer(pattern,text)) if i in selected}


def market_passage_spans(text,left,right,complete_sections):
    if right-left<=Market_PASSAGE_CHARACTERS:
        return [(left,right)]
    if complete_sections:
        return []
    boundaries=[left,*[left+m.end() for m in re.finditer(r'\n[ \t]*\n',text[left:right])],right]
    spans=[]
    start=end=None
    for a,b in zip(boundaries,boundaries[1:]):
        if b-a>Market_PASSAGE_CHARACTERS or start is not None and b-start>Market_PASSAGE_CHARACTERS:
            if start is not None:
                spans.append((start,end))
            start=end=None
        if b>a and b-a<=Market_PASSAGE_CHARACTERS:
            start=a if start is None else start
            end=b
    return [*spans,*([(start,end)] if start is not None else [])]


def market_passage_candidates(pages,*,complete_sections=False):
    candidates=[]
    for page in pages:
        text=page['text']
        checksum=sha256(text.encode('utf-8')).hexdigest()
        headings=market_document_headings(text)
        boundaries=sorted({0,len(text),*headings})
        for left,right in zip(boundaries,boundaries[1:]):
            for start,end in market_passage_spans(text,left,right,complete_sections):
                title=headings.get(left,'')
                candidates.append({'candidate_id':'p'+str(len(candidates)),'page':page.get('page'),
                    'start':start,'end':end,'heading':market_text_prefix(title,100),
                    'preview':market_text_prefix(text[start:end],300),'text_sha256':checksum})
    if len(candidates)>Market_OUTLINE_CANDIDATES:
        # Spread a bounded outline over the whole admitted document, retaining both ends.
        candidates=[candidates[i*(len(candidates)-1)//(Market_OUTLINE_CANDIDATES-1)]
                    for i in range(Market_OUTLINE_CANDIDATES)]
    return candidates


def market_complete_html_passages(pages):
    if len(pages)!=1 or pages[0].get('page') is not None:
        return None
    original=pages[0];text=original['text']
    if len(text)>2*Market_PASSAGE_CHARACTERS or len(text.encode('utf-8'))>60000:
        return None
    spans=market_passage_spans(text,0,len(text),False)
    if len(spans)>2 or ''.join(text[start:end] for start,end in spans)!=text:
        return None
    checksum=sha256(text.encode('utf-8')).hexdigest()
    return [{**original,'text':text[start:end],
             'locator':f'Caracteres {start}–{end} del texto admitido; SHA-256 {checksum}.',
             'passage_id':'p'+str(index)} for index,(start,end) in enumerate(spans)]


async def market_project_passages(pages,selector,context,*,complete_html=False):
    if not pages:
        return [],False
    if (all(len(page['text'])<=Market_PASSAGE_CHARACTERS for page in pages)
            and sum(len(page['text'].encode('utf-8')) for page in pages)<=60000):
        return [dict(page) for page in pages],False
    if complete_html and context.get('source')!='financial_document' and not context.get('document_sha256'):
        complete=market_complete_html_passages(pages)
        if complete is not None:
            return complete,False
    candidates=market_passage_candidates(pages,complete_sections=context.get('source')=='financial_document')
    if not candidates:
        return [],True
    selected=await selector(context,candidates) if selector is not None else [candidates[0]['candidate_id']]
    indexed={row['candidate_id']:row for row in candidates}
    if (not isinstance(selected,list) or len(selected)>2 or any(not isinstance(i,str) or i not in indexed for i in selected)
            or len(set(selected))!=len(selected)):
        raise ValueError('Document selection escaped the enumerated captured passages')
    originals={page.get('page'):page for page in pages}
    result=[]
    for identity in selected:
        item=indexed[identity];original=originals[item['page']]
        start,end=item['start'],item['end']
        result.append({**original,'text':original['text'][start:end],
            'locator':f"Caracteres {start}–{end} del texto admitido; SHA-256 {item['text_sha256']}.",
            'passage_id':identity})
    return result,True
