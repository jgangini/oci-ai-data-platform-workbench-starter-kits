import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { territorialEndpoint, territorialError, type TerritorialApi } from './territorialAdminState';

export type SyntheticReset = { operation_id?: string; sensor_type?: string; status?: 'pending' | 'completed' | 'error'; stage?: string; error?: string; counts?: Record<string, number>; replacements?: Record<string, string> };

const trash = <svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M5 7h14m-9 4v6m4-6v6M9 7l.7-3h4.6l.7 3m-8.2 0 .7 13h9.2l.7-13" /></svg>;

export function SyntheticDataResetStatus({ state, error, runtime, sensorLabel }: { state: SyntheticReset; error: string; runtime: string; sensorLabel?: string }) {
  if ((!state.status || state.status === 'completed') && !error) return null;
  const local = runtime === 'local_fixture', aidp = runtime === 'aidp';
  const failed = state.status === 'error' || !!error;
  const subject = sensorLabel || 'Synthetic';
  const stages: Record<string, string> = sensorLabel ? {
    preparing: `Preparing the ${subject} cleanup…`, waiting_for_aidp: 'Waiting for the AIDP cleanup job…',
    draining: 'Waiting for active captures to finish…', landing: `Deleting ${subject} generated files…`,
    delta: `Deleting ${subject} readings from Delta tables…`, database: `Deleting ${subject} stored readings…`,
    publish: 'Publishing the cleaned sensor readings…', publishing: 'Publishing the cleaned sensor readings…', history: `Cleaning ${subject} reading history…`,
  } : aidp ? {
    preparing: 'Preparing the Synthetic cleanup…', waiting_for_aidp: 'Waiting for the AIDP cleanup job…',
    draining: 'Waiting for active captures to finish…', landing: 'Deleting Synthetic Landing files…',
    delta: 'Deleting Synthetic rows from Delta tables…', database: 'Deleting Synthetic records from Autonomous Database…',
    publishing: 'Publishing the cleaned events and publications…', history: 'Cleaning Synthetic publication history…',
  } : local ? {
    preparing: 'Preparing the local Synthetic cleanup…', landing: 'Deleting local Synthetic files…',
    publish: 'Rebuilding local events and publications…', publishing: 'Rebuilding local events and publications…',
  } : {};
  const title = state.status === 'completed' ? 'Unable to verify the reset.'
    : state.status === 'error' ? `${subject} reset is incomplete.` : !state.status && error ? `${subject} reset could not be started.` : `Resetting ${subject} data…`;
  return <div className="registration-result territorial-reset-progress" role={failed ? 'alert' : 'status'} aria-live="polite">
    {state.status === 'pending' && !failed && <span className="progress-orbit" aria-hidden="true" />}
    <strong>{title}</strong>
    {state.status === 'pending' && <span className="sr-only">{subject} reset progress</span>}
    {state.status === 'pending' && <p>{stages[state.stage || ''] || (aidp ? 'Waiting for the AIDP cleanup to finish…' : local ? `Deleting local ${subject} data…` : 'Waiting for cleanup to finish…')}</p>}
    {state.stage === 'history' && state.replacements && <p>Historical publications rebuilt: {Object.keys(state.replacements).length}.</p>}
    {local && <p>Local records and generated files only. No AIDP job or Delta tables are involved.</p>}
    {aidp && <p>Cleanup includes Delta tables, Autonomous Database and Object Storage. Completion is confirmed by the AIDP job.</p>}
    {state.status === 'error' && <p>{state.error || `Use Retry ${subject} reset to continue.`}</p>}
    {error && <p>{error}</p>}
    {['pending', 'error'].includes(state.status || '') && <p>Capture controls stay paused until cleanup finishes. If interrupted, Retry {subject} reset resumes the same request.</p>}
  </div>;
}

export function SyntheticDataReset({ api, status, runtime, onChange, onComplete, sensor, disabled = false }: {
  api: TerritorialApi; status?: SyntheticReset; runtime: string; onChange: (state: SyntheticReset, error: string) => void; onComplete: () => void;
  sensor?: { type: string; label: string }; disabled?: boolean;
}) {
  const [operation, setOperation] = useState<SyntheticReset>({});
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const current = useRef<SyntheticReset>({});
  const request = useRef<AbortController | null>(null);
  const submitting = useRef(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  const callbacks = useRef({ onChange, onComplete });
  callbacks.current = { onChange, onComplete };
  const endpoint = sensor ? `${territorialEndpoint}/sensors/${encodeURIComponent(sensor.type)}/reset` : `${territorialEndpoint}/synthetic/reset`;
  const subject = sensor?.label || 'Synthetic';
  const progressOpen = ['pending', 'error'].includes(operation.status || '') || !!error;
  const visible = open || progressOpen;

  function accept(next: SyntheticReset, error = '') {
    if (next.operation_id && (next.sensor_type || undefined) !== sensor?.type) {
      const message = 'Another data cleanup is active. Wait until it finishes before retrying.';
      setError(message); callbacks.current.onChange(current.current, message);
      return;
    }
    const completed = next.status === 'completed' && (current.current.operation_id !== next.operation_id || current.current.status !== 'completed');
    if (next.status === 'completed' || next.status === 'pending' && current.current.operation_id !== next.operation_id) setOpen(false);
    current.current = next; setOperation(next); setError(error); callbacks.current.onChange(next, error);
    if (completed) callbacks.current.onComplete();
  }
  function verified(next: SyntheticReset, error = '') {
    if (next.operation_id === current.current.operation_id) accept(next);
    else accept({ ...current.current, status: 'error', error: 'The reset could not be confirmed. Retry will resume the same request.' }, error);
  }
  useEffect(() => {
    // A completed configuration read can resolve a stalled status request, but never another operation or scope.
    if (status?.status === 'completed' && status.operation_id === current.current.operation_id &&
        (status.sensor_type || undefined) === sensor?.type && ['pending', 'error'].includes(current.current.status || '')) {
      request.current?.abort(); request.current = null; submitting.current = false; setBusy(false); accept(status); return;
    }
    // A local request owns its status until verified; older source polls cannot undo it.
    if (!status?.operation_id || request.current || status.status === 'completed') return;
    if (!current.current.operation_id) accept(status);
    else if (current.current.status === 'completed' && current.current.operation_id !== status.operation_id) void checkStatus();
  }, [status]);
  useEffect(() => () => request.current?.abort(), []);
  useEffect(() => {
    if (!visible) return;
    const origin = document.activeElement, node = dialog.current;
    node?.showModal(); if (open) cancel.current?.focus(); else node?.focus();
    return () => { node?.close(); if (origin instanceof HTMLElement && origin.isConnected) origin.focus(); };
  }, [visible, open]);

  async function checkStatus() {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    const discovering = current.current.status === 'completed';
    try {
      const next = await api<SyntheticReset>(endpoint, { signal: controller.signal });
      if (!controller.signal.aborted) { if (discovering) accept(next); else verified(next); }
    } catch (reason) {
      if (!controller.signal.aborted) {
        const message = `Unable to verify the reset: ${territorialError(reason)} Status will be checked again.`;
        setError(message); callbacks.current.onChange(current.current, message);
      }
    } finally { if (request.current === controller) request.current = null; }
  }
  useEffect(() => {
    if (!['pending', 'error'].includes(operation.status || '')) return;
    const timer = window.setInterval(() => { void checkStatus(); }, 5000);
    return () => window.clearInterval(timer);
  }, [operation.status, operation.operation_id]);

  async function recoverReset(reason: unknown, controller: AbortController) {
    const code = reason && typeof reason === 'object' && 'status' in reason ? reason.status : undefined;
    if (code === 501 || code === 422) { accept({}, territorialError(reason)); return; }
    // The server may have accepted a POST whose response was lost. Recover before allowing another operation.
    try {
      const next = await api<SyntheticReset>(endpoint, { signal: controller.signal });
      if (controller.signal.aborted) return;
      if (code === 409 && next.operation_id && ['pending', 'error'].includes(next.status || '')) accept(next);
      else verified(next, territorialError(reason));
    } catch {
      if (!controller.signal.aborted) {
        const message = 'The reset response was interrupted. Checking its status before another reset can start.';
        setError(message); callbacks.current.onChange(current.current, message);
      }
    }
  }

  async function reset() {
    if (disabled || submitting.current) return;
    submitting.current = true; setBusy(true); request.current?.abort();
    const controller = new AbortController(); request.current = controller;
    const operation_id = ['pending', 'error'].includes(current.current.status || '') && current.current.operation_id ? current.current.operation_id : crypto.randomUUID();
    accept({ operation_id, ...(sensor ? { sensor_type: sensor.type } : {}), status: 'pending', stage: 'preparing' }); setOpen(false);
    try {
      const next = await api<SyntheticReset>(endpoint, {
        method: 'POST', signal: controller.signal, body: JSON.stringify({ operation_id, confirm: true }),
      });
      if (!controller.signal.aborted) verified(next);
    } catch (reason) {
      if (!controller.signal.aborted) await recoverReset(reason, controller);
    } finally { if (request.current === controller) { submitting.current = false; setBusy(false); request.current = null; } }
  }
  const retry = ['pending', 'error'].includes(operation.status || '');
  const label = retry ? `Retry ${subject} reset` : `Delete ${subject} data`;
  return <>
    <button type="button" className="table-action table-delete territorial-toolbar-button" aria-label={label} title={label} disabled={disabled || busy} onClick={() => setOpen(true)}>{trash}</button>
    {visible && createPortal(<dialog ref={dialog} className={`${open ? 'confirm-modal confirm-delete' : 'territorial-reset-popup'} territorial-reset-dialog`} aria-label={open ? undefined : `${subject} reset`} aria-labelledby={open ? 'territorial-reset-title' : undefined} aria-describedby={open ? 'territorial-reset-description' : undefined} tabIndex={-1}
      onCancel={event => { event.preventDefault(); if (open) setOpen(false); else if (!operation.status) setError(''); }}>
      {open ? <><div className="confirm-content"><div className="confirm-icon">{trash}</div><h2 id="territorial-reset-title">{retry ? `Retry ${subject} reset?` : sensor ? `Delete ${subject} data?` : 'Delete all Synthetic data?'}</h2>
        <p id="territorial-reset-description">{sensor ? `This pauses ${subject} capture and permanently deletes its readings, generated files and reading history. Other sensor types, social network data, saved sensor locations and sensor configuration are kept.` : <>{runtime === 'local_fixture'
          ? 'This stops Synthetic capture and permanently deletes its local publications, events and generated files across all networks. No AIDP job or Delta tables are involved.'
          : runtime === 'aidp' ? 'This stops Synthetic capture and runs an AIDP cleanup for Synthetic publications, events and generated data across all networks, including Delta tables, Autonomous Database and Object Storage.'
          : 'This stops Synthetic capture and permanently deletes its publications, events and generated data across all networks.'} Real data, source configuration and credentials are kept.</>}</p>
        {retry && <p>This resumes the same reset request if it was interrupted.</p>}
        <p>After the reset, choose Run now to start a new demonstration.</p></div>
      <footer><button ref={cancel} type="button" onClick={() => setOpen(false)} autoFocus>Cancel</button><button className="confirm-primary" type="button" disabled={disabled || busy} onClick={() => void reset()}>{retry ? 'Retry reset' : `Delete ${subject} data`}</button></footer></>
      : <><SyntheticDataResetStatus state={operation} error={error} runtime={runtime} sensorLabel={sensor?.label} />
        <div className="territorial-reset-popup-actions">{retry ? <button type="button" disabled={disabled || busy} onClick={() => void reset()}>Retry {subject} reset</button>
          : <button type="button" onClick={() => setError('')}>Close</button>}</div></>}
    </dialog>, document.body)}
  </>;
}
