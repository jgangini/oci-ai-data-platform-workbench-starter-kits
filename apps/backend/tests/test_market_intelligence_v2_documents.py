"""PDF authority, redirect and extraction boundaries without external requests."""
import asyncio
from hashlib import sha256
from io import BytesIO

import httpx
import pypdf
import pytest
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

from app.labs.ai_market_intelligence.notebooks.agent.ai_market_intelligence import v2_documents as documents


def pdf_bytes(text='Revenue 2025: 120 COP'):
    writer=pypdf.PdfWriter()
    page=writer.add_blank_page(width=300,height=300)
    if text is not None:
        font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                               NameObject('/BaseFont'):NameObject('/Helvetica')})
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        stream=DecodedStreamObject()
        stream.set_data(('BT /F1 12 Tf 10 100 Td ('+text+') Tj ET').encode())
        page[NameObject('/Contents')]=stream
    output=BytesIO()
    writer.write(output)
    return output.getvalue()


async def public(host):
    return ['93.184.216.34']


def test_observed_document_links_require_complete_literal_public_pdf_destinations():
    from test_market_intelligence_v2 import v2_evidence
    row=v2_evidence()
    row.update(url='https://example.com/reports/',text='\n'.join([
        '[Informe](annual.pdf)','[Duplicado](https://example.com/reports/annual.pdf)',
        '[HTTP](http://example.com/unsafe.pdf)','[Privado](https://127.0.0.1/a.pdf)',
        '[Credencial](https://user:password@example.com/a.pdf)','[Puerto](https://example.com:444/a.pdf)',
        '[Truncado](https://example.com/incomplete.pdf','[Instrucción](javascript:alert(1))']))
    links=documents.v2_document_links([row],set())
    assert list(links)==['https://example.com/reports/annual.pdf']
    proof=next(iter(links.values()))
    assert proof['parent_evidence_id']==row['evidence_id'] and proof['parent_receipt_id']==row['receipt_id']
    assert proof['observed_link']=='[Informe](annual.pdf)' and proof['parent_text_sha256']==sha256(row['text'].encode('utf-8')).hexdigest()
    assert documents.v2_document_links([{**row,'content_basis':'pdf_text'}],set())=={}
    assert documents.v2_document_links([{**row,'receipt_id':None}],set())=={}
    assert documents.v2_document_links([row],set(links))=={}


def test_pdf_preserves_text_page_hash_and_explicit_empty_text_limit():
    data=pdf_bytes()
    result=documents.v2_extract_pdf(data,'https://example.com/report.pdf')
    assert result['sha256']==sha256(data).hexdigest() and result['page_count']==1
    assert 'Revenue 2025: 120 COP' in result['pages'][0]['text'] and result['pages'][0]['page']==1
    empty=documents.v2_extract_pdf(pdf_bytes(None),'https://example.com/scan.pdf')
    assert empty['pages'][0]['text']=='' and any('no se realizó OCR' in item for item in empty['limitations'])


@pytest.mark.parametrize('addresses',[['127.0.0.1'],['169.254.169.254'],['::1'],['224.0.0.1'],
    ['::ffff:127.0.0.1'],['93.184.216.34','10.0.0.1'],[]])
def test_pdf_rejects_every_nonpublic_or_mixed_resolution(addresses):
    with pytest.raises(ValueError,match='nonpublic'):
        documents.v2_pdf_target('https://example.com/report.pdf',addresses)


def test_pdf_pins_public_ip_preserves_tls_hostname_and_revalidates_redirect():
    seen=[]
    def handler(request):
        seen.append(request)
        if len(seen)==1:
            return httpx.Response(302,headers={'location':'https://other.example/report.pdf','set-cookie':'secret=1'})
        return httpx.Response(200,stream=httpx.ByteStream(pdf_bytes()))
    result=asyncio.run(documents.v2_fetch_pdf('https://example.com/report.pdf',resolver=public,
        transport_factory=lambda:httpx.MockTransport(handler)))
    assert result['source_url']=='https://example.com/report.pdf' and result['url']=='https://other.example/report.pdf'
    assert [str(request.url.host) for request in seen]==['93.184.216.34']*2
    assert [request.headers['host'] for request in seen]==['example.com','other.example']
    assert [request.extensions['sni_hostname'] for request in seen]==['example.com','other.example']
    assert all('cookie' not in r.headers and 'authorization' not in r.headers for r in seen)


def test_pdf_private_redirect_and_encoding_are_rejected_before_reading():
    calls=[]
    def redirect(request):
        calls.append(request)
        return httpx.Response(302,headers={'location':'https://internal.example/report.pdf'})
    async def resolve(host):
        return ['10.0.0.1'] if host=='internal.example' else await public(host)
    with pytest.raises(ValueError,match='nonpublic'):
        asyncio.run(documents.v2_fetch_pdf('https://example.com/report.pdf',resolver=resolve,
            transport_factory=lambda:httpx.MockTransport(redirect)))
    assert len(calls)==1
    def encoded(request):
        return httpx.Response(200,headers={'content-encoding':'gzip'},stream=httpx.ByteStream(b'bad'))
    with pytest.raises(ValueError,match='unencoded'):
        asyncio.run(documents.v2_fetch_pdf('https://example.com/report.pdf',resolver=public,
            transport_factory=lambda:httpx.MockTransport(encoded)))


def test_pdf_rejects_oversize_malformed_and_encrypted_documents(monkeypatch):
    with pytest.raises(ValueError,match='bounded PDF'):
        documents.v2_extract_pdf(b'<html>not a PDF</html>','https://example.com/report.pdf')
    with pytest.raises(ValueError,match='could not be parsed'):
        documents.v2_extract_pdf(b'%PDF-1.7\nmalformed document','https://example.com/report.pdf')
    writer=pypdf.PdfWriter()
    writer.add_blank_page(width=10,height=10)
    writer.encrypt('fixture')
    data=BytesIO()
    writer.write(data)
    with pytest.raises(ValueError,match='Encrypted'):
        documents.v2_extract_pdf(data.getvalue(),'https://example.com/report.pdf')
    monkeypatch.setattr(documents,'V2_PDF_BYTES',5)
    with pytest.raises(ValueError,match='download limit'):
        asyncio.run(documents.v2_fetch_pdf('https://example.com/report.pdf',resolver=public,
            transport_factory=lambda:httpx.MockTransport(lambda r:httpx.Response(200,stream=httpx.ByteStream(b'123456')))))


@pytest.mark.parametrize('algorithm',['RC4-128','AES-256'])
def test_public_pdf_with_empty_open_password_keeps_limits_and_source_hash(algorithm):
    writer=pypdf.PdfWriter(clone_from=BytesIO(pdf_bytes('Annual revenue 2025: 120 COP')))
    writer.encrypt('',owner_password='fixture-owner',algorithm=algorithm)
    output=BytesIO();writer.write(output);data=output.getvalue()
    assert pypdf.PdfReader(BytesIO(data)).is_encrypted
    result=documents.v2_extract_pdf(data,'https://example.com/public.pdf')
    assert result['sha256']==sha256(data).hexdigest() and result['byte_count']==len(data)
    assert result['page_count']==1 and 'Annual revenue 2025: 120 COP' in result['pages'][0]['text']
