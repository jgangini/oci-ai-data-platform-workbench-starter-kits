import { FormEvent, useEffect, useRef, useState } from 'react';
import { LoadingIndicator } from './LoadingIndicator';
import { SettingsConfirmation } from './SettingsConfirmation';
import { godsEyeViewError, type GodsEyeViewApi } from './godsEyeViewAdminState';

type ViewerIdentity = { name: string; description: string };
const endpoint = '/api/admin/gods-eye-view/identity';

export function ViewerIdentitySettings({ api }: { api: GodsEyeViewApi }) {
  const [identity, setIdentity] = useState<ViewerIdentity | null>(null);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState(true);
  const [reload, setReload] = useState(0);
  const [confirmation, setConfirmation] = useState<ViewerIdentity | null>(null);
  const controller = useRef<AbortController | null>(null);
  useEffect(() => {
    const request = new AbortController(); controller.current = request;
    setBusy(true); setError('');
    void api<ViewerIdentity>(endpoint, { signal: request.signal }).then(value => {
      if (!request.signal.aborted) setIdentity(value);
    }).catch(reason => { if (!request.signal.aborted) setError(godsEyeViewError(reason)); })
      .finally(() => { if (controller.current === request) { controller.current = null; setBusy(false); } });
    return () => { request.abort(); controller.current?.abort(); controller.current = null; };
  }, [reload]);
  async function save() {
    if (!confirmation || controller.current) return;
    const request = new AbortController(); controller.current = request;
    setConfirmation(null); setBusy(true); setError(''); setMessage('');
    try {
      const value = await api<ViewerIdentity>(endpoint, { method: 'PUT', body: JSON.stringify(confirmation), signal: request.signal });
      if (!request.signal.aborted) { setIdentity(value); setMessage('Viewer identity saved. Reload the viewer to apply it.'); }
    } catch (reason) { if (!request.signal.aborted) setError(godsEyeViewError(reason)); }
    finally { if (controller.current === request) { controller.current = null; setBusy(false); } }
  }
  return <form className="viewer-identity-settings" onSubmit={(event: FormEvent) => { event.preventDefault(); if (identity && !controller.current && !confirmation) setConfirmation({ ...identity }); }} aria-label="Viewer identity">
    <fieldset disabled={busy || !identity}><legend>Viewer identity</legend>
      <div className="gods-eye-view-fields viewer-identity-fields">
        <label>Name<input maxLength={80} value={identity?.name ?? ''} placeholder="God's Eye View" onChange={event => setIdentity(previous => previous && { ...previous, name: event.target.value })} /></label>
        <label>Description<input maxLength={200} value={identity?.description ?? ''} placeholder="NO PLACE LEFT BEHIND" onChange={event => setIdentity(previous => previous && { ...previous, description: event.target.value })} /></label>
        <button type="submit">{busy && identity ? 'Saving…' : 'Save Name'}</button>
      </div><small>Leave a field blank to restore its original value.</small>
    </fieldset>
    {busy && !identity && <LoadingIndicator label="Loading viewer identity…" inline />}
    {error && <p role="alert" className="gods-eye-view-error">{error}</p>}
    {error && !identity && <button type="button" className="secondary" disabled={busy} onClick={() => setReload(value => value + 1)}>Retry</button>}
    {message && <p role="status" className="gods-eye-view-success">{message}</p>}
    {confirmation && <SettingsConfirmation title="Save viewer name and description?" confirmLabel="Save"
      description={<>Set the viewer name to <strong className="confirmation-value">{confirmation.name.trim() || "God's Eye View (original)"}</strong> and description to <strong className="confirmation-value">{confirmation.description.trim() || 'NO PLACE LEFT BEHIND (original)'}</strong> for everyone. Reload the viewer to update its browser tab, loading page and heading.</>}
      onCancel={() => setConfirmation(null)} onConfirm={() => void save()} />}
  </form>;
}
