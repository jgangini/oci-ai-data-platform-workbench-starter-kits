import { FormEvent, useEffect, useRef, useState } from 'react';
import { godsEyeViewEndpoint, godsEyeViewError, scheduleLocalTime, schedulePayload, type CaptureSchedule, type GodsEyeViewApi } from './godsEyeViewAdminState';
import { SettingsConfirmation } from './SettingsConfirmation';

export function CaptureScheduleForm({ schedule, api, kind, disabled, onUpdate }: {
  schedule?: CaptureSchedule; api: GodsEyeViewApi; kind: 'social' | 'sensor'; disabled: boolean; onUpdate: (schedule: CaptureSchedule) => void;
}) {
  const [saved, setSaved] = useState(schedule);
  const [start, setStart] = useState(() => scheduleLocalTime(schedule?.start_at ?? null));
  const [interval, setInterval] = useState<number | ''>(schedule?.interval_minutes ?? '');
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const [confirmation, setConfirmation] = useState<ReturnType<typeof schedulePayload> | null>(null);
  const controller = useRef<AbortController | null>(null);
  const dirty = !!saved && (start !== scheduleLocalTime(saved.start_at) || interval !== saved.interval_minutes);
  function receive(value: CaptureSchedule) { setSaved(value); setStart(scheduleLocalTime(value.start_at)); setInterval(value.interval_minutes); }
  useEffect(() => { if (!dirty && !confirmation && schedule && (!saved || schedule.config_version >= saved.config_version)) receive(schedule); }, [schedule]);
  useEffect(() => () => controller.current?.abort(), []);
  function review() {
    if (disabled || !saved || controller.current || confirmation) return;
    try { setConfirmation(schedulePayload(start, Number(interval), saved.config_version)); setError(''); }
    catch (reason) { setError(godsEyeViewError(reason)); }
  }
  async function save() {
    if (disabled || !confirmation || controller.current) return;
    const request = new AbortController(); controller.current = request;
    setConfirmation(null); setBusy(true); setError(''); setMessage('');
    try {
      const result = await api<CaptureSchedule>(`${godsEyeViewEndpoint}/${kind}-schedule`, { method: 'PUT', signal: request.signal,
        body: JSON.stringify(confirmation) });
      if (!request.signal.aborted) { receive(result); onUpdate(result); setMessage('Schedule saved. Paused and completed captures remain stopped.'); }
    } catch (reason) { if (!request.signal.aborted) setError(godsEyeViewError(reason)); }
    finally { if (!request.signal.aborted) setBusy(false); controller.current = null; }
  }
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return <form className="gods-eye-view-source" aria-label={`${kind === 'social' ? 'Social networks' : 'Sensors'} capture schedule`} aria-busy={!saved} onSubmit={(event: FormEvent) => { event.preventDefault(); review(); }}>
    <fieldset disabled={disabled || busy || !saved}>
      <div className="gods-eye-view-fields gods-eye-view-schedule-fields"><label>Scheduled · {zone}<input type="datetime-local" step="1" required value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>Capture interval (minutes)<input type="number" min="1" max="1440" step="1" required value={interval} onChange={event => setInterval(event.target.value === '' ? '' : Number(event.target.value))} /></label>
        <button type="submit">{busy ? 'Saving…' : 'Save schedule'}</button></div>
      {saved && <small>{!saved.start_at && 'Schedule not configured. '}Applies to {kind === 'social' ? 'all four social networks' : 'all sensor types'}. Times use your browser time zone; paused and completed captures stay stopped.</small>}
      {dirty && schedule && <div className="gods-eye-view-source-actions"><button type="button" className="secondary" onClick={() => { receive(schedule); setError(''); setMessage(''); }}>Discard schedule changes</button></div>}
      {dirty && schedule && schedule.config_version !== saved?.config_version && <p role="status">Schedule changed elsewhere. Discard changes to load it; Save will check for a conflict.</p>}
      {message && <p role="status" className="gods-eye-view-success">{message}</p>}{error && <p role="alert" className="gods-eye-view-error">{error}</p>}
    </fieldset>
    {confirmation && <SettingsConfirmation title={`Save ${kind === 'social' ? 'social networks' : 'sensors'} capture schedule?`} confirmLabel="Save schedule"
      description={`This replaces the shared start time and capture interval for ${kind === 'social' ? 'all four social networks' : 'all sensor types'}. Paused and completed captures remain stopped.`}
      changes={[`Scheduled: ${scheduleLocalTime(confirmation.start_at).replace('T', ' ')} · ${zone}`, `Capture interval: ${confirmation.interval_minutes} minutes`]}
      onCancel={() => setConfirmation(null)} onConfirm={() => void save()} />}
  </form>;
}
