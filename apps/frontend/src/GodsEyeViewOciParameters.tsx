import { LoadingIndicator } from './LoadingIndicator';
import { FormEvent, useEffect, useRef, useState } from 'react';
import { godsEyeViewEndpoint, godsEyeViewError, type GodsEyeViewApi } from './godsEyeViewAdminState';
import { SettingsConfirmation } from './SettingsConfirmation';

type OciStatus = { configured: boolean; available: boolean; region: string; model_id: string; model_name: string; voice?: string; voices?: string[]; tts_model?: string; last_test?: { status: string; error?: { message?: string } } };
type Model = { id: string; name: string; vendor: string; selectable: boolean; reason?: string };

export function GodsEyeViewOciParameters({ api, active, voice = false, onConfigured }: { api: GodsEyeViewApi; active: boolean; voice?: boolean; onConfigured?: (configured: boolean) => void }) {
  const endpoint = `${godsEyeViewEndpoint}/${voice ? 'oci-voice' : 'oci-provider'}`;
  const title = `OCI Generative AI · ${voice ? 'voice' : 'text'}`;
  const [saved, setSaved] = useState<OciStatus | null>(null);
  const [model, setModel] = useState('');
  const [selectedVoice, setSelectedVoice] = useState('ara');
  const [models, setModels] = useState<Model[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [confirm, setConfirm] = useState(false);
  const [retry, setRetry] = useState(0);
  const request = useRef<AbortController | null>(null);
  const savedGeneration = useRef(0);
  const dirty = !!saved && (model !== saved.model_id || voice && selectedVoice !== saved.voice);
  const editor = useRef({ dirty, saved }); editor.current = { dirty, saved };
  function accept(next: OciStatus) { setSaved(next); setModel(next.model_id); setSelectedVoice(next.voice || 'ara'); }
  function refreshSaved(next: OciStatus) { if (editor.current.dirty) setSaved(next); else accept(next); }
  useEffect(() => { if (saved) onConfigured?.(saved.configured); }, [saved?.configured, onConfigured]);
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController(), generation = savedGeneration.current; setLoading(true); setError('');
    void api<OciStatus>(endpoint, { signal: controller.signal }).then(next => { if (!controller.signal.aborted && generation === savedGeneration.current) { refreshSaved(next); setMessage(''); } })
      .catch(reason => { if (!controller.signal.aborted && generation === savedGeneration.current) setError(godsEyeViewError(reason)); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [active, retry]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => { if (!active) { request.current?.abort(); request.current = null; setBusy(''); setLoading(false); setConfirm(false); } }, [active]);
  async function action(kind: 'save' | 'test' | 'models') {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    setConfirm(false); setBusy(kind); setError(''); setMessage('');
    try {
      if (kind === 'models') {
        const result = await api<{ items: Model[]; next_cursor: string | null }>(`${endpoint}/models${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`, { signal: controller.signal });
        if (!controller.signal.aborted) { setModels(previous => [...new Map([...previous, ...result.items].map(item => [item.id, item])).values()]); setCursor(result.next_cursor); }
      } else if (kind === 'save') {
        const result = await api<OciStatus>(endpoint, { method: 'PUT', signal: controller.signal, body: JSON.stringify({ model_id: model, ...(voice ? { voice: selectedVoice } : {}) }) });
        if (!controller.signal.aborted) { savedGeneration.current++; accept(result); setMessage('Saved. New requests use these settings; no VM restart is needed.'); }
      } else {
        const tested = editor.current.saved;
        const result = await api<{ test?: { status: string; model_id: string; voice?: string } }>(`${endpoint}/test`, { method: 'POST', signal: controller.signal });
        if (controller.signal.aborted) return;
        const current = await api<OciStatus>(endpoint, { signal: controller.signal });
        if (!controller.signal.aborted) {
          savedGeneration.current++; refreshSaved(current);
          if (result.test?.model_id !== current.model_id || tested?.model_id !== current.model_id || voice && (result.test.voice !== current.voice || tested?.voice !== current.voice)) {
            throw new Error('The saved OCI selection changed. Review the refreshed settings and test again.');
          }
          if (result.test?.status !== 'success') throw new Error('The inference test did not pass.');
          setSaved(previous => previous ? { ...previous, last_test: result.test } : previous); setMessage('Test passed.');
        }
      }
    } catch (reason) { if (!controller.signal.aborted) setError(godsEyeViewError(reason)); }
    finally { if (!controller.signal.aborted) setBusy(''); if (request.current === controller) request.current = null; }
  }
  return <form className="gods-eye-view-source" onSubmit={(event: FormEvent) => { event.preventDefault(); if (dirty && !busy && !loading) setConfirm(true); }} aria-label={title}>
    <div className="gods-eye-view-source-heading"><h3>{title}</h3><div className="gods-eye-view-source-badges">{saved && <span className={`gods-eye-view-mode ${saved.configured ? 'real' : ''}`}>{saved.configured ? 'Configured' : 'Not configured'}</span>}</div></div>
    <p className="gods-eye-view-provider-description">{voice ? 'Interprets spoken requests, controls the globe and generates spoken replies.' : 'Selects the model for the direct OCI text assistant.'}</p>
    {!saved && !error && <LoadingIndicator label="Loading settings…" />}
    <fieldset disabled={!!busy || loading}>{saved && <><div className="gods-eye-view-fields"><label>Model<select required value={model} onChange={event => { setModel(event.target.value); setMessage(''); setError(''); }}>
      {!models.some(item => item.id === model) && <option value={model}>{(model === saved.model_id ? saved.model_name : model) || 'Select a model'}</option>}
      {models.map(item => <option key={item.id} value={item.id} disabled={!item.selectable}>{item.name}{item.reason ? ` · ${item.reason}` : ''}</option>)}</select></label>
      {voice ? <label>Voice<select value={selectedVoice} onChange={event => { setSelectedVoice(event.target.value); setMessage(''); setError(''); }}>{(saved.voices || [saved.voice || 'ara']).map(value => <option key={value}>{value}</option>)}</select></label> : <label>Region<input value={saved.region} readOnly /></label>}</div>
      {voice && <small>Region: {saved.region} · Speech model: {saved.tts_model}</small>}</>}
      <div className="gods-eye-view-parameter-fields-help"><a className="gods-eye-view-key-help" href="https://docs.oracle.com/en-us/iaas/Content/generative-ai/getting-started.htm" target="_blank" rel="noopener noreferrer" aria-label={`Get key information for ${title}`}>GET KEY ↗</a></div>
      {saved && <><div className="gods-eye-view-source-actions"><button type="submit" disabled={!dirty || !model || !saved.available}>Save</button><button type="button" className="secondary" disabled={dirty || !saved.configured} onClick={() => void action('test')}>{busy === 'test' ? 'Testing…' : 'Test'}</button>
        <button type="button" className="secondary" disabled={!saved.available} onClick={() => void action('models')}>{busy === 'models' ? <LoadingIndicator label="Loading models…" inline /> : cursor ? 'More models' : 'Load models'}</button>
        {dirty && <button type="button" className="secondary" onClick={() => accept(saved)}>Discard changes</button>}</div>
      <small>{dirty ? 'Save before testing. ' : ''}{voice ? 'Test makes speech and audio inference requests using the saved model and voice.' : 'Test makes an inference request using the saved model.'}</small>
      {!dirty && !message && !error && <small>Last inference test: {saved.last_test?.status === 'success' ? 'Passed' : saved.last_test?.error?.message || (saved.last_test ? 'Failed' : 'Not tested')}</small>}
      {!saved.available && <p role="status">OCI credentials must be configured on the server.</p>}</>}
    </fieldset>
    {message && <p role="status" className="gods-eye-view-success">{message}</p>}{error && <p role="alert" className="gods-eye-view-error">{error}</p>}
    {!saved && error && <button type="button" className="secondary" disabled={!!busy || loading} onClick={() => setRetry(value => value + 1)}>Retry settings</button>}
    {confirm && <SettingsConfirmation title={`Save ${title}?`} changes={[...(model !== saved?.model_id ? [`Model: ${models.find(item => item.id === model)?.name || model}`] : []), ...(voice && selectedVoice !== saved?.voice ? [`Voice: ${selectedVoice}`] : [])]} onCancel={() => setConfirm(false)} onConfirm={() => void action('save')} />}
  </form>;
}
