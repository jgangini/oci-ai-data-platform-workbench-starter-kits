import { useEffect, useRef, useState } from 'react';
import { networkNames, prismaEndpoint, prismaError, refreshedPosts, safeMediaUrl, timestamp, type Post, type PostPage, type PostListing, type PrismaApi } from './prismaAdminState';
const locationDescriptions: Record<string, string> = {
  unresolved: 'Location not established', text_locality_anchor: 'Approximate locality inferred from text; not an exact address',
  text_locality_centroid: 'Approximate locality inferred from text; not an exact address',
};

function PostPreview({ post, onClose }: { post: Post; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const origin = document.activeElement;
    dialog.current?.showModal();
    return () => { dialog.current?.close(); if (origin instanceof HTMLElement && origin.isConnected) origin.focus(); };
  }, []);
  const original = post.mode === 'real' && post.url?.startsWith('https://') ? safeMediaUrl(post.url) : null;
  return <dialog ref={dialog} className="prisma-post-dialog" aria-labelledby="prisma-preview-title" onClose={onClose}
    onClick={event => { if (event.target === event.currentTarget) onClose(); }}>
    <div className="prisma-preview-heading"><div><p className="eyebrow">{networkNames[post.platform] || post.platform} · {post.mode === 'real' ? 'Real source' : 'Synthetic fixture'}</p>
      <h2 id="prisma-preview-title">Publication preview</h2></div><button type="button" className="secondary" onClick={onClose} autoFocus>Close</button></div>
    <p><strong>{post.display_name || 'Unknown author'}</strong>{post.username && <> · @{post.username.replace(/^@/, '')}</>}</p>
    <p className="prisma-post-message">{post.text}</p>
    <dl className="prisma-source-status"><div><dt>Published · Bogotá time</dt><dd>{timestamp(post.published_at)}</dd></div>
      <div><dt>Ingested</dt><dd>{timestamp(post.ingested_at)}</dd></div><div><dt>Processing</dt><dd>{post.processing_status.replaceAll('_', ' ')}</dd></div>
      <div><dt>Reported location</dt><dd>{[post.locality, post.city, post.country].filter(value => value && value !== 'Unknown').join(', ') || 'Unknown'}</dd></div>
      <div><dt>Location provenance</dt><dd>{locationDescriptions[post.location_method || 'unresolved'] || `Source method: ${post.location_method?.replaceAll('_', ' ')}`}</dd></div></dl>
    <p className="prisma-post-note">Location is reported context. An attachment does not confirm this report.</p>
    <div className="prisma-post-media">{post.attachments.map(attachment => {
      const url = safeMediaUrl(attachment.url);
      if (!url) return <p key={attachment.id}>Attachment unavailable.</p>;
      if (attachment.mime_type.startsWith('image/')) return <img key={attachment.id} src={url} alt={attachment.alt_text || 'Attachment to this publication'} loading="lazy" />;
      if (attachment.mime_type.startsWith('video/')) return <video key={attachment.id} src={url} aria-label={attachment.alt_text || 'Attached video'} controls preload="metadata" />;
      return <p key={attachment.id}>Preview unavailable for this attachment format.</p>;
    })}</div>
    {original && <a href={original} target="_blank" rel="noopener noreferrer">Open original publication ↗</a>}
  </dialog>;
}

export function PrismaPosts({ api, platform, active, refreshKey }: { api: PrismaApi; platform: string; active: boolean; refreshKey: number }) {
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [{ page, changed }, setListing] = useState<PostListing>({ page: null, changed: false });
  const [error, setError] = useState('');
  const [expired, setExpired] = useState(false);
  const [busy, setBusy] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const [preview, setPreview] = useState<Post | null>(null);
  const cursor = cursors[cursors.length - 1];
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    let loading = false;
    async function load(replace: boolean) {
      if (loading) return;
      loading = true;
      if (replace) setBusy(true);
      try {
        const query = new URLSearchParams({ platform, limit: '20', ...(cursor ? { cursor } : {}) });
        const result = await api<PostPage>(`${prismaEndpoint}/posts?${query}`, { signal: controller.signal });
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
  }, [active, platform, cursor, refresh, refreshKey]);
  return <section className="prisma-posts" aria-labelledby={`prisma-posts-${platform}`}>
    <div className="prisma-sources-title"><h3 id={`prisma-posts-${platform}`}>Captured publications{page ? ` · ${page.total}` : ''}</h3>
      <button type="button" className="secondary" disabled={busy} onClick={() => setRefresh(value => value + 1)}>{busy ? 'Refreshing…' : 'Refresh posts'}</button></div>
    <p className="prisma-post-note">Publications from {networkNames[platform]}. Captured does not mean ingested or verified. Times are shown in Bogotá time.</p>
    {changed && <p role="status">New results are available. Refresh posts to update this page.</p>}
    {error && <p className="prisma-error" role="alert">{error}{expired && <> <button type="button" className="secondary" onClick={() => { setCursors([null]); setRefresh(value => value + 1); }}>Reload latest</button></>}</p>}
    <div className="prisma-post-table" aria-busy={busy}><table><thead><tr>{['Attachments', 'Username', 'Display name', 'Country / City', 'Message', 'Published at', 'Processing', 'Preview'].map(title => <th key={title} scope="col">{title}</th>)}</tr></thead>
      <tbody>{page?.items.map(post => <tr key={post.id}><td>{post.attachments.length || '—'}</td><td>{post.username || 'Unknown'}</td><td>{post.display_name || 'Unknown'}</td>
        <td>{[post.country, post.city, post.locality].filter(value => value && value !== 'Unknown').join(' / ') || 'Unknown'}</td><td><span className="prisma-post-excerpt">{post.text}</span></td>
        <td>{timestamp(post.published_at)}</td><td>{post.processing_status.replaceAll('_', ' ')}</td><td><button className="secondary prisma-preview-button" type="button" aria-label={`Preview publication by ${post.display_name || post.username || 'unknown author'}`} onClick={() => setPreview(post)}>
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z" fill="none" stroke="currentColor" strokeWidth="1.6" /><circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" strokeWidth="1.6" /></svg></button></td></tr>)}</tbody></table>
      {!page && busy && <p role="status">Loading publications…</p>}{page?.items.length === 0 && <p>No publications captured for this network yet.</p>}</div>
    <nav className="prisma-pagination" aria-label={`${networkNames[platform]} publication pages`}><button type="button" className="secondary" disabled={busy || cursors.length === 1} onClick={() => setCursors(value => value.slice(0, -1))}>Previous</button>
      <span>Page {cursors.length}</span><button type="button" className="secondary" disabled={busy || expired || !page?.next_cursor} onClick={() => { if (page?.next_cursor) setCursors(value => [...value, page.next_cursor]); }}>Next</button></nav>
    {preview && <PostPreview post={preview} onClose={() => setPreview(null)} />}
  </section>;
}
