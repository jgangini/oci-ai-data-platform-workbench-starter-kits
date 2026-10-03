import { FormEvent, useEffect, useState } from 'react';
import './prisma.css';

type Api = <T>(path: string, init?: RequestInit) => Promise<T>;
type Source = {
  platform: string; enabled: boolean; mode: 'simulation' | 'real'; query: string;
  interval_minutes: number; secret_ref: string; credential_configured: boolean;
  status: string; last_run_at?: string; next_due?: string; last_error?: string;
  last_received_count?: number | null;
};
type Simulation = { status: string; elapsed_seconds: number; duration_seconds: number };
type Configuration = { sources: Source[]; simulation: Simulation; runtime: string; capture_summary?: { status?: string; last_run_at?: string; landing_count?: number; last_landing_key?: string; last_error?: string }; pipeline?: { status?: string; last_run_at?: string; version?: string; stream?: { microbatches?: number; last_input_rows?: number } } };
const endpoint = '/api/admin/prisma';
const labels: Record<string, string> = { x: 'X', facebook: 'Facebook', instagram: 'Instagram', tiktok: 'TikTok' };
const timestamp = (value?: string) => value ? new Date(value).toLocaleString('en-GB', { timeZone: 'America/Bogota' }) : 'Not run yet';
function errorMessage(error: unknown) {
  if (error && typeof error === 'object' && 'status' in error && error.status === 401) window.location.assign('/admin/login');
  return error instanceof Error ? error.message : 'The operation could not be completed.';
}

function SourceCard({ source, api, simulationRunning }: { source: Source; api: Api; simulationRunning: boolean }) {
  const [draft, setDraft] = useState(source);
  const [saved, setSaved] = useState(source);
  const [token, setToken] = useState('');
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState('');
  const overdueSeconds = draft.enabled && draft.next_due ? Math.max(0, Math.floor((Date.now() - Date.parse(draft.next_due)) / 1000)) : 0;
  const [error, setError] = useState('');
  const dirty = ['enabled', 'mode', 'query', 'interval_minutes'].some((key) => draft[key as keyof Source] !== saved[key as keyof Source]) || !!token;
  useEffect(() => { setDraft(source); setSaved(source); }, [source]);
  async function action(kind: 'save' | 'test' | 'run') {
    setBusy(true); setMessage(''); setError('');
    try {
      if (kind === 'save') {
        const { enabled, mode, query, interval_minutes } = draft;
        const updated = await api<Source>(`${endpoint}/sources/${encodeURIComponent(source.platform)}`, {
          method: 'PUT', body: JSON.stringify({ enabled, mode, query, interval_minutes, ...(token ? { bearer_token: token } : {}) }),
        });
        setDraft(updated);
        setSaved(updated);
        setToken(''); setMessage('Configuration saved.');
      } else {
        const result = await api<{ status: string; message: string; source: Source }>(`${endpoint}/sources/${encodeURIComponent(source.platform)}/${kind}`, { method: 'POST' });
        setDraft(result.source); setSaved(result.source); setMessage(result.message);
      }
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <form className="prisma-source" onSubmit={(event: FormEvent) => { event.preventDefault(); void action('save'); }}>
    <div className="prisma-source-heading"><h2>{labels[source.platform] && <img className="prisma-platform-logo" src={`/brand-icons/${source.platform}.svg`} alt="" width="25" height="25" />}{labels[source.platform] || source.platform}</h2><span className={`prisma-mode ${draft.mode}`}>{draft.mode === 'real' ? 'REAL' : 'SIMULATED'}</span></div>
    <fieldset disabled={busy}>
      <label className="prisma-toggle"><input type="checkbox" checked={draft.enabled} onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })} /> Source enabled</label>
      <div className="prisma-fields"><label>Mode<select value={draft.mode} onChange={(event) => setDraft({ ...draft, mode: event.target.value as Source['mode'] })}><option value="simulation">SIMULATED</option><option value="real" disabled={source.platform !== 'x'}>REAL{source.platform !== 'x' ? ' · unavailable' : ''}</option></select></label>
        <label>Interval (minutes)<input type="number" min="1" max="1440" required value={draft.interval_minutes} onChange={(event) => setDraft({ ...draft, interval_minutes: Number(event.target.value) })} /></label></div>
      <label>Search query or supported criteria<textarea rows={2} maxLength={512} value={draft.query} onChange={(event) => setDraft({ ...draft, query: event.target.value })} placeholder="E.g. Bogotá inundación" /></label>
      <label>Credential reference<input value={draft.secret_ref || ''} readOnly title="Reference managed by the kit" /></label>
      {source.platform !== 'x' && <small>Simulation available. Real capture requires a validated connector for this platform.</small>}
      <label>Update token<input type="password" autoComplete="new-password" value={token} onChange={(event) => setToken(event.target.value)} placeholder={draft.credential_configured ? 'Configured · leave blank to keep current' : 'No credential configured'} /></label>
      <small>Tokens are submitted when you save and are never returned.</small>
      <div className="prisma-source-actions"><button type="submit">Save</button><button className="secondary" type="button" disabled={dirty} onClick={() => void action('test')}>Test</button><button className="secondary" type="button" disabled={dirty || !draft.enabled} onClick={() => void action('run')}>Run now</button></div>
      {dirty && <small>Save changes before testing or running the source.</small>}
    </fieldset>
    <dl className="prisma-source-status"><div><dt>Status</dt><dd>{draft.status}</dd></div><div><dt>Last run</dt><dd>{timestamp(draft.last_run_at)}</dd></div><div><dt>Next run</dt><dd>{draft.next_due ? timestamp(draft.next_due) : 'Not scheduled'}</dd></div></dl>
    <dl className="prisma-source-status"><div><dt>Records received · last successful capture</dt><dd>{draft.last_received_count ?? 'No successful capture'}</dd></div><div><dt>Capture delay</dt><dd>{draft.mode === 'simulation' && !simulationRunning ? 'Simulation stopped' : `${overdueSeconds} s`}</dd></div></dl>
    {draft.last_error && <p className="prisma-error">{draft.last_error}</p>}
    {message && <p role="status" className="prisma-success">{message}</p>}
    {error && <p role="alert" className="prisma-error">{error}</p>}
  </form>;
}

export function PrismaAdmin({ api }: { api: Api }) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  async function load(signal?: AbortSignal) {
    setError('');
    try { setConfig(await api<Configuration>(`${endpoint}/sources`, { signal })); }
    catch (reason) { if (!signal?.aborted) setError(errorMessage(reason)); }
  }
  useEffect(() => { const controller = new AbortController(); void load(controller.signal); return () => controller.abort(); }, []);
  async function simulation(action: 'start' | 'pause' | 'resume' | 'reset') {
    setBusy(true); setError('');
    try { await api(`${endpoint}/simulation`, { method: 'POST', body: JSON.stringify({ action }) }); await load(); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <section className="prisma-admin">
    <div className="prisma-heading"><div><p className="eyebrow">TERRITORIAL CONTROL · BOGOTÁ</p><h1>Sources and simulation</h1><p>Configure selected channels and review their evidence in the viewer.</p></div><a className="prisma-open" href="/prisma/">Open God’s Eye View ↗</a></div>
    {error && <p role="alert" className="prisma-error">{error}</p>}
    {!config && !error && <p role="status">Loading configuration…</p>}
    {config && <>
      <section className="prisma-simulation" aria-label="Simulation controls">
        <div><span className="prisma-mode simulation">SIMULATED</span><h2>Bogotá risk scenario</h2><p>Replay social posts, institutional reports and sensors to practise incident review.</p><p className="prisma-runtime">Status: <strong>{config.simulation.status}</strong> · {Math.floor(config.simulation.elapsed_seconds)} / {config.simulation.duration_seconds} s · {config.runtime === 'aidp' ? 'AIDP' : 'Local demonstration'}</p></div>
        <div className="prisma-simulation-actions"><button type="button" disabled={busy || config.simulation.status === 'running'} onClick={() => void simulation(config.simulation.status === 'paused' ? 'resume' : 'start')}>{config.simulation.status === 'paused' ? 'Resume' : 'Start scenario'}</button><button className="secondary" type="button" disabled={busy || config.simulation.status !== 'running'} onClick={() => void simulation('pause')}>Pause</button><button className="secondary" type="button" disabled={busy} onClick={() => void simulation('reset')}>Restart simulation</button></div>
      </section>
      <section className="prisma-simulation" aria-label="Data pipeline status"><div><h2>Capture → Landing → AIDP → Published snapshot</h2>
        <p>{config.runtime === 'aidp' ? 'Synthetic searches run on this VM. AIDP consumes Landing with a checkpointed stream.' : 'Local fixture pipeline. OCI streaming is verified separately on the deployed environment.'}</p>
        <dl className="prisma-source-status"><div><dt>Capture status</dt><dd>{config.capture_summary?.status || 'Not run yet'}</dd></div><div><dt>Landing objects</dt><dd>{config.capture_summary?.landing_count ?? 0}</dd></div><div><dt>Last capture</dt><dd>{timestamp(config.capture_summary?.last_run_at)}</dd></div></dl>
        <dl className="prisma-source-status"><div><dt>Processing status</dt><dd>{config.pipeline?.status || 'Not run yet'}</dd></div><div><dt>Microbatches</dt><dd>{config.pipeline?.stream?.microbatches ?? 'Not available'}</dd></div><div><dt>Published version</dt><dd>{config.pipeline?.version || 'Not published'}</dd></div></dl>
        {config.capture_summary?.last_error && <p role="alert">{config.capture_summary.last_error}</p>}
      </div></section>
      <div className="prisma-sources-title"><h2>Selected networks</h2><button className="secondary" type="button" disabled={busy} onClick={() => void load()}>Refresh status</button></div>
      <div className="prisma-source-grid">{config.sources.map((source) => <SourceCard key={source.platform} source={source} api={api} simulationRunning={config.simulation.status === 'running'} />)}</div>
      <p className="prisma-icon-credit">Logos: <a href="https://github.com/simple-icons/simple-icons/tree/d9ea58066506bc80da65d5516813636b22b58a06" target="_blank" rel="noreferrer">Simple Icons</a> · <a href="/brand-icons/LICENSE.md" target="_blank">CC0</a>. Trademarks belong to their respective owners.</p>
    </>}
  </section>;
}
