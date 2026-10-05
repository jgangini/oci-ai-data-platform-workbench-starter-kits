import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LoadingIndicator } from './LoadingIndicator';

type ModuleStatus = {
  module_id: string; enabled: boolean; installed: boolean; status: string;
  operation_id?: string; runtime: string; message: string;
};
const endpoint = '/api/admin/territorial/module';

export function GodsEyeModuleManager({ api, onClose, onChanged }: {
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  onClose: () => void; onChanged?: () => void;
}) {
  const [module, setModule] = useState<ModuleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [unverified, setUnverified] = useState(false);
  const [error, setError] = useState('');
  const current = useRef<ModuleStatus | null>(null);
  const request = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const callbacks = useRef({ onClose, onChanged });
  callbacks.current = { onClose, onChanged };
  const titleId = useId(), descriptionId = useId();

  function accept(next: ModuleStatus, deployed = false, message = '') {
    if (next.module_id !== 'territorial_control') throw new Error('Unexpected module response. Refresh its settings.');
    const ready = next.enabled && next.status === 'ready';
    const changed = ready && (deployed || current.current?.status === 'activating');
    current.current = next; setModule(next); setUnverified(false); setError(ready ? '' : message);
    if (changed) callbacks.current.onChanged?.();
  }

  async function readOrDeploy(deploy = false) {
    if (request.current) return;
    const controller = new AbortController(); request.current = controller;
    if (deploy) setBusy(true);
    if (!current.current) setLoading(true);
    try {
      const next = await api<ModuleStatus>(endpoint + (deploy ? '/deploy' : ''), {
        ...(deploy ? { method: 'POST' } : {}), signal: controller.signal,
      });
      if (!controller.signal.aborted) accept(next, deploy);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const message = reason instanceof Error ? reason.message : 'Unable to verify the module. Retry settings.';
      setError(message); setUnverified(true);
      // A lost POST response may already have activated the module. Read before allowing another action.
      if (deploy) {
        try {
          const next = await api<ModuleStatus>(endpoint, { signal: controller.signal });
          if (!controller.signal.aborted) accept(next, true, message);
        } catch {
          if (!controller.signal.aborted) setError(`${message} Check the current status before retrying deployment.`);
        }
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) { setLoading(false); setBusy(false); }
      }
    }
  }

  useEffect(() => {
    const origin = document.activeElement, node = dialog.current;
    node?.showModal(); close.current?.focus(); void readOrDeploy();
    return () => {
      request.current?.abort(); request.current = null; node?.close();
      if (origin instanceof HTMLElement && origin.isConnected) origin.focus();
    };
  }, []);
  useEffect(() => {
    if (module?.status !== 'activating') return;
    const timer = window.setInterval(() => { void readOrDeploy(); }, 5000);
    return () => window.clearInterval(timer);
  }, [module?.status]);

  const ready = module?.enabled && module.status === 'ready';
  const label = ready ? 'Verify installation' : module?.status === 'activating' ? 'Resume activation'
    : module?.status === 'failed' ? 'Retry deployment' : 'Deploy';
  return createPortal(<dialog ref={dialog} className="lab-manager-modal governance-module-modal module-manager-dialog"
    aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy} tabIndex={-1}
    onCancel={event => { event.preventDefault(); if (!busy) callbacks.current.onClose(); }}>
    <header>
      <div><p className="eyebrow">Shared global module</p><h2 id={titleId}>God’s Eye View · Custom layers</h2>
        <p id={descriptionId}>One shared module for social networks, sensors and the AI Assistant.</p></div>
      {module && <span className={`lab-state ${ready ? 'installed' : 'unassigned'}`}>{module.status.replaceAll('_', ' ')}</span>}
    </header>
    <div className="governance-module-body">
      {loading ? <LoadingIndicator label="Loading module settings…" /> : <>
        <p className="governance-module-note">Activate the infrastructure provisioned for this application and verify its workflows, publication, viewer and agent.</p>
        {module?.runtime === 'local_fixture' && <p className="governance-module-note">Local simulation only. This does not deploy OCI or AIDP resources.</p>}
        {module?.message && <p className="governance-module-note" role={module.status === 'failed' ? 'alert' : 'status'}>{module.message}</p>}
      </>}
      {error && <p className="lab-manager-error" role="alert">{error}</p>}
      {(busy || module?.status === 'activating' && !error) && <div className="governance-module-progress">
        <LoadingIndicator label={busy ? 'Verifying module activation…' : 'Waiting for module activation…'} inline />
        <span>{busy ? 'Verifying module activation…' : 'Checking the existing activation. You can close and reopen this popup to follow its progress.'}</span>
      </div>}
    </div>
    <footer>
      <button ref={close} className="secondary" type="button" disabled={busy} onClick={onClose}>Close</button>
      {unverified ? <button type="button" disabled={busy || loading} onClick={() => void readOrDeploy()}>Retry settings</button>
        : module && module.status !== 'deployment_required' && <button type="button" disabled={busy || loading} onClick={() => void readOrDeploy(true)}>{label}</button>}
    </footer>
  </dialog>, document.body);
}
