import { FormEvent, KeyboardEvent, ReactNode, useEffect, useRef, useState } from 'react';
import { GodsEyeViewPosts } from './GodsEyeViewPosts';
import { GodsEyeViewSensors } from './GodsEyeViewSensors';
import { GodsEyeViewParameters } from './GodsEyeViewParameters';
import { CaptureScheduleForm } from './CaptureScheduleForm';
import { ViewerIdentitySettings } from './ViewerIdentitySettings';
import { SyntheticDataReset, type SyntheticReset } from './SyntheticDataReset';
import { captureState, networkNames, godsEyeViewEndpoint as endpoint, godsEyeViewError, refreshedEditor, sourceDefaults,
  sourceDirty, sourcePayload, sourceQueryLimit, timestamp, type CaptureSchedule, type GodsEyeViewApi, type Source, type SourceEditor } from './godsEyeViewAdminState';
import './gods-eye-view.css';

type Configuration = { sources: Source[]; runtime: string; social_schedule?: CaptureSchedule; synthetic_reset?: SyntheticReset };
const networks = Object.keys(networkNames);

function SourceCard({ source, platform = source?.platform || '', api, onUpdate, timeZone, disabled }: { source?: Source; platform?: string; api: GodsEyeViewApi; onUpdate: (source: Source) => void; timeZone: string; disabled: boolean }) {
  const [editor, setEditor] = useState<SourceEditor | null>(source ? { draft: source, saved: source, token: '' } : null);
  const draft = editor?.draft, saved = editor?.saved, token = editor?.token || '';
  const dirty = !!editor && sourceDirty(editor);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');
  const controller = useRef<AbortController | null>(null);
  const state = source ? captureState(source) : '';
  const overdueSeconds = source?.capture_running && source.next_due ? Math.max(0, Math.floor((Date.now() - Date.parse(source.next_due)) / 1000)) : 0;
  useEffect(() => { if (source) setEditor(previous => previous ? refreshedEditor(previous, source) : { draft: source, saved: source, token: '' }); }, [source]);
  useEffect(() => () => controller.current?.abort(), []);
  const updateDraft = (values: Partial<Source>) => setEditor(previous => previous ? ({ ...previous, draft: { ...previous.draft, ...values }, token: values.mode === 'Synthetic' ? '' : previous.token }) : previous);
  async function action(kind: 'save' | 'test' | 'run' | 'pause') {
    if (!source || !editor || disabled || controller.current) return;
    const request = new AbortController(); controller.current = request;
    setBusy(kind); setMessage(''); setError('');
    try {
      if (kind === 'save') {
        const updated = sourceDefaults(await api<Source>(`${endpoint}/sources/${encodeURIComponent(source.platform)}`, {
          method: 'PUT', signal: request.signal, body: JSON.stringify(sourcePayload(editor)),
        }));
        setEditor({ draft: updated, saved: updated, token: '' }); onUpdate(updated); setMessage('Configuration saved. Capture was not started.');
      } else {
        const result = await api<{ status: string; message: string; source: Source }>(`${endpoint}/sources/${encodeURIComponent(source.platform)}/${kind}`, { method: 'POST', signal: request.signal });
        onUpdate(sourceDefaults(result.source));
        if (result.source.last_error) setError(result.source.last_error); else setMessage(result.message);
      }
    } catch (reason) { if (!request.signal.aborted) setError(godsEyeViewError(reason)); }
    finally { if (!request.signal.aborted) setBusy(''); controller.current = null; }
  }
  return <form className="gods-eye-view-source" aria-label={`${networkNames[platform]} source configuration`} aria-busy={!source} onSubmit={(event: FormEvent) => { event.preventDefault(); void action('save'); }}>
    <div className="gods-eye-view-source-heading"><h3><img className="gods-eye-view-platform-logo" src={`/brand-icons/${platform}.svg`} width="22" height="22" alt="" />{networkNames[platform]}</h3><div className="gods-eye-view-source-badges">
      {state && <span className={`gods-eye-view-mode gods-eye-view-capture-state ${state.toLowerCase()}`} role="status" title={state}><span className={`gods-eye-view-capture-dot ${state.toLowerCase()}`} aria-hidden="true" />{state}</span>}</div></div>
    <fieldset disabled={!source || !editor || disabled || !!busy}>
      <div className="gods-eye-view-fields"><label>Producer mode<select value={draft?.mode || ''} onChange={event => updateDraft({ mode: event.target.value as Source['mode'] })}>{!draft && <option value="" />}<option value="Synthetic">Synthetic</option><option value="real" disabled={platform !== 'x'}>Credentials{platform !== 'x' ? ' · unavailable' : ''}</option></select></label>
        {draft?.mode !== 'real' && <label>Maximum synthetic records per capture<input type="number" min="1" max="100" step="1" required value={draft?.synthetic_batch_max ?? ''} onChange={event => updateDraft({ synthetic_batch_max: Number(event.target.value) })} /></label>}</div>
      {draft?.mode === 'real' && <fieldset className="gods-eye-view-credentials"><legend>Credentials</legend><div className="gods-eye-view-fields">
        <label>Credential reference<input value={draft.secret_ref || ''} readOnly title="Reference managed by the kit" /></label>
        <label>Update token<input type="password" autoComplete="new-password" value={token} onChange={event => setEditor(previous => previous ? ({ ...previous, token: event.target.value }) : previous)} placeholder={source?.credential_configured ? 'Configured · leave blank to keep current' : 'No credential configured'} /></label></div></fieldset>}
      <div className="gods-eye-view-query-field"><label>Searches · one per line<textarea rows={4} maxLength={sourceQueryLimit} aria-describedby={`gods-eye-view-query-count-${platform}`} aria-invalid={(draft?.query.length || 0) > sourceQueryLimit || undefined} value={draft?.query || ''} onChange={event => updateDraft({ query: event.target.value })} placeholder={draft ? '#bogota #inundacion\n#colombia #incendio\n#desastre' : ''} /></label>
        <small id={`gods-eye-view-query-count-${platform}`} className="gods-eye-view-query-count">{draft ? `${draft.query.length} / ${sourceQueryLimit}` : ''}</small></div>
      <fieldset className="gods-eye-view-thresholds"><legend>Report activity thresholds</legend><div className="gods-eye-view-threshold-fields">
        <label>Correlation window (minutes)<input type="number" min="1" max="1440" step="1" required value={draft?.correlation_window_minutes ?? ''} onChange={event => updateDraft({ correlation_window_minutes: Number(event.target.value) })} /></label>
        {(['low', 'medium', 'high'] as const).map(level => <label key={level}>{level[0].toUpperCase() + level.slice(1)}<input type="number" min="1" step="1" required value={draft?.report_thresholds[level] ?? ''} onChange={event => { if (draft) updateDraft({ report_thresholds: { ...draft.report_thresholds, [level]: Number(event.target.value) } }); }} /></label>)}</div>
        <small>Original reports within this network’s window. These thresholds measure report activity, not severity, truth or independent corroboration.</small></fieldset>
      <div className="gods-eye-view-source-actions"><button type="submit">{busy === 'save' ? 'Saving…' : 'Save'}</button><button className="secondary" type="button" disabled={dirty} onClick={() => void action('test')}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        <button className="secondary" type="button" disabled={dirty} onClick={() => void action('run')}>{busy === 'run' ? 'Starting…' : 'Run now'}</button>
        <button className="secondary" type="button" disabled={!source?.capture_running} onClick={() => void action('pause')}>{busy === 'pause' ? 'Pausing…' : 'Pause'}</button>
        {dirty && source && <button className="secondary" type="button" onClick={() => setEditor({ draft: source, saved: source, token: '' })}>Discard changes</button>}</div>
      {dirty && <small>Unsaved changes are kept while status refreshes. Save before Test or Run now.</small>}
      {dirty && source?.config_version !== saved?.config_version && <p role="status">Configuration changed elsewhere. Discard changes to load it; Save will check for a conflict.</p>}
      {message && <p role="status" className="gods-eye-view-success">{message}</p>}{error && <p role="alert" className="gods-eye-view-error">{error}</p>}
    </fieldset>
    {source && <dl className="gods-eye-view-source-status"><div><dt>Last capture</dt><dd>{timestamp(source.last_run_at, timeZone)}</dd></div><div><dt>Next capture</dt><dd>{source.next_due ? timestamp(source.next_due, timeZone) : 'Not scheduled'}</dd></div>
      <div><dt>Records received · last successful capture</dt><dd>{source.last_received_count ?? 'No successful capture'}</dd></div><div><dt>Capture delay</dt><dd>{source.capture_running ? `${overdueSeconds} s` : 'Stopped'}</dd></div></dl>}
    {source?.last_error && !error && <p role="alert" className="gods-eye-view-error">{source.last_error}</p>}
  </form>;
}

export function GodsEyeViewAdmin({ api, timeZone = 'America/Bogota', viewerUrlControl, searchIcon, refreshIcon }: { api: GodsEyeViewApi; timeZone?: string; viewerUrlControl: ReactNode; searchIcon: ReactNode; refreshIcon: ReactNode }) {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [error, setError] = useState('');
  const [refreshKey, setRefreshKey] = useState(0);
  const [resetRevision, setResetRevision] = useState(0);
  const [resetView, setResetView] = useState<{ state: SyntheticReset; error: string }>({ state: {}, error: '' });
  const resetBlocked = ['pending', 'error'].includes(resetView.state.status || '');
  const [collapsed, setCollapsed] = useState(false);
  const [module, setModule] = useState(() => window.location.hash === '#parameters' ? 'parameters' : 'social');
  const [selected, setSelected] = useState(() => {
    try { const saved = sessionStorage.getItem('gods-eye-view-control-network'); return saved && networks.includes(saved) ? saved : 'x'; }
    catch { return 'x'; }
  });
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  useEffect(() => { const navigate = () => { if (window.location.hash === '#parameters') setModule('parameters'); }; window.addEventListener('hashchange', navigate); return () => window.removeEventListener('hashchange', navigate); }, []);
  useEffect(() => { try { sessionStorage.setItem('gods-eye-view-control-network', selected); } catch { /* Storage may be disabled. */ } }, [selected]);
  useEffect(() => {
    if (module !== 'social') return;
    const controller = new AbortController(); let loading = false;
    async function load() {
      if (loading || document.hidden) return;
      loading = true;
      try {
        const result = await api<Configuration>(`${endpoint}/sources`, { signal: controller.signal });
        if (!controller.signal.aborted) { setConfig({ ...result, sources: result.sources.map(sourceDefaults) }); setError(''); }
      } catch (reason) { if (!controller.signal.aborted) setError(godsEyeViewError(reason)); }
      finally { loading = false; }
    }
    const resume = () => { void load(); };
    resume(); const timer = window.setInterval(resume, 5000);
    document.addEventListener('visibilitychange', resume);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', resume); };
  }, [refreshKey, module]);
  function selectNetwork(platform: string) { setSelected(platform); setCollapsed(false); }
  function tabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const offset = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
    if (!offset && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? networks.length - 1 : (networks.indexOf(selected) + offset + networks.length) % networks.length;
    selectNetwork(networks[index]); tabs.current[networks[index]]?.focus();
  }
  function updated(source: Source) {
    setConfig(previous => previous ? { ...previous, sources: previous.sources.map(item => item.platform === source.platform ? source : item) } : previous);
    setRefreshKey(value => value + 1);
  }
  return <section className="gods-eye-view-admin settings-panel module-configuration">
    <div className="gods-eye-view-heading"><div><p className="eyebrow">God’s Eye View</p><h2>Source configuration</h2></div></div>
    {viewerUrlControl}
    <ViewerIdentitySettings api={api} />
    <div className="settings-tabs gods-eye-view-module-tabs" role="group" aria-label="Source modules"><button type="button" className="settings-tab" aria-pressed={module === 'social'} onClick={() => setModule('social')}>Social Networks</button><button type="button" className="settings-tab" aria-pressed={module === 'sensors'} onClick={() => setModule('sensors')}>Sensors</button><button type="button" className="settings-tab" aria-pressed={module === 'parameters'} onClick={() => setModule('parameters')}>Parameters</button></div>
    <div hidden={module !== 'social'} className="gods-eye-view-module-content">
    <div className="gods-eye-view-sources-title"><h2>Social Networks</h2></div>
    {error && <p role="alert" className="gods-eye-view-error">{error}</p>}
      <CaptureScheduleForm schedule={config?.social_schedule} api={api} kind="social" disabled={resetBlocked}
        onUpdate={social_schedule => { setConfig(previous => previous ? { ...previous, social_schedule } : previous); setRefreshKey(value => value + 1); }} />
      <div className="gods-eye-view-network-toolbar"><div className="settings-tabs gods-eye-view-network-tabs" role="tablist" aria-label="Social networks">{networks.map(platform => { const source = config?.sources.find(item => item.platform === platform); const state = source ? captureState(source) : ''; return <button key={platform} ref={element => { tabs.current[platform] = element; }} id={`gods-eye-view-tab-${platform}`} className="settings-tab" type="button" role="tab"
        aria-selected={selected === platform} aria-expanded={selected === platform && !collapsed} aria-controls={`gods-eye-view-panel-${platform}`} tabIndex={selected === platform ? 0 : -1} onClick={() => selected === platform ? setCollapsed(value => !value) : selectNetwork(platform)} onKeyDown={tabKey}>
        <img className="gods-eye-view-platform-logo" src={`/brand-icons/${platform}.svg`} width="22" height="22" alt="" />{networkNames[platform]}{state && <span className={`gods-eye-view-capture-dot ${state.toLowerCase()}`} role="img" aria-label={state} title={state} />}</button>; })}</div>
        <div className="gods-eye-view-network-actions" role="group" aria-label="Source configuration actions">
          <button type="button" className="secondary gods-eye-view-toolbar-button gods-eye-view-form-toggle" aria-expanded={!collapsed} aria-controls="gods-eye-view-source-forms" aria-label={collapsed ? 'Expand source configuration' : 'Collapse source configuration'} title={collapsed ? 'Expand source configuration' : 'Collapse source configuration'} onClick={() => setCollapsed(value => !value)}>
            <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d={collapsed ? 'm6 9 6 6 6-6' : 'm6 15 6-6 6 6'} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg></button>
          <button type="button" className="secondary gods-eye-view-toolbar-button" aria-label="Refresh status" title="Refresh status" onClick={() => setRefreshKey(value => value + 1)}>{refreshIcon}</button>
          <SyntheticDataReset api={api} status={config?.synthetic_reset} runtime={config?.runtime || ''} disabled={!config} onChange={(state, resetError) => setResetView({ state, error: resetError })}
            onComplete={() => { setResetRevision(value => value + 1); setRefreshKey(value => value + 1); }} />
        </div></div>
      <div id="gods-eye-view-source-forms" hidden={collapsed}>{networks.map(platform => { const source = config?.sources.find(item => item.platform === platform); return <section key={platform} id={`gods-eye-view-panel-${platform}`} className="settings-panel gods-eye-view-network-panel" role="tabpanel" aria-labelledby={`gods-eye-view-tab-${platform}`} hidden={selected !== platform}>
        <SourceCard source={source} platform={platform} api={api} onUpdate={updated} timeZone={timeZone} disabled={resetBlocked} />
        {config && !source && <p>Source configuration is unavailable.</p>}</section>; })}</div>
      <GodsEyeViewPosts key={resetRevision} api={api} active={module === 'social'} refreshKey={refreshKey} timeZone={timeZone} searchIcon={searchIcon} refreshIcon={refreshIcon} />
    </div><div hidden={module !== 'sensors'}><GodsEyeViewSensors api={api} timeZone={timeZone} active={module === 'sensors'} searchIcon={searchIcon} refreshIcon={refreshIcon} /></div>
    <div hidden={module !== 'parameters'}><GodsEyeViewParameters api={api} active={module === 'parameters'} /></div>
  </section>;
}
