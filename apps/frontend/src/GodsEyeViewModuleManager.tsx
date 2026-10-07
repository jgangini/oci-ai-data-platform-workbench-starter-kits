import { useEffect, useId, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { LoadingIndicator } from './LoadingIndicator';
import { SearchableCombobox } from './SearchableCombobox';

export type ModuleStatus = {
  module_id: string; enabled: boolean; installed: boolean; status: string;
  operation_id?: string; stage?: string; resumable?: boolean; runtime: string; message: string;
  bundled_version?: string;
};
const endpoint = '/api/admin/gods-eye-view/module';
export const godsEyeServices = ['OCI Compute', 'Object Storage', 'AIDP Workbench', 'Master Catalog', 'Spark Compute', 'AI Compute'];
const phases: Record<string, string> = {
  plan: 'OCI Compute', infrastructure: 'OCI Compute', aidp: 'AIDP Workbench',
  controls: 'Object Storage', volumes: 'Master Catalog', computes: 'Spark Compute · AI Compute',
  workflows: 'AIDP Workbench', agent: 'AI Compute', streaming: 'Spark Compute',
  publication: 'Object Storage', verification: 'Verifying services…',
};

export function GodsEyeViewModuleManager({ api, onClose, onChanged }: {
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  onClose: () => void; onChanged?: () => void;
}) {
  const [module, setModule] = useState<ModuleStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [unverified, setUnverified] = useState(false);
  const [administrators, setAdministrators] = useState<{ id: string; email: string; is_aidp_admin: boolean }[]>([]);
  const currentAdministrators = useRef<typeof administrators>([]);
  const [administratorsLoaded, setAdministratorsLoaded] = useState(false);
  const [administratorsError, setAdministratorsError] = useState('');
  const [administratorId, setAdministratorId] = useState('');
  const selectedAdministrator = useRef('');
  const administratorsRequest = useRef<AbortController | null>(null);
  const [error, setError] = useState('');
  const current = useRef<ModuleStatus | null>(null);
  const request = useRef<AbortController | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const close = useRef<HTMLButtonElement>(null);
  const callbacks = useRef({ onClose, onChanged });
  callbacks.current = { onClose, onChanged };
  const titleId = useId(), descriptionId = useId();

  function accept(next: ModuleStatus, deployed = false, message = '') {
    const flags = [next?.installed, next?.enabled];
    if (!next || next.module_id !== 'gods_eye_view' || !flags.every(value => typeof value === 'boolean')
      || !['available', 'deployment_required', 'activating', 'failed', 'ready'].includes(next.status)
      || next.status === 'ready' && !flags.every(Boolean)
      || typeof next.message !== 'string' || !['aidp', 'local_fixture'].includes(next.runtime))
      throw new Error('Unexpected module response. Refresh its settings.');
    const ready = next.status === 'ready';
    const changed = ready && (deployed || current.current?.status === 'activating');
    current.current = next; setModule(next); setUnverified(false); setError(ready ? '' : message);
    if (changed) callbacks.current.onChanged?.();
  }

  async function readOrDeploy(deploy = false) {
    if (request.current) return;
    if (deploy) {
      if (!current.current || !selectedAdministrator.current || !currentAdministrators.current.some(user => user.id === selectedAdministrator.current)) return;
      if (!['available', 'ready', 'failed', ...(current.current.resumable === true ? ['activating'] : [])].includes(current.current.status)) return;
    }
    const controller = new AbortController(); request.current = controller;
    setBusy(deploy); setLoading(!current.current);
    try {
      const next = await api<ModuleStatus>(deploy ? `/api/admin/users/${encodeURIComponent(selectedAdministrator.current)}/modules/gods_eye_view` : endpoint, {
        ...(deploy ? { method: 'POST' } : {}), signal: controller.signal,
      });
      controller.signal.throwIfAborted(); accept(next, deploy);
    } catch (reason) {
      if (controller.signal.aborted) return;
      current.current = null;
      const message = reason instanceof Error ? reason.message : 'Unable to verify the module. Retry settings.';
      setError(message); setUnverified(true);
      // A lost POST response may already have started installation. Read before allowing another action.
      if (deploy) {
        try {
          const next = await api<ModuleStatus>(endpoint, { signal: controller.signal });
          controller.signal.throwIfAborted(); accept(next, true, message);
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

  useEffect(() => {
    const origin = document.activeElement, node = dialog.current;
    void loadAdministrators();
    node?.showModal(); close.current?.focus(); void readOrDeploy();
    return () => {
      current.current = null; selectedAdministrator.current = ''; currentAdministrators.current = [];
      administratorsRequest.current?.abort();
      request.current?.abort(); request.current = null; node?.close();
      if (origin instanceof HTMLElement && origin.isConnected) origin.focus();
    };
  }, []);
  async function loadAdministrators() {
    administratorsRequest.current?.abort();
    const controller = new AbortController(); administratorsRequest.current = controller;
    selectedAdministrator.current = ''; setAdministratorId(''); currentAdministrators.current = []; setAdministrators([]);
    setAdministratorsError(''); setAdministratorsLoaded(false);
    try {
      const result = await api<{ users: typeof administrators }>('/api/admin/users', { signal: controller.signal });
      if (!controller.signal.aborted) {
        currentAdministrators.current = result.users.filter(user => user.is_aidp_admin === true);
        setAdministrators(currentAdministrators.current);
      }
    } catch (reason) {
      if (!controller.signal.aborted) setAdministratorsError(reason instanceof Error ? reason.message : 'Unable to load administrators.');
    } finally {
      if (!controller.signal.aborted) setAdministratorsLoaded(true);
    }
  }
  useEffect(() => {
    if (module?.status !== 'activating') return;
    const timer = window.setInterval(() => { void readOrDeploy(); }, 5000);
    return () => window.clearInterval(timer);
  }, [module?.status]);

  const ready = module?.status === 'ready';
  const activating = module?.status === 'activating';
  const progressing = busy || activating && module?.resumable !== true && !error;
  const label = ready ? 'Verify' : activating ? module.resumable === true ? 'Resume deployment' : 'Deploying…'
    : module?.status === 'failed' ? 'Retry deployment' : 'Deploy';
  const statusLabel = module?.status === 'available' ? 'Not installed' : ready ? 'Installed' : module?.status.replaceAll('_', ' ');
  const phaseLabel = activating ? phases[module.stage || ''] || 'Installing module…' : ready ? 'Verifying installation…' : 'Starting installation…';
  const phaseId = useId();
  return createPortal(<dialog ref={dialog} className="lab-manager-modal governance-module-modal module-manager-dialog"
    aria-labelledby={titleId} aria-describedby={descriptionId} aria-busy={busy} tabIndex={-1}
    onCancel={event => { event.preventDefault(); callbacks.current.onClose(); }}>
    <header>
      <div><p className="eyebrow">Shared global module</p><h2 id={titleId}>God’s Eye View · Custom layers</h2>
        <p id={descriptionId}>One shared module for social networks, sensors and the AI Assistant.</p></div>
      {module && <span className={`lab-state ${ready ? 'installed' : 'unassigned'}`}>{statusLabel}</span>}
    </header>
    <div className="governance-module-body">
      {!progressing && (loading || !administratorsLoaded ? <LoadingIndicator label="Loading module settings…" /> : <>
        <SearchableCombobox label="AI Data Platform administrator" placeholder="Select an administrator"
          value={administratorId} disabled={busy || activating && module?.resumable !== true}
          options={administrators.map(user => ({ value: user.id, label: user.email }))}
          onChange={value => { selectedAdministrator.current = value; setAdministratorId(value); }} />
        {!administratorsError && !administrators.length && <p role="alert" className="lab-manager-error">No AI Data Platform administrator is available.</p>}
        <p className="governance-module-note">Deploy the private viewer, shared AI Assistant and independent social network and sensor streams.</p>
        <ul className="module-service-tags" aria-label="Services">
          {godsEyeServices.map(service => <li className="badge inactive" key={service}>{service}</li>)}
        </ul>
        {module?.runtime === 'local_fixture' && <p className="governance-module-note">Local simulation only. This does not deploy OCI or AIDP resources.</p>}
        {module?.status !== 'available' && module?.message && <p className="governance-module-note" role={module.status === 'failed' ? 'alert' : 'status'}>{module.message}</p>}
      </>)}
      {error && <p className="lab-manager-error" role="alert">{error}</p>}
      {administratorsError && <p className="lab-manager-error" role="alert">{administratorsError}</p>}
      {progressing && <div className="registration-result module-install-progress" role="status" aria-live="polite" aria-busy="true">
        <span className="progress-orbit" aria-hidden="true" />
        <p className="eyebrow">Deployment progress</p>
        <p className="registration-progress-phase" id={phaseId}>{phaseLabel}</p>
        <div className="registration-progress-track gods-eye-view-reset-track" role="progressbar" aria-labelledby={phaseId} aria-valuetext="Deployment in progress">
          <span />
        </div>
        <p className="registration-progress-detail">{activating ? module.message : 'Submitting the installation request…'}</p>
        <p className="registration-progress-detail">Installation continues in the background when you close this window. Reopen it to follow progress.</p>
      </div>}
    </div>
    <footer>
      <button ref={close} className="secondary" type="button" onClick={onClose}>Close</button>
      {unverified || administratorsError ? <button type="button" disabled={busy || loading} onClick={() => { void readOrDeploy(); void loadAdministrators(); }}>Retry settings</button>
        : module && module.status !== 'deployment_required' && <button type="button" disabled={busy || loading || !administratorsLoaded || !administrators.some(user => user.id === administratorId && user.is_aidp_admin === true) || activating && module.resumable !== true}
          onClick={() => void readOrDeploy(true)}>{label}</button>}
    </footer>
  </dialog>, document.body);
}
