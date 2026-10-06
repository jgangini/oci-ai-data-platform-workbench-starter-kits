import { LoadingIndicator } from './LoadingIndicator';
import { FormEvent, KeyboardEvent, ReactNode, useEffect, useRef, useState } from 'react';
import { godsEyeViewEndpoint, godsEyeViewError, timestamp, type CaptureSchedule, type GodsEyeViewApi } from './godsEyeViewAdminState';
import { CaptureScheduleForm } from './CaptureScheduleForm';
import { GodsEyeViewSensorReadings } from './GodsEyeViewSensorReadings';
import { SyntheticDataReset, type SyntheticReset } from './SyntheticDataReset';

export const sensorFamilies: Record<string, string> = { river_level: 'River Level', rainfall: 'Rainfall', temperature: 'Temperature', soil_moisture: 'Soil Moisture', wind_speed: 'Wind Speed' };
const families = Object.keys(sensorFamilies);
const sensorIcons: Record<string, string> = {
  river_level: 'M3 16q3-3 6 0t6 0t6 0M3 21q3-3 6 0t6 0t6 0M12 2v9m-4-4 4 4 4-4',
  rainfall: 'M6 14a4 4 0 0 1-1-8 6 6 0 0 1 11-1 4.5 4.5 0 0 1 2 9M7 17l-1 3m6-3-1 3m6-3-1 3',
  temperature: 'M9 14V5a3 3 0 0 1 6 0v9a5 5 0 1 1-6 0M12 8v10m-1 0h2',
  soil_moisture: 'M4 13c0-7 8-10 15-10 0 9-4 14-11 14M4 20l10-10M2 23h20',
  wind_speed: 'M3 8h12a3 3 0 1 0-3-3M2 12h17a3 3 0 1 1-3 3M4 16h5a3 3 0 1 1-3 3',
};
type SensorConfig = { sensor_type: string; config_version: number; mode: 'Synthetic'; is_simulated: true; interval_minutes: number; sensor_count: number;
  capture_running: boolean; last_run_at?: string; next_due?: string; last_received_count?: number; last_error?: string; reset?: SyntheticReset };
type SensorEditor = { saved: SensorConfig; draft: SensorConfig };
export const sensorDirty = ({ saved, draft }: SensorEditor) => saved.sensor_count !== draft.sensor_count;
const endpoint = `${godsEyeViewEndpoint}/sensors`;

export function GodsEyeViewSensors({ api, timeZone, active = true, searchIcon, refreshIcon }: { api: GodsEyeViewApi; timeZone: string; active?: boolean; searchIcon: ReactNode; refreshIcon: ReactNode }) {
  const [selected, setSelected] = useState(families[0]), [collapsed, setCollapsed] = useState(false), [refresh, setRefresh] = useState(0);
  const tabs = useRef<Record<string, HTMLButtonElement | null>>({});
  const [configs, setConfigs] = useState<Record<string, SensorConfig> | null>(null);
  const [schedule, setSchedule] = useState<CaptureSchedule | null>(null);
  const [editors, setEditors] = useState<Record<string, SensorEditor>>({});
  const [feedback, setFeedback] = useState<Record<string, { busy: string; error: string; message: string }>>({});
  const [loadError, setLoadError] = useState('');
  const [runtime, setRuntime] = useState(''), [resetRevision, setResetRevision] = useState(0);
  const [reset, setReset] = useState<SyntheticReset>({});
  const [resetView, setResetView] = useState<{ state: SyntheticReset; error: string } | null>(null);
  const mutations = useRef<Record<string, AbortController>>({});
  const revisions = useRef<Record<string, number>>({});
  const config = configs?.[selected], editor = editors[selected];
  const { busy = '', error = '', message = '' } = feedback[selected] || {};
  const resetBlocked = [reset, resetView?.state].some(state => ['pending', 'error'].includes(state?.status || ''));
  const dirty = !!editor && sensorDirty(editor);
  const overdueSeconds = config?.capture_running && config.next_due ? Math.max(0, Math.floor((Date.now() - Date.parse(config.next_due)) / 1000)) : 0;
  const receive = (next: SensorConfig) => {
    const type = next.sensor_type;
    setConfigs(previous => previous && previous[type]?.config_version > next.config_version ? previous : { ...previous, [type]: next });
    setEditors(previous => previous[type] && (sensorDirty(previous[type]) || previous[type].saved.config_version > next.config_version) ? previous : { ...previous, [type]: { saved: next, draft: next } });
  };
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(); let loading = false;
    const load = async () => {
      if (loading || document.hidden) return;
      loading = true; const sequence = { ...revisions.current }, pending = new Set(Object.keys(mutations.current));
      try {
        const result = await api<{ configs: SensorConfig[]; runtime: string; sensor_schedule?: CaptureSchedule; reset?: SyntheticReset }>(endpoint, { signal: controller.signal });
        if (!controller.signal.aborted) {
          setRuntime(result.runtime);
          setReset(result.reset?.operation_id ? result.reset : result.configs.find(item => ['pending', 'error'].includes(item.reset?.status || ''))?.reset || {});
          if (result.sensor_schedule) setSchedule(previous => previous && previous.config_version > result.sensor_schedule!.config_version ? previous : result.sensor_schedule!);
          for (const next of result.configs) if (!pending.has(next.sensor_type) && !mutations.current[next.sensor_type] && sequence[next.sensor_type] === revisions.current[next.sensor_type]) receive(next);
          setLoadError('');
        }
      }
      catch (reason) { if (!controller.signal.aborted) setLoadError(godsEyeViewError(reason)); }
      finally { loading = false; }
    };
    const resume = () => { void load(); };
    resume(); const timer = window.setInterval(resume, 5000);
    document.addEventListener('visibilitychange', resume);
    return () => { controller.abort(); window.clearInterval(timer); document.removeEventListener('visibilitychange', resume); };
  }, [api, refresh, active]);
  useEffect(() => () => { for (const controller of Object.values(mutations.current)) controller.abort(); }, []);
  function tabKey(event: KeyboardEvent<HTMLButtonElement>) {
    const offset = ['ArrowRight', 'ArrowDown'].includes(event.key) ? 1 : ['ArrowLeft', 'ArrowUp'].includes(event.key) ? -1 : 0;
    if (!offset && !['Home', 'End'].includes(event.key)) return;
    event.preventDefault();
    const index = event.key === 'Home' ? 0 : event.key === 'End' ? families.length - 1 : (families.indexOf(selected) + offset + families.length) % families.length;
    setSelected(families[index]); setCollapsed(false); tabs.current[families[index]]?.focus();
  }
  async function action(kind: 'save' | 'run' | 'pause') {
    const type = selected;
    if (!editor || resetBlocked || mutations.current[type]) return;
    const controller = new AbortController(); mutations.current[type] = controller; revisions.current[type] = (revisions.current[type] || 0) + 1;
    const status = (values: Partial<{ busy: string; error: string; message: string }>) => setFeedback(previous => ({ ...previous, [type]: { ...(previous[type] || { busy: '', error: '', message: '' }), ...values } }));
    status({ busy: kind, error: '', message: '' });
    try {
      const { sensor_count } = editor.draft;
      if (kind === 'save' && (!Number.isInteger(sensor_count) || sensor_count < 1 || sensor_count > 5000)) throw new Error('Enter whole numbers: 1–5000 sensors.');
      const path = `${endpoint}/${encodeURIComponent(type)}`;
      const result = await api<{ config: SensorConfig; message?: string }>(kind === 'save' ? path : `${path}/${kind}`, {
        method: kind === 'save' ? 'PUT' : 'POST', signal: controller.signal,
        ...(kind === 'save' ? { body: JSON.stringify({ expected_revision: editor.saved.config_version, sensor_count }) } : {}),
      });
      if (controller.signal.aborted) return;
      if (result.config.sensor_type !== type) throw new Error('The server returned a different sensor type. Refresh its status before trying again.');
      receive(result.config); setLoadError('');
      if (kind === 'save') setEditors(previous => ({ ...previous, [type]: { saved: result.config, draft: result.config } }));
      // Own Pause advances the revision without invalidating a draft; external edits still require conflict resolution.
      else if (kind === 'pause' && config?.config_version === editor.saved.config_version && result.config.config_version === editor.saved.config_version + 1 && !sensorDirty({ saved: editor.saved, draft: result.config }))
        setEditors(previous => ({ ...previous, [type]: { saved: result.config, draft: { ...result.config, sensor_count } } }));
      status({ message: result.message || (kind === 'save' ? 'Sensor configuration saved.' : 'Sensor capture updated.') });
    } catch (reason) { if (!controller.signal.aborted) status({ error: godsEyeViewError(reason) }); }
    finally { if (!controller.signal.aborted) status({ busy: '' }); if (mutations.current[type] === controller) delete mutations.current[type]; }
  }
  const update = (values: Partial<SensorConfig>) => setEditors(previous => ({ ...previous, [selected]: { ...previous[selected], draft: { ...previous[selected].draft, ...values } } }));
  return <section className="gods-eye-view-sensors" aria-label="Sensors configuration">
    <div className="gods-eye-view-sources-title"><h2>Sensors</h2></div>
    {schedule && <CaptureScheduleForm schedule={schedule} api={api} kind="sensor"
      disabled={resetBlocked}
      onUpdate={value => { setSchedule(value); setRefresh(previous => previous + 1); }} />}
    <div className="gods-eye-view-network-toolbar">
      <div className="settings-tabs gods-eye-view-network-tabs" role="tablist" aria-label="Sensor types">{families.map(family => {
        const state = !configs?.[family] ? 'Unavailable' : configs[family].capture_running ? 'Running' : 'Paused';
        return <button key={family} ref={element => { tabs.current[family] = element; }} id={`gods-eye-view-sensor-tab-${family}`} className="settings-tab" type="button" role="tab"
          aria-selected={selected === family} aria-expanded={selected === family && !collapsed} aria-controls="gods-eye-view-sensor-readings-panel" tabIndex={selected === family ? 0 : -1} onClick={() => { setCollapsed(value => selected === family ? !value : false); setSelected(family); }} onKeyDown={tabKey}>
          <svg className="gods-eye-view-platform-logo" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d={sensorIcons[family]} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>{sensorFamilies[family]}
          <span className={`gods-eye-view-capture-dot ${state.toLowerCase()}`} role="img" aria-label={state} title={state} /></button>;
      })}</div>
      <div className="gods-eye-view-network-actions" role="group" aria-label="Sensor configuration actions">
        <button type="button" className="secondary gods-eye-view-toolbar-button gods-eye-view-form-toggle" aria-expanded={!collapsed} aria-controls="gods-eye-view-sensor-configuration" aria-label={collapsed ? 'Expand sensor configuration' : 'Collapse sensor configuration'} title={collapsed ? 'Expand sensor configuration' : 'Collapse sensor configuration'} onClick={() => setCollapsed(value => !value)}>
          <svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path d={collapsed ? 'm6 9 6 6 6-6' : 'm6 15 6-6 6 6'} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" /></svg></button>
        <button type="button" className="secondary gods-eye-view-toolbar-button" disabled={!!busy} aria-label="Refresh sensor status" title="Refresh sensor status" onClick={() => setRefresh(value => value + 1)}>{refreshIcon}</button>
        <SyntheticDataReset api={api} runtime={runtime} sensor={{ type: 'all', label: 'Synthetic sensor', labels: sensorFamilies }} status={reset}
          disabled={!configs || Object.values(feedback).some(item => !!item.busy)}
          onChange={(state, error) => setResetView({ state, error })}
          onComplete={() => { setRefresh(value => value + 1); setResetRevision(value => value + 1); }} />
      </div>
    </div>
    {!config && !error && !loadError && <LoadingIndicator label="Loading sensor configuration…" />}
    <section id="gods-eye-view-sensor-readings-panel" className="gods-eye-view-module-content" role="tabpanel" aria-labelledby={`gods-eye-view-sensor-tab-${selected}`}>
    <div id="gods-eye-view-sensor-configuration" hidden={collapsed}>{config && editor && <form className="gods-eye-view-source" onSubmit={(event: FormEvent) => { event.preventDefault(); void action('save'); }}>
      <div className="gods-eye-view-source-heading"><h3><svg className="gods-eye-view-platform-logo" viewBox="0 0 24 24" width="22" height="22" aria-hidden="true"><path d={sensorIcons[selected]} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" /></svg>{sensorFamilies[selected]}</h3><div className="gods-eye-view-source-badges"><span className={`gods-eye-view-mode gods-eye-view-capture-state ${config.capture_running ? 'running' : 'paused'}`} role="status" title={config.capture_running ? 'Running' : 'Paused'}><span className={`gods-eye-view-capture-dot ${config.capture_running ? 'running' : 'paused'}`} aria-hidden="true" />{config.capture_running ? 'Running' : 'Paused'}</span></div></div>
      <fieldset disabled={resetBlocked || !!busy}><div className="gods-eye-view-fields">
        <label>Producer mode<select value={editor.draft.mode} onChange={() => update({ mode: 'Synthetic' })} aria-describedby="gods-eye-view-sensor-mode-note"><option value="Synthetic">Synthetic</option><option value="real" disabled>Credentials · unavailable</option></select></label>
        {editor.draft.mode === 'Synthetic' && <label>Number of sensors<input type="number" min="1" max="5000" step="1" required value={editor.draft.sensor_count} onChange={event => update({ sensor_count: Number(event.target.value) })} /></label>}</div>
        <small id="gods-eye-view-sensor-mode-note">Real sensor ingestion is not available yet.</small>
        <div className="gods-eye-view-source-actions"><button type="submit">{busy === 'save' ? 'Saving…' : 'Save'}</button><button type="button" className="secondary" disabled={dirty} onClick={() => void action('run')}>{busy === 'run' ? 'Starting…' : 'Run now'}</button><button type="button" className="secondary" disabled={!config.capture_running} onClick={() => void action('pause')}>{busy === 'pause' ? 'Pausing…' : 'Pause'}</button>
          {dirty && <button type="button" className="secondary" onClick={() => setEditors(previous => ({ ...previous, [selected]: { saved: config, draft: config } }))}>Discard changes</button>}</div>
        {dirty && <small>Unsaved changes are kept while status refreshes. Save before Run now.</small>}
        {dirty && config.config_version !== editor.saved.config_version && <p role="status">Configuration changed elsewhere. Discard changes to load it; Save will check for a conflict.</p>}
      </fieldset>
      <dl className="gods-eye-view-source-status"><div><dt>Last capture</dt><dd>{timestamp(config.last_run_at, timeZone)}</dd></div><div><dt>Next capture</dt><dd>{config.next_due ? timestamp(config.next_due, timeZone) : 'Not scheduled'}</dd></div><div><dt>Records received · last successful capture</dt><dd>{config.last_received_count ?? 'No successful capture'}</dd></div><div><dt>Capture delay</dt><dd>{config.capture_running ? `${overdueSeconds} s` : 'Stopped'}</dd></div></dl>
      {config.last_error && !error && <p role="alert" className="gods-eye-view-error">{config.last_error}</p>}
    </form>}</div>
    {message && <p role="status" className="gods-eye-view-success">{message}</p>}{(error || loadError) && <p role="alert" className="gods-eye-view-error">{error || loadError}</p>}
      <GodsEyeViewSensorReadings key={resetRevision} api={api} timeZone={timeZone} active={active} family={selected} families={sensorFamilies} searchIcon={searchIcon} refreshIcon={refreshIcon} />
    </section>
  </section>;
}
