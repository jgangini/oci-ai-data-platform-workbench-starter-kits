import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LoadingIndicator } from './LoadingIndicator';
import { SettingsConfirmation } from './SettingsConfirmation';

type ModuleStatus = {
  module_id: string; enabled: boolean; installed: boolean; status: string;
  operation_id?: string; stage?: string; resumable?: boolean; runtime: string; message: string;
};
const endpoint = '/api/admin/gods-eye-view/module';

export function GodsEyeViewModuleManager({ api, onClose, onChanged }: {
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  onClose: () => void; onChanged?: () => void;
}) {
  const [module, setModule] = useState<ModuleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [unverified, setUnverified] = useState(false);
  const [error, setError] = useState('');
  const [review, setReview] = useState<ModuleStatus | null>(null);
  const confirmation = useRef<ModuleStatus | null>(null);
  const current = useRef<ModuleStatus | null>(null);
  const request = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const callbacks = useRef({ onClose, onChanged });
  callbacks.current = { onClose, onChanged };
  const titleId = useId(), descriptionId = useId();

  function accept(next: ModuleStatus, deployed = false, message = '') {
    if (next.module_id !== 'gods_eye_view') throw new Error('Unexpected module response. Refresh its settings.');
    const ready = next.installed && next.enabled && next.status === 'ready';
    const changed = ready && (deployed || current.current?.status === 'activating');
    if (confirmation.current && (confirmation.current.status !== next.status || confirmation.current.operation_id !== next.operation_id
        || next.status === 'activating' && !next.resumable)) closeConfirmation();
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
      closeConfirmation();
      const message = reason instanceof Error ? reason.message : 'Unable to verify the module. Retry settings.';
      setError(message); setUnverified(true);
      // A lost POST response may already have started installation. Read before allowing another action.
      if (deploy) {
        try {
          const next = await api<ModuleStatus>(endpoint, { signal: controller.signal });
          if (!controller.signal.aborted) accept(next, true, message);
        } catch {
          if (!controller.signal.aborted) setError(`${message} Check the current status before retrying installation.`);
        }
      }
    } finally {
      if (request.current === controller) {
        request.current = null;
        if (!controller.signal.aborted) { setLoading(false); setBusy(false); }
      }
    }
  }

  function closeConfirmation() { confirmation.current = null; setReview(null); }
  function reviewInstallation() {
    if (request.current || unverified || !current.current) return;
    confirmation.current = current.current; setReview(current.current);
  }
  function install() {
    const approved = confirmation.current;
    closeConfirmation();
    if (!approved || request.current || approved.status !== current.current?.status || approved.operation_id !== current.current.operation_id
        || current.current.status === 'activating' && !current.current.resumable) return;
    void readOrDeploy(true);
  }

  useEffect(() => {
    const origin = document.activeElement, node = dialog.current;
    node?.showModal(); close.current?.focus(); void readOrDeploy();
    return () => {
      confirmation.current = null;
      request.current?.abort(); request.current = null; node?.close();
      if (origin instanceof HTMLElement && origin.isConnected) origin.focus();
    };
  }, []);
  useEffect(() => {
    if (module?.status !== 'activating') return;
    const timer = window.setInterval(() => { void readOrDeploy(); }, 5000);
    return () => window.clearInterval(timer);
  }, [module?.status]);

  const ready = module?.installed && module.enabled && module.status === 'ready';
  const activating = module?.status === 'activating';
  const progressing = busy || activating && !module?.resumable && !error;
  const label = ready ? 'Verify installation' : activating ? module.resumable ? 'Resume installation' : 'Installing module…'
    : module?.status === 'failed' ? 'Retry installation' : 'Install module';
  const phases: Record<string, string> = {
    plan: 'Planning installation…', infrastructure: 'Provisioning the private viewer…',
    aidp: 'Preparing AIDP resources…', verification: 'Verifying installation…',
  };
  return createPortal(<><dialog ref={dialog} className="lab-manager-modal governance-module-modal module-manager-dialog"
    aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy} tabIndex={-1}
    onCancel={event => { event.preventDefault(); callbacks.current.onClose(); }}>
    <header>
      <div><p className="eyebrow">Shared global module</p><h2 id={titleId}>God’s Eye View · Custom layers</h2>
        <p id={descriptionId}>One shared module for social networks, sensors and the AI Assistant.</p></div>
      {module && <span className={`lab-state ${ready ? 'installed' : 'unassigned'}`}>{module.status.replaceAll('_', ' ')}</span>}
    </header>
    <div className="governance-module-body">
      {loading ? <LoadingIndicator label="Loading module settings…" /> : <>
        {!progressing && <p className="governance-module-note">Install the private viewer and shared AIDP workflows, compute and AI Assistant for this application.</p>}
        {module?.runtime === 'local_fixture' && <p className="governance-module-note">Local simulation only. This does not deploy OCI or AIDP resources.</p>}
        {!progressing && module?.message && <p className="governance-module-note" role={module.status === 'failed' ? 'alert' : 'status'}>{module.message}</p>}
      </>}
      {error && <p className="lab-manager-error" role="alert">{error}</p>}
      {progressing && <div className="registration-result module-install-progress" role="status" aria-live="polite" aria-busy="true">
        <span className="progress-orbit" aria-hidden="true" />
        <p className="registration-progress-phase">{activating ? phases[module.stage || ''] || 'Installing module…'
          : ready ? 'Verifying installation…' : 'Starting installation…'}</p>
        <p className="registration-progress-detail">{activating ? module.message : 'Submitting the installation request…'}</p>
        <p className="registration-progress-detail">Closing this popup does not cancel installation. Reopen it to follow progress.</p>
      </div>}
    </div>
    <footer>
      <button ref={close} className="secondary" type="button" onClick={onClose}>Close</button>
      {unverified ? <button type="button" disabled={busy || loading} onClick={() => void readOrDeploy()}>Retry settings</button>
        : module && module.status !== 'deployment_required' && <button type="button" disabled={busy || loading || activating && !module.resumable}
          onClick={ready ? () => void readOrDeploy(true) : reviewInstallation}>{label}</button>}
    </footer>
  </dialog>
  {review && <SettingsConfirmation title={review.status === 'activating' ? 'Resume God’s Eye View installation?' : review.status === 'failed'
    ? 'Retry God’s Eye View installation?' : 'Install God’s Eye View?'}
    confirmLabel={review.status === 'activating' ? 'Resume installation' : review.status === 'failed' ? 'Retry installation' : 'Install module'}
    description={review.runtime === 'local_fixture' ? 'This enables the local simulation only. No OCI or AIDP resources will be created.'
      : 'This installs one shared module for this application. Review the resources before continuing.'}
    changes={[
      ...(review.runtime === 'local_fixture' ? [] : [
        'Provision a private viewer VM using the existing network and shared credentials.',
        'Prepare the social network and sensor streams with separate AIDP compute, Gold query compute and dedicated agent AI Compute.',
        'These resources use OCI capacity and incur costs while running.',
      ]),
      ...(review.status === 'activating' || review.status === 'failed' ? ['Continue the existing installation and reuse its managed resources without creating a duplicate viewer VM.'] : []),
      'Closing the progress popup does not cancel installation. You can reopen it to check progress.',
    ]} onCancel={closeConfirmation} onConfirm={install} />}
  </>, document.body);
}
