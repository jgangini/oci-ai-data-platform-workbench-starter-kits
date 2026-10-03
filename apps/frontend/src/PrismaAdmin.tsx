import { FormEvent, KeyboardEvent, useEffect, useRef, useState } from 'react';
import { PrismaPosts } from './PrismaPosts';
import { captureState, networkNames, prismaEndpoint as endpoint, prismaError, refreshedEditor, sourceDefaults,
  sourceDirty, timestamp, type PrismaApi, type Source, type SourceEditor } from './prismaAdminState';
import './prisma.css';

type Configuration = { sources: Source[]; runtime: string };
const networks = Object.keys(networkNames);

function SourceCard({ source, api, onUpdate }: { source: Source; api: PrismaApi; onUpdate: (source: Source) => void }) {
  const [editor, setEditor] = useState<SourceEditor>({ draft: source, saved: source, token: '' });
  const { draft, saved, token } = editor;
  const dirty = sourceDirty(editor);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const state = captureState(source);
  const overdueSeconds = source.capture_running && source.next_due ? Math.max(0, Math.floor((Date.now() - Date.parse(source.next_due)) / 1000)) : 0;
  useEffect(() => { setEditor(previous => refreshedEditor(previous, source)); }, [source]);
  useEffect(() => () => controller.current?.abort(), []);
  const updateDraft = (values: Partial<Source>) => setEditor(previous => ({ ...previous, draft: { ...previous.draft, ...values } }));
  async function action(kind: 'save' | 'test' | 'run' | 'pause') {
    if (controller.current) return;
    const request = new AbortController(); controller.current = request;
    setBusy(kind); setMessage(''); setError('');
    try {
      if (kind === 'save') {
        const { enabled, mode, query, interval_minutes, correlation_window_minutes, report_thresholds } = draft;
        if (!(report_thresholds.low > 0 && report_thresholds.low < report_thresholds.medium && report_thresholds.medium < report_thresholds.high)) {
          throw new Error('Report activity thresholds must be positive and increase from Low to Medium to High.');
        }
        const updated = sourceDefaults(await api<Source>(`${endpoint}/sources/${encodeURIComponent(source.platform)}`, {
          method: 'PUT', signal: request.signal, body: JSON.stringify({ enabled, mode, query, interval_minutes,
            correlation_window_minutes, report_thresholds, expected_revision: saved.config_version, ...(token ? { bearer_token: token } : {}) }),
        }));
        setEditor({ draft: updated, saved: updated, token: '' }); onUpdate(updated); setMessage('Configuration saved. Capture was not started.');
      } else {
        const result = await api<{ status: string; message: string; source: Source }>(`${endpoint}/sources/${encodeURIComponent(source.platform)}/${kind}`, { method: 'POST', signal: request.signal });
        onUpdate(sourceDefaults(result.source));
        if (result.source.last_error) setError(result.source.last_error); else setMessage(result.message);
      }
    } catch (reason) { if (!request.signal.aborted) setError(prismaError(reason)); }
    finally { if (!request.signal.aborted) setBusy(''); controller.current = null; }
  }
  return <form className="prisma-source" onSubmit={(event: FormEvent) => { event.preventDefault(); void action('save'); }}>
    <div className="prisma-source-heading"><h3>{networkNames[source.platform]}</h3><div className="prisma-source-badges">
      <span className={`prisma-capture-state ${state.toLowerCase()}`} role="status"><span className="prisma-capture-dot" aria-hidden="true" />{source.capture_running && ['Scheduled', 'Capturing'].includes(state) ? `Running · ${state}` : state}</span>
      <span className={`prisma-mode ${source.mode}`}>{source.mode === 'real' ? 'REAL' : 'SIMULATED'}</span></div></div>
    <fieldset disabled={!!busy}>
      <label className="prisma-toggle"><input type="checkbox" checked={draft.enabled} onChange={event => updateDraft({ enabled: event.target.checked })} /> Source enabled</label>
      <div className="prisma-fields"><label>Producer mode<select value={draft.mode} onChange={event => updateDraft({ mode: event.target.value as Source['mode'] })}><option value="simulation">SIMULATED</option><option value="real" disabled={source.platform !== 'x'}>REAL{source.platform !== 'x' ? ' · unavailable' : ''}</option></select></label>
        <label>Capture interval (minutes)<input type="number" min="1" max="1440" step="1" required value={draft.interval_minutes} onChange={event => updateDraft({ interval_minutes: Number(event.target.value) })} /></label></div>
      <label>Searches · one per line<textarea rows={4} maxLength={5129} value={draft.query} onChange={event => updateDraft({ query: event.target.value })} placeholder={'#bogota #inundacion\n#colombia #incendio\n#desastre'} /></label>
      <small>Each line is a separate search. Use keywords, #tags or @mentions; up to 10 searches, 512 characters each. The default capture interval is 5 minutes. Results are written to Landing before processing.</small>
      <div className="prisma-fields"><label>Correlation window (minutes)<input type="number" min="1" max="1440" step="1" required value={draft.correlation_window_minutes} onChange={event => updateDraft({ correlation_window_minutes: Number(event.target.value) })} /></label>
        <label>Credential reference<input value={draft.secret_ref || ''} readOnly title="Reference managed by the kit" /></label></div>
      <fieldset className="prisma-thresholds"><legend>Report activity thresholds</legend><div className="prisma-threshold-fields">{(['low', 'medium', 'high'] as const).map(level => <label key={level}>{level[0].toUpperCase() + level.slice(1)}<input type="number" min="1" step="1" required value={draft.report_thresholds[level]} onChange={event => updateDraft({ report_thresholds: { ...draft.report_thresholds, [level]: Number(event.target.value) } })} /></label>)}</div>
        <small>Original reports within this network’s window. These thresholds measure report activity, not severity, truth or independent corroboration.</small></fieldset>
      <label>Update token<input type="password" autoComplete="new-password" value={token} onChange={event => setEditor(previous => ({ ...previous, token: event.target.value }))} placeholder={source.credential_configured ? 'Configured · leave blank to keep current' : 'No credential configured'} /></label>
      <div className="prisma-source-actions"><button type="submit">{busy === 'save' ? 'Saving…' : 'Save'}</button><button className="secondary" type="button" disabled={dirty} onClick={() => void action('test')}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        <button className="secondary" type="button" disabled={dirty || !saved.enabled} onClick={() => void action('run')}>{busy === 'run' ? 'Starting…' : 'Run now'}</button>
        <button className="secondary" type="button" disabled={!source.capture_running} onClick={() => void action('pause')}>{busy === 'pause' ? 'Pausing…' : 'Pause'}</button>
        {dirty && <button className="secondary" type="button" onClick={() => setEditor({ draft: source, saved: source, token: '' })}>Discard changes</button>}</div>
      {dirty && <small>Unsaved changes are kept while status refreshes. Save before Test or Run now.</small>}
      {dirty && source.config_version !== saved.config_version && <p role="status">Configuration changed elsewhere. Discard changes to load it; Save will check for a conflict.</p>}
      {message && <p role="status" className="prisma-success">{message}</p>}{error && <p role="alert" className="prisma-error">{error}</p>}
    </fieldset>
    <dl className="prisma-source-status"><div><dt>Last capture</dt><dd>{timestamp(source.last_run_at)}</dd></div><div><dt>Next capture</dt><dd>{source.next_due ? timestamp(source.next_due) : 'Not scheduled'}</dd></div>
      <div><dt>Records received · last successful capture</dt><dd>{source.last_received_count ?? 'No successful capture'}</dd></div><div><dt>Capture delay</dt><dd>{source.capture_running ? `${overdueSeconds} s` : 'Stopped'}</dd></div></dl>
    {source.last_error && !error && <p role="alert" className="prisma-error">{source.last_error}</p>}
  </form>;
}

export function PrismaAdmin({ api }: { api: PrismaApi }) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [selected, setSelected] = useState(() => {
    try { const saved = sessionStorage.getItem('territorial-control-network'); return saved && networks.includes(saved) ? saved : 'x'; }
    catch { return 'x'; }
  });
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => { try { sessionStorage.setItem('territorial-control-network', selected); } catch { /* Storage may be disabled. */ } }, [selected]);
  useEffect(() => {
    const controller = new AbortController(); let loading = false;
    async function load() {
      if (loading) return;
      loading = true;
      try {
        const result = await api<Configuration>(`${endpoint}/sources`, { signal: controller.signal });
        if (!controller.signal.aborted) { setConfig({ ...result, sources: result.sources.map(sourceDefaults) }); setError(''); }
      } catch (reason) { if (!controller.signal.aborted) setError(prismaError(reason)); }
      finally { loading = false; }
    }
    void load(); const timer = window.setInterval(() => { void load(); }, 5000);
    return () => { controller.abort(); window.clearInterval(timer); };
  }, [refreshKey]);
  function tabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const offset = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
    if (!offset && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? networks.length - 1 : (networks.indexOf(selected) + offset + networks.length) % networks.length;
    setSelected(networks[index]); tabs.current[networks[index]]?.focus();
  }
  function updated(source: Source) {
    setConfig(previous => previous ? { ...previous, sources: previous.sources.map(item => item.platform === source.platform ? source : item) } : previous);
    setRefreshKey(value => value + 1);
  }
  return <section className="prisma-admin application-release module-configuration">
    <a className="module-return" href="/admin/settings#application"><svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d="M19 12H5m6-6-6 6 6 6" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg>Return</a>
    <div className="prisma-heading"><div><p className="eyebrow">TERRITORIAL CONTROL · BOGOTÁ</p><h2>Source configuration</h2><p>Configure searches and start continuous capture for each network.</p></div><a className="prisma-open" href="/gods-eye-view/" target="_blank" rel="noopener noreferrer">Open God’s Eye View ↗</a></div>
    {error && <p role="alert" className="prisma-error">{error}</p>}{!config && !error && <p role="status">Loading configuration…</p>}
    {config && <><div className="prisma-sources-title"><h2>Selected networks</h2><button className="secondary" type="button" onClick={() => setRefreshKey(value => value + 1)}>Refresh status</button></div>
      <div className="settings-tabs prisma-network-tabs" role="tablist" aria-label="Social networks">{networks.map(platform => <button key={platform} ref={element => { tabs.current[platform] = element; }} id={`prisma-tab-${platform}`} className="settings-tab" type="button" role="tab"
        aria-selected={selected === platform} aria-controls={`prisma-panel-${platform}`} tabIndex={selected === platform ? 0 : -1} onClick={() => setSelected(platform)} onKeyDown={tabKey}>
        <img className="prisma-platform-logo" src={`/brand-icons/${platform}.svg`} width="22" height="22" alt="" />{networkNames[platform]}</button>)}</div>
      {networks.map(platform => { const source = config.sources.find(item => item.platform === platform); return <section key={platform} id={`prisma-panel-${platform}`} className="settings-panel prisma-network-panel" role="tabpanel" aria-labelledby={`prisma-tab-${platform}`} hidden={selected !== platform}>
        {source ? <><SourceCard source={source} api={api} onUpdate={updated} /><PrismaPosts api={api} platform={platform} active={selected === platform} refreshKey={refreshKey} /></> : <p>Source configuration is unavailable.</p>}</section>; })}
      <p className="prisma-icon-credit">Logos: <a href="https://github.com/simple-icons/simple-icons/tree/d9ea58066506bc80da65d5516813636b22b58a06" target="_blank" rel="noreferrer">Simple Icons</a> · <a href="/brand-icons/LICENSE.md" target="_blank">CC0</a>. Trademarks belong to their respective owners.</p></>}
  </section>;
}
