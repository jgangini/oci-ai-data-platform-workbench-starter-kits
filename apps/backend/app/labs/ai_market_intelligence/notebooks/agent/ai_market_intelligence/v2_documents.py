"""Public PDF retrieval with pinned DNS/TLS destinations and bounded text extraction."""
import asyncio
from hashlib import sha256
from io import BytesIO
from ipaddress import ip_address
import socket
import re
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from .runtime_core import public_url
from .v2_passages import V2_DOCUMENT_TEXT_BYTES,v2_text_prefix


V2_PDF_BYTES=12*1024*1024
V2_PDF_PAGES=50
V2_PDF_STREAM_BYTES=8*1024*1024


def v2_document_links(evidence,excluded):
    candidates={}
    for row in evidence:
        if row['content_basis']!='page_text' or not row.get('receipt_id'):
            continue
        for match in re.finditer(r'\[[^\]\r\n]{0,300}\]\(([^\s<>)]{1,1500})\)',row['text']):
            try:
                url=public_url(urljoin(row['url'],match[1]))
            except ValueError:
                continue
            if (not urlsplit(url).path.lower().endswith('.pdf') or len(url)>500 or url in excluded or url in candidates):
                continue
            candidates[url]={'parent_evidence_id':row['evidence_id'],'parent_url':row['url'],
                'parent_receipt_id':row['receipt_id'],'parent_text_sha256':sha256(row['text'].encode('utf-8')).hexdigest(),
                'observed_link':match[0],'resolved_url':url}
            if len(candidates)==10:
                return candidates
    return candidates


async def v2_pdf_addresses(host):
    records=await asyncio.get_running_loop().getaddrinfo(host,443,type=socket.SOCK_STREAM)
    return [row[4][0] for row in records]


def v2_pdf_target(url,addresses):
    logical=public_url(url)
    parts=urlsplit(logical)
    if parts.scheme!='https' or parts.port not in (None,443):
        raise ValueError('PDF retrieval requires public HTTPS on port 443')
    ips=[ip_address(value) for value in addresses]
    if not ips or any(not value.is_global or value.is_multicast or
            value.version==6 and value.ipv4_mapped and not value.ipv4_mapped.is_global for value in ips):
        raise ValueError('PDF DNS resolution includes a nonpublic destination')
    address=str(ips[0])
    netloc='['+address+']' if ips[0].version==6 else address
    return logical,urlunsplit(('https',netloc,parts.path or '/',parts.query,'')),parts.hostname


async def v2_fetch_pdf(url,*,resolver=v2_pdf_addresses,transport_factory=None):
    original=public_url(url)
    logical=original
    async with asyncio.timeout(60):
        for hop in range(4):
            host=urlsplit(public_url(logical)).hostname
            logical,pinned,host=v2_pdf_target(logical,await resolver(host))
            transport=(transport_factory() if transport_factory else httpx.AsyncHTTPTransport(
                verify=True,trust_env=False,http2=False,limits=httpx.Limits(max_keepalive_connections=0)))
            async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,
                                        timeout=httpx.Timeout(20),headers={'Accept-Encoding':'identity'}) as client:
                async with client.stream('GET',pinned,headers={'Host':host,'Accept':'application/pdf'},
                                         extensions={'sni_hostname':host}) as response:
                    if response.status_code in {301,302,303,307,308}:
                        if hop==3 or not response.headers.get('location'):
                            raise ValueError('PDF redirect limit or missing destination')
                        logical=public_url(urljoin(logical,response.headers['location']))
                        continue
                    response.raise_for_status()
                    if response.status_code!=200 or response.headers.get('content-encoding','identity').lower()!='identity':
                        raise ValueError('PDF retrieval requires an unencoded complete response')
                    length=response.headers.get('content-length')
                    if length is not None and (not length.isdigit() or int(length)>V2_PDF_BYTES):
                        raise ValueError('PDF exceeds the download limit')
                    data=bytearray()
                    async for chunk in response.aiter_raw():
                        if len(data)+len(chunk)>V2_PDF_BYTES:
                            raise ValueError('PDF exceeds the download limit')
                        data.extend(chunk)
            result=await asyncio.to_thread(v2_extract_pdf,bytes(data),logical)
            return {**result,'source_url':original}
    raise ValueError('PDF retrieval did not complete')


def v2_extract_pdf(data,url):
    import pypdf
    if not isinstance(data,bytes) or not data.startswith(b'%PDF-') or len(data)>V2_PDF_BYTES:
        raise ValueError('A bounded PDF document is required')
    config=pypdf.Configuration(maximum_declared_stream_length=V2_PDF_STREAM_BYTES,
        zlib_maximum_output_length=V2_PDF_STREAM_BYTES,lzw_maximum_output_length=V2_PDF_STREAM_BYTES,
        run_length_maximum_output_length=V2_PDF_STREAM_BYTES,brotli_maximum_output_length=V2_PDF_STREAM_BYTES,
        array_based_stream_maximum_output_length=V2_PDF_STREAM_BYTES,stream_filters_maximum_length=8,
        stream_decoding_work_maximum_length=4*V2_PDF_STREAM_BYTES,page_tree_maximum_entries=1000,
        page_tree_maximum_depth=32,jbig2dec_binary=None)
    pages=[]
    try:
        with pypdf.apply_configuration(config):
            reader=pypdf.PdfReader(BytesIO(data),strict=True)
            if reader.is_encrypted and reader.decrypt('')==pypdf.PasswordType.NOT_DECRYPTED:
                raise ValueError('Encrypted PDFs requiring a password are not supported')
            if not 1<=len(reader.pages)<=1000:
                raise ValueError('Unbounded PDFs are not supported')
            count=len(reader.pages)
            remaining=V2_DOCUMENT_TEXT_BYTES
            for number,page in enumerate(reader.pages[:V2_PDF_PAGES],1):
                text=page.extract_text() or ''
                retained=v2_text_prefix(text,remaining)
                remaining-=len(retained.encode('utf-8'))
                pages.append({'page':number,'text':retained,'truncated':len(retained)<len(text)})
    except pypdf.errors.PyPdfError:
        raise ValueError('The PDF could not be parsed within the extraction contract') from None
    limits=['Extracción de la capa textual del PDF; no se verificaron imágenes, OCR ni la integridad semántica de tablas complejas.']
    if count>len(pages) or any(row['truncated'] for row in pages):
        limits.append('Documento leído parcialmente por los límites de páginas o texto.')
    if any(not row['text'].strip() for row in pages):
        limits.append('Hay páginas sin texto extraíble; no se realizó OCR.')
    return {'url':public_url(url),'sha256':sha256(data).hexdigest(),'byte_count':len(data),
            'page_count':count,'pages':pages,'limitations':limits}
