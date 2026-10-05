import { LoadingIndicator } from './LoadingIndicator';
import { useEffect, useRef, useState, type ReactNode } from 'react';
import { networkNames, postStatus, prismaEndpoint, prismaError, refreshedPosts, safeMediaUrl, timestamp, type Post, type PostPage, type PostListing, type PrismaApi } from './prismaAdminState';

function highlightMatch(text: string, query: string) {
  if (!query) return text;
  const pattern = new RegExp(`(${query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})`, 'giu');
  return text.split(pattern).map((part, index) => index % 2 ? <mark key={index}>{part}</mark> : part);
}

function PostAttachments({ post, onPreview }: { post: Post; onPreview: () => void }) {
  const attachments = post.attachments.flatMap(attachment => {
    const url = safeMediaUrl(attachment.url);
    return url && /^(image|video)\//.test(attachment.mime_type) ? [{ ...attachment, url }] : [];
  });
  return <div className="prisma-attachment-thumbnails">{attachments.slice(0, 3).map((attachment, index) => <button key={attachment.id} className="prisma-attachment-thumbnail" type="button" onClick={onPreview}
    aria-label={`Preview ${attachment.mime_type.startsWith('video/') ? 'video' : 'image'} ${index + 1} by ${post.username || 'unknown author'}`}>
    {attachment.mime_type.startsWith('image/') ? <img src={attachment.url} alt="" loading="lazy" referrerPolicy="no-referrer" /> : <><video src={attachment.url} muted preload="metadata" aria-hidden="true" /><span className="prisma-video-play" aria-hidden="true">▶</span></>}
  </button>)}{attachments.length > 3 && <span>+{attachments.length - 3}</span>}{!attachments.length && '—'}</div>;
}

function PostPreview({ post, onClose, timeZone }: { post: Post; onClose: () => void; timeZone: string }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const origin = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (origin instanceof HTMLElement && origin.isConnected) origin.focus(); };
  }, []);
  const original = post.mode === 'real' && post.url?.startsWith('https://') ? safeMediaUrl(post.url) : null;
  return <dialog ref={dialog} className="prisma-post-dialog" aria-labelledby="prisma-preview-title" onClose={onClose}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="prisma-preview-heading"><p id="prisma-preview-title" className="eyebrow">{networkNames[post.platform] || post.platform}</p>
      <button type="button" className="table-action table-edit" aria-label="Close preview" title="Close" onClick={onClose} autoFocus>
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m6 6 12 12M6 18 18 6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" /></svg></button></div>
    <p><strong>{post.display_name || 'Unknown author'}</strong>{post.username && <> · @{post.username.replace(/^@/, '')}</>}</p>
    {post.attachments.length > 0 && <div className="prisma-post-media">{post.attachments.map(attachment => {
      const url = safeMediaUrl(attachment.url);
      if (!url) return <p key={attachment.id}>Attachment unavailable.</p>;
      if (attachment.mime_type.startsWith('image/')) return <img key={attachment.id} src={url} alt={attachment.alt_text || 'Attachment to this publication'} loading="lazy" />;
      if (attachment.mime_type.startsWith('video/')) return <video key={attachment.id} src={url} aria-label={attachment.alt_text || 'Attached video'} controls preload="metadata" />;
      return <p key={attachment.id}>Preview unavailable for this attachment format.</p>;
    })}</div>}
    {post.attachments.some(attachment => attachment.origin === 'ai_generated') && <p className="eyebrow">Synthetic · AI-generated image</p>}
    <p className="prisma-post-message">{post.text}</p>
    <dl className="prisma-source-status"><div><dt>Published · {timeZone}</dt><dd>{timestamp(post.published_at, timeZone)}</dd></div>
      <div><dt>Captured</dt><dd>{timestamp(post.captured_at, timeZone)}</dd></div><div><dt>Ingested</dt><dd>{timestamp(post.ingested_at, timeZone)}</dd></div><div><dt>Status</dt><dd>{postStatus(post.processing_status)}</dd></div>
      <div><dt>Reported location</dt><dd>{[post.locality, post.city, post.country].filter(value => value && value !== 'Unknown').join(', ') || 'Unknown'}</dd></div></dl>
    {original && <a href={original} target="_blank" rel="noopener noreferrer">Open original publication ↗</a>}
  </dialog>;
}

export function PrismaPosts({ api, refreshKey, searchIcon, refreshIcon, timeZone = 'America/Bogota' }: { api: PrismaApi; refreshKey: number; searchIcon: ReactNode; refreshIcon: ReactNode; timeZone?: string }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [pageSize, setPageSize] = useState(20);
  const [search, setSearch] = useState('');
  const [query, setQuery] = useState('');
  const [platform, setPlatform] = useState('');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [{ page }, setListing] = useState<PostListing>({ page: null, changed: false });
  const [error, setError] = useState('');
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [preview, setPreview] = useState<Post | null>(null);
  const cursor = cursors[cursors.length - 1];
  const searching = busy || search.trim() !== query;
  useEffect(() => {
    if (search.trim() === query) return;
    const timer = window.setTimeout(() => {
      setQuery(search.trim()); setCursors([null]); setListing({ page: null, changed: false }); setError(''); setExpired(false);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [search, query]);
  useEffect(() => {
    const controller = new AbortController();
    let loading = false;
    async function load(replace: boolean) {
      if (loading) return;
      loading = true;
      if (replace) setBusy(true);
      try {
        const params = new URLSearchParams({ limit: String(pageSize), sort: 'published_at', order,
          ...(platform ? { platform } : {}), ...(cursor ? { cursor } : {}), ...(query ? { q: query } : {}) });
        const result = await api<PostPage>(`${prismaEndpoint}/posts?${params}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setError(''); setExpired(false);
        setListing(previous => refreshedPosts(previous, result, replace));
      } catch (reason) {
        if (!controller.signal.aborted) {
          setExpired(!!reason && typeof reason === 'object' && 'status' in reason && reason.status === 409);
          setError(prismaError(reason));
        }
      } finally { loading = false; if (!controller.signal.aborted) setBusy(false); }
    }
    void load(true);
    const timer = window.setInterval(() => { void load(false); }, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [pageSize, cursor, query, platform, order, refresh, refreshKey]);
  const first = page?.items.length ? (cursors.length - 1) * pageSize + 1 : 0;
  const last = page?.items.length ? first + page.items.length - 1 : 0;
  return <section className="prisma-posts" aria-label="Captured publications">
    {error && <p className="prisma-error" role="alert">{error}{expired && <> <button type="button" className="secondary" onClick={() => { setCursors([null]); setListing({ page: null, changed: false }); setRefresh(value => value + 1); }}>Reload latest</button></>}</p>}
    <div className="admin-panel">
      <div className="admin-toolbar">
        <form className="search" role="search" aria-label="Publications" onSubmit={event => {
          event.preventDefault(); setQuery(search.trim()); setCursors([null]); setListing({ page: null, changed: false });
          setError(''); setExpired(false); setRefresh(value => value + 1);
        }}>
          <label><span className="sr-only">Search publications</span><input type="search" maxLength={200} value={search} onChange={event => setSearch(event.target.value)} placeholder="Search by message, author, network or country" /></label>
          <button className="search-submit" type="submit" aria-label="Search publications" title="Search publications">{searchIcon}</button>
        </form>
        <label className="prisma-network-filter"><span className="sr-only">Social network</span><select value={platform} onChange={event => {
          setPlatform(event.target.value); setQuery(search.trim()); setCursors([null]); setListing({ page: null, changed: false }); setError(''); setExpired(false);
        }}><option value="">All networks</option>{Object.entries(networkNames).map(([value, name]) => <option key={value} value={value}>{name}</option>)}</select></label>
        <div className="toolbar-actions"><button className="toolbar-icon" type="button" disabled={busy} onClick={() => setRefresh(value => value + 1)} aria-label="Refresh posts" title="Refresh posts">{refreshIcon}</button></div>
      </div>
    <div className="table-wrap prisma-post-table" aria-busy={searching}><table><thead><tr>
      <th scope="col">Social network</th><th scope="col">Multimedia</th><th scope="col">Username</th><th scope="col">Region</th>
      <th scope="col" aria-sort={order === 'desc' ? 'descending' : 'ascending'}><button type="button" className="prisma-sort" aria-label={`Sort by creation date, ${order === 'desc' ? 'oldest' : 'newest'} first`} onClick={() => {
        setOrder(value => value === 'desc' ? 'asc' : 'desc'); setQuery(search.trim()); setCursors([null]); setListing({ page: null, changed: false }); setError(''); setExpired(false);
      }}>Created at<span aria-hidden="true">{order === 'desc' ? '↓' : '↑'}</span></button></th><th scope="col">Status</th><th scope="col">Preview</th>
      </tr></thead>
      <tbody>{page?.items.map((post, index) => <tr key={post.id} className={query ? 'prisma-search-match' : undefined}><td><span className="prisma-post-network"><span className="row-index">{first + index}</span>{networkNames[post.platform] && <img src={`/brand-icons/${post.platform}.svg`} width="18" height="18" alt="" />}<span>{highlightMatch(networkNames[post.platform] || post.platform, query)}</span></span></td>
        <td><PostAttachments post={post} onPreview={() => setPreview(post)} /></td><td>{highlightMatch(post.username || 'Unknown', query)}</td>
        <td>{highlightMatch(post.country || 'Unknown', query)}</td>
        <td className="prisma-post-created"><time dateTime={post.published_at || undefined}>{timestamp(post.published_at, timeZone)}</time></td><td><span className={`badge ${post.processing_status === 'processed' ? 'active' : 'pending'}`}>{postStatus(post.processing_status)}</span></td><td className="prisma-post-preview-cell"><button className="table-action table-edit prisma-preview-button" type="button" aria-label={`Preview publication by ${post.username || 'unknown author'}`} onClick={() => setPreview(post)}>
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" fill="none" stroke="currentColor" strokeWidth="1.6" /><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg></button></td></tr>)}</tbody></table>
      {!page && busy && <LoadingIndicator label="Loading publications…" />}{page?.items.length === 0 && <p className="empty" role="status">{query || platform ? 'No publications match these filters.' : 'No publications captured yet.'}</p>}</div>
    </div>
    <nav className="prisma-pagination" aria-label="Publication pages"><label>Rows per page<select value={pageSize} disabled={searching} onChange={event => { setPageSize(Number(event.target.value)); setCursors([null]); setListing({ page: null, changed: false }); }}>{[10, 20, 50, 100].map(size => <option key={size} value={size}>{size}</option>)}</select></label>
      <span aria-live="polite">{first}–{last} of {page?.total ?? 0}</span><button type="button" className="secondary" disabled={searching || cursors.length === 1} onClick={() => { setCursors(value => value.slice(0, -1)); setListing({ page: null, changed: false }); }}>Previous</button>
      <span>Page {cursors.length}</span><button type="button" className="secondary" disabled={searching || expired || !page?.next_cursor} onClick={() => { if (page?.next_cursor) { setCursors(value => [...value, page.next_cursor]); setListing({ page: null, changed: false }); } }}>Next</button></nav>
    {preview && <PostPreview post={preview} timeZone={timeZone} onClose={() => setPreview(null)} />}
  </section>;
}
