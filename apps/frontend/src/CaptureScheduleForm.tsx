import { FormEvent, useEffect, useRef, useState } from 'react';
import { prismaEndpoint, prismaError, scheduleLocalTime, schedulePayload, type CaptureSchedule, type PrismaApi } from './prismaAdminState';

export function CaptureScheduleForm({ schedule, api, kind, disabled, onUpdate }: {
  schedule: CaptureSchedule; api: PrismaApi; kind: 'social' | 'sensor'; disabled: boolean; onUpdate: (schedule: CaptureSchedule) => void;
}) {
  const [saved, setSaved] = useState(schedule);
  const [start, setStart] = useState(() => scheduleLocalTime(schedule.start_at));
  const [interval, setInterval] = useState(schedule.interval_minutes);
  const [busy, setBusy] = useState(false), [error, setError] = useState(''), [message, setMessage] = useState('');
  const controller = useRef<AbortController | null>(null);
  const dirty = start !== scheduleLocalTime(saved.start_at) || interval !== saved.interval_minutes;
  function receive(value: CaptureSchedule) { setSaved(value); setStart(scheduleLocalTime(value.start_at)); setInterval(value.interval_minutes); }
  useEffect(() => { if (!dirty && schedule.config_version >= saved.config_version) receive(schedule); }, [schedule]);
  useEffect(() => () => controller.current?.abort(), []);
  async function save() {
    if (disabled || controller.current) return;
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError(''); setMessage('');
    try {
      const result = await api<CaptureSchedule>(`${prismaEndpoint}/${kind}-schedule`, { method: 'PUT', signal: request.signal,
        body: JSON.stringify(schedulePayload(start, interval, saved.config_version)) });
      if (!request.signal.aborted) { receive(result); onUpdate(result); setMessage('Schedule saved. Paused and completed captures remain stopped.'); }
    } catch (reason) { if (!request.signal.aborted) setError(prismaError(reason)); }
    finally { if (!request.signal.aborted) setBusy(false); controller.current = null; }
  }
  const zone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return <form className="prisma-source" aria-label={`${kind === 'social' ? 'Social networks' : 'Sensors'} capture schedule`} onSubmit={(event: FormEvent) => { event.preventDefault(); void save(); }}>
    <fieldset disabled={disabled || busy}>
      <div className="prisma-fields"><label>Scheduled · {zone}<input type="datetime-local" step="1" required value={start} onChange={event => setStart(event.target.value)} /></label>
        <label>Capture interval (minutes)<input type="number" min="1" max="1440" step="1" required value={interval} onChange={event => setInterval(Number(event.target.value))} /></label></div>
      <small>{!saved.start_at && 'Schedule not configured. '}Applies to {kind === 'social' ? 'all four social networks' : 'all sensor types'}. Times use your browser time zone; paused and completed captures stay stopped.</small>
      <div className="prisma-source-actions"><button type="submit">{busy ? 'Saving…' : 'Save schedule'}</button>
        {dirty && <button type="button" className="secondary" onClick={() => { receive(schedule); setError(''); setMessage(''); }}>Discard schedule changes</button>}</div>
      {dirty && schedule.config_version !== saved.config_version && <p role="status">Schedule changed elsewhere. Discard changes to load it; Save will check for a conflict.</p>}
      {message && <p role="status" className="prisma-success">{message}</p>}{error && <p role="alert" className="prisma-error">{error}</p>}
    </fieldset>
  </form>;
}
