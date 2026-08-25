import {
  FormEvent,
  KeyboardEvent,
  ReactNode,
  RefObject,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { createPortal } from "react-dom";

import { labAssignmentChanges } from "./labAssignments";

import {
  ApiRequestError,
  getOrCreateModuleOperation,
  getOrCreateLabOperation,
  loadModuleOperation,
  loadLabOperation,
  moduleOperationKind,
  parseRetryAfter,
  persistModuleOperation,
  persistLabOperation,
  pollRegistration,
  registrationProgress,
  type RegistrationPhase,
  type RegistrationPhaseValue,
  type RegistrationResponse,
  type ModuleOperation,
  type ModuleOperationKind,
} from "./registrationPoll";

type ApiError = { detail?: string };
type LabUser = {
  id: string;
  name: string;
  email: string;
  status: "active" | "pending";
  labs: AssignedLab[];
  active: boolean;
  managed?: boolean;
  is_aidp_admin: boolean;
  participant_code?: number | null;
};

type AssignedLab = {
  lab_id: string;
  pack_version: string;
  bundled_version?: string | null;
  update_available?: boolean;
  phase: string;
  workspace_path: string;
  job_name: string;
};
type CatalogLab = {
  lab_id: string;
  display_name: string;
  description?: string;
  pack_version: string;
  status: "available" | "planned";
  available: boolean;
};
type UserDraft = { name: string; email: string; lab_ids: string[] };
type AdminSettingsResponse = {
  aidp_service_endpoint: string;
  aidp_url: string;
  aidp_platform_id: string;
  deployment_mode: "laboratory" | "production";
  operator_username: string;
  registration_code_configured: boolean;
};
type AdminModule = {
  module_id: "ai_data_governance_vsc_extension" | (string & {});
  display_name: string;
  status: "not_installed" | "installing" | "active" | "redeploying" | "deleting" | "error" | (string & {});
  installed: boolean;
  installed_version?: string | null;
  bundled_version?: string | null;
  update_available?: boolean;
  operation_id?: string | null;
  operation_type?: ModuleOperationKind | null;
  message?: string | null;
  enabled: boolean;
};
type AdminModuleOperationResponse = {
  status: AdminModule["status"];
  phase?: RegistrationPhaseValue;
  operation_id: string;
  message?: string;
};
type PublicConfig = {
  deployment_mode: "laboratory" | "production";
  labs: CatalogLab[];
};
type AdminSession = { username: string; operator_username: string };
type ApplicationReleaseOperation = {
  operation_id: string;
  status: "queued" | "checking" | "downloading" | "building" | "validating" | "activating" | "succeeded" | "up_to_date" | "failed" | (string & {});
  phase: RegistrationPhaseValue;
  message: string;
  target_release?: string;
};
type ApplicationReleasePackage = {
  package_id: string;
  display_name: string;
  bundled_version: string;
  kind: string;
  scope: "participant" | "global";
  status: string;
};
type AdminApplicationRelease = {
  repository: string;
  current_release: string;
  current_commit_sha: string;
  latest_release: string | null;
  latest_published_at: string | null;
  latest_release_url: string | null;
  latest_release_immutable: boolean;
  update_available: boolean;
  updater_available: boolean;
  update_check_error: string;
  operation: ApplicationReleaseOperation | null;
  packages: ApplicationReleasePackage[];
};
const fallbackCatalog: CatalogLab[] = [
  { lab_id: "banking", display_name: "Banking", description: "Explore customer accounts, branches and transactions through a governed medallion pipeline.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "telecommunications", display_name: "Telecommunications", description: "Analyze subscribers, plans, network sites and usage events for service and network insights.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "telco_lineage", display_name: "Telco Customer 360 Lineage", description: "Test end-to-end data lineage for prepaid, postpaid and home services, from Landing through Gold with entity and column relationships.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "retail", display_name: "Retail", description: "Transform customers, products, orders and order items into sales and customer analytics.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "healthcare", display_name: "Healthcare", description: "Prepare patients, providers, appointments and encounters for operational healthcare analysis.", pack_version: "2.0.0", status: "available", available: true },
];

function participantLabCatalog(catalog: CatalogLab[]) {
  return catalog.filter(({ lab_id }) => !["agent", "ai_data_governance_vsc_extension"].includes(lab_id));
}

function moduleOperationKey(moduleId: string, kind: ModuleOperationKind) {
  return `${moduleId}:${kind}`;
}

function readStoredModuleOperation(moduleId: string, kind: ModuleOperationKind) {
  try {
    return loadModuleOperation(window.localStorage, moduleId, kind);
  } catch {
    return undefined;
  }
}

function writeStoredModuleOperation(
  moduleId: string,
  kind: ModuleOperationKind,
  operation?: ModuleOperation,
) {
  try {
    persistModuleOperation(window.localStorage, moduleId, kind, operation);
  } catch {
    // The server manifest and the in-memory copy remain authoritative.
  }
}

function labLabel(catalog: CatalogLab[], labId: string) {
  return catalog.find(({ lab_id }) => lab_id === labId)?.display_name ?? labId;
}

function labDescription(lab: CatalogLab) {
  return lab.description || "Description unavailable.";
}

function labPhaseLabel(phase: string) {
  return phase ? `${phase[0].toUpperCase()}${phase.slice(1)}` : "Pending";
}

const registrationPhaseLabels: Record<RegistrationPhase, string> = {
  identity: "Identity account",
  workspace: "Workspace",
  database: "Governed database",
  schemas: "Shared schemas",
  content: "Lab content",
  permissions: "Permissions",
};
function registrationPhaseLabel(phase?: RegistrationPhaseValue) {
  if (phase === "cleanup") return "Cleaning AIDP environment";
  return phase && Object.hasOwn(registrationPhaseLabels, phase)
    ? registrationPhaseLabels[phase as RegistrationPhase]
    : "Reconciling OCI access";
}

const focusableSelector = [
  "a[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

function useDialogFocus<Panel extends HTMLElement, Initial extends HTMLElement>(
  open: boolean,
  onClose: () => void,
  panelRef: RefObject<Panel | null>,
  initialFocusRef: RefObject<Initial | null>,
) {
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  useEffect(() => {
    if (!open) return undefined;
    previousFocusRef.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    initialFocusRef.current?.focus();
    const onKeyDown = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== "Tab") return;
      const focusable = Array.from(
        panelRef.current?.querySelectorAll<HTMLElement>(focusableSelector) ?? [],
      );
      if (!focusable.length) {
        event.preventDefault();
        panelRef.current?.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    const previousOverflow = document.body.style.overflow;
    const appRoot = document.getElementById("root");
    const previousInert = appRoot?.inert;
    if (appRoot) appRoot.inert = true;
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (appRoot) appRoot.inert = previousInert ?? false;
      previousFocusRef.current?.focus();
    };
  }, [initialFocusRef, open, panelRef]);
}

function ConfirmModal({
  open,
  kind,
  title,
  description,
  children,
  error,
  confirmLabel,
  onClose,
  onConfirm,
}: {
  open: boolean;
  kind: "question" | "delete" | "reset";
  title: string;
  description: string;
  children?: ReactNode;
  error?: string;
  confirmLabel: string;
  onClose: () => void;
  onConfirm: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useDialogFocus(open, onClose, panelRef, closeRef);

  if (!open) return null;
  const icon =
    kind === "delete" ? (
      <TrashIcon />
    ) : kind === "reset" ? (
      <RefreshIcon />
    ) : (
      <svg
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.75"
        aria-hidden="true"
      >
        <path
          strokeLinecap="round"
          strokeLinejoin="round"
          d="M8.23 9c.55-1.17 2.03-2 3.77-2 2.21 0 4 1.34 4 3 0 1.4-1.28 2.58-3.01 2.91-.54.1-.99.54-.99 1.09m0 3h.01M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Z"
        />
      </svg>
    );
  return createPortal(
    <div className="confirm-overlay">
      <section
        className={`confirm-modal confirm-${kind}`}
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <div className="confirm-content">
          <div className="confirm-icon">{icon}</div>
          <h2 id={titleId}>{title}</h2>
          <p id={descriptionId}>{description}</p>
          {children}
          {error && (
            <p className="confirm-error" role="alert">
              {error}
            </p>
          )}
        </div>
        <footer>
          <button ref={closeRef} type="button" onClick={onClose}>
            Cancel
          </button>
          <button className="confirm-primary" type="button" onClick={onConfirm}>
            {confirmLabel}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}

function Toast({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!message) return undefined;
    const timeout = window.setTimeout(onDismiss, 4_000);
    return () => window.clearTimeout(timeout);
  }, [message, onDismiss]);

  if (!message) return null;
  return createPortal(
    <div className="toast" role="status" aria-live="polite">
      <span>{message}</span>
      <button
        className="toast-dismiss"
        type="button"
        onClick={onDismiss}
        aria-label="Dismiss notification"
      >
        ×
      </button>
    </div>,
    document.body,
  );
}

function ProvisioningOverlay({
  phase,
  message,
  label,
  indeterminate = false,
}: {
  phase?: RegistrationPhaseValue;
  message?: string;
  label?: string;
  indeterminate?: boolean;
}) {
  const phaseId = useId();
  const progress = registrationProgress(phase);
  const phaseLabel = label || registrationPhaseLabel(phase);
  return (
    <section
      className="registration-overlay"
      role="status"
      aria-live="polite"
      aria-busy="true"
    >
      <div className="registration-result">
        <span className="progress-orbit" aria-hidden="true" />
        <p className="registration-loading-title">Loading...</p>
        <p className="registration-progress-phase" id={phaseId}>
          {phaseLabel}
        </p>
        {!indeterminate && (
          <>
            <div
              className="registration-progress-track"
              role="progressbar"
              aria-labelledby={phaseId}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={progress.percent}
              aria-valuetext={`${progress.percent}% completed, step ${progress.step} of ${progress.total}: ${phaseLabel}`}
            >
              <span style={{ width: `${progress.percent}%` }} />
            </div>
            <div className="registration-progress-meta">
              <strong>{progress.percent}% completed</strong>
              <span>
                Step {progress.step} of {progress.total}
              </span>
            </div>
          </>
        )}
        <p className="registration-progress-detail">{message}</p>
      </div>
    </section>
  );
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) {
    const body = (await response.json().catch(() => ({}))) as ApiError;
    throw new ApiRequestError(
      body.detail || `Request failed (${response.status})`,
      response.status,
      parseRetryAfter(response.headers.get("Retry-After")),
    );
  }
  return response.status === 204
    ? (undefined as T)
    : ((await response.json()) as T);
}

function CreateUserModal({
  open,
  catalog,
  draft,
  creating,
  error,
  onDraftChange,
  onClose,
  onSubmit,
}: {
  open: boolean;
  catalog: CatalogLab[];
  draft: UserDraft;
  creating: boolean;
  error: string;
  onDraftChange: (draft: UserDraft) => void;
  onClose: () => void;
  onSubmit: (event: FormEvent) => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  useDialogFocus(open, creating ? () => undefined : onClose, panelRef, nameRef);

  if (!open) return null;
  return createPortal(
    <div className="lab-manager-overlay">
      <section
        className="lab-manager-modal create-user-modal"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={creating}
        tabIndex={-1}
      >
        <header>
          <div>
            <p className="eyebrow">Participant access</p>
            <h2 id={titleId}>Add user</h2>
            <p id={descriptionId}>
              Enter the participant details and select one or more initial starter kits.
            </p>
          </div>
          <span className="lab-selection-count">
            {draft.lab_ids.length} selected
          </span>
        </header>
        <form className="create-user-form" onSubmit={onSubmit}>
          <div className="create-user-fields">
            <label>
              Full name
              <input
                ref={nameRef}
                value={draft.name}
                onChange={(event) => onDraftChange({ ...draft, name: event.target.value })}
                minLength={2}
                maxLength={120}
                disabled={creating}
                autoComplete="name"
                required
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={draft.email}
                onChange={(event) => onDraftChange({ ...draft, email: event.target.value })}
                disabled={creating}
                autoComplete="email"
                required
              />
            </label>
          </div>
          <div className="lab-manager-table-wrap">
            <table className="lab-manager-table create-user-lab-table">
              <caption className="sr-only">Select initial starter kits</caption>
              <thead>
                <tr>
                  <th scope="col">Select</th>
                  <th scope="col">Starter kit</th>
                  <th scope="col">Version</th>
                  <th scope="col">Description</th>
                  <th scope="col">Availability</th>
                </tr>
              </thead>
              <tbody>
                {catalog.map((lab) => {
                  const selected = draft.lab_ids.includes(lab.lab_id);
                  return (
                    <tr key={lab.lab_id}>
                      <td>
                        <input
                          className="lab-assignment-check"
                          type="checkbox"
                          checked={selected}
                          disabled={creating || !lab.available}
                          aria-label={`Select ${lab.display_name} starter kit`}
                          onChange={(event) => onDraftChange({
                            ...draft,
                            lab_ids: event.target.checked
                              ? [...draft.lab_ids, lab.lab_id]
                              : draft.lab_ids.filter((value) => value !== lab.lab_id),
                          })}
                        />
                      </td>
                      <td><strong>{lab.display_name}</strong></td>
                      <td>{lab.pack_version}</td>
                      <td className="lab-table-description">{labDescription(lab)}</td>
                      <td>
                        <span className={`lab-state ${lab.available ? "unassigned" : "planned"}`}>
                          {lab.available ? "Available" : "Planned"}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          {error && <p className="lab-manager-error" role="alert">{error}</p>}
          <footer>
            <button className="secondary" type="button" disabled={creating} onClick={onClose}>
              Cancel
            </button>
            <button type="submit" disabled={creating || !draft.lab_ids.length}>
              {creating ? "Creating..." : "Create user"}
            </button>
          </footer>
        </form>
      </section>
    </div>,
    document.body,
  );
}

function LabManagerModal({
  open,
  user,
  catalog,
  selectedLabIds,
  confirmingRemoval,
  error,
  onSelectionChange,
  onRedeploy,
  onClose,
  onSave,
}: {
  open: boolean;
  user: LabUser | null;
  catalog: CatalogLab[];
  selectedLabIds: string[];
  confirmingRemoval: boolean;
  error: string;
  onSelectionChange: (labIds: string[]) => void;
  onRedeploy: (lab: AssignedLab) => void;
  onClose: () => void;
  onSave: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useDialogFocus(open, onClose, panelRef, closeRef);

  if (!open || !user) return null;
  const assigned = new Map(user.labs.map((lab) => [lab.lab_id, lab]));
  const changes = labAssignmentChanges(
    user.labs.map((lab) => lab.lab_id),
    selectedLabIds,
  );
  const hasChanges = Boolean(changes.add.length || changes.remove.length);
  return createPortal(
    <div className="lab-manager-overlay">
      <section
        className="lab-manager-modal"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        tabIndex={-1}
      >
        <header>
          <div>
            <p className="eyebrow">Participant starter kits</p>
            <h2 id={titleId}>Manage starter kits</h2>
            <p id={descriptionId}>{user.email}</p>
          </div>
          <span className="lab-selection-count">
            {selectedLabIds.length} selected
          </span>
        </header>
        <div className="lab-manager-table-wrap">
          <table className="lab-manager-table">
            <thead>
              <tr>
                <th scope="col">Assigned</th>
                <th scope="col">Starter kit</th>
                <th scope="col">Version</th>
                <th scope="col">Description</th>
                <th scope="col">State</th>
                <th scope="col" className="actions-column">Action</th>
              </tr>
            </thead>
            <tbody>
              {catalog.map((lab) => {
                const installed = assigned.get(lab.lab_id);
                const selected = selectedLabIds.includes(lab.lab_id);
                const hasBundledUpdate = Boolean(
                  installed && installed.pack_version !== lab.pack_version,
                );
                return (
                  <tr key={lab.lab_id}>
                    <td>
                      <input
                        className="lab-assignment-check"
                        type="checkbox"
                        checked={selected}
                        disabled={!lab.available}
                        aria-label={`${selected ? "Remove" : "Add"} ${lab.display_name} ${selected ? "from" : "to"} ${user.email}`}
                        onChange={(event) => onSelectionChange(
                          event.target.checked
                            ? [...selectedLabIds, lab.lab_id]
                            : selectedLabIds.filter((value) => value !== lab.lab_id),
                        )}
                      />
                    </td>
                    <td><strong>{lab.display_name}</strong></td>
                    <td>
                      <span className="kit-version-copy">
                        <strong>{installed ? `Installed ${installed.pack_version}` : `Bundled ${lab.pack_version}`}</strong>
                        {installed && <small>Bundled {lab.pack_version}</small>}
                        {installed && (
                          <span className={`kit-version-state ${hasBundledUpdate ? "update" : "current"}`}>
                            {hasBundledUpdate ? "Update available" : "Current"}
                          </span>
                        )}
                      </span>
                    </td>
                    <td className="lab-table-description">{labDescription(lab)}</td>
                    <td>
                      <span className={`lab-state ${installed ? "installed" : lab.available ? "unassigned" : "planned"}`}>
                        {installed ? labPhaseLabel(installed.phase) : lab.available ? "Pending" : "Planned"}
                      </span>
                    </td>
                    <td className="actions-column">
                      <button
                        className="table-action table-reset"
                        type="button"
                        disabled={!installed}
                        onClick={() => installed && onRedeploy(installed)}
                        aria-label={hasBundledUpdate
                          ? `Update ${lab.display_name} from ${installed?.pack_version} to ${lab.pack_version} for ${user.email}`
                          : `Redeploy ${lab.display_name} for ${user.email}`}
                        title={installed ? hasBundledUpdate ? "Update starter kit" : "Redeploy starter kit" : "Assign the lab before redeploying"}
                      >
                        <RefreshIcon />
                      </button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {confirmingRemoval && (
          <p className="lab-manager-warning" role="alert">
            Confirm removal of {changes.remove.length} {changes.remove.length === 1 ? "starter kit" : "starter kits"}. Only their jobs, tables, objects and workspace content will be deleted.
          </p>
        )}
        {error && <p className="lab-manager-error" role="alert">{error}</p>}
        <footer>
          <button ref={closeRef} className="secondary" type="button" onClick={onClose}>
            {confirmingRemoval ? "Back" : "Cancel"}
          </button>
          <button type="button" disabled={!hasChanges || !selectedLabIds.length} onClick={onSave}>
                  {confirmingRemoval ? "Confirm changes" : "Save"}
          </button>
        </footer>
      </section>
    </div>,
    document.body,
  );
}

function GovernanceModuleModal({
  open,
  user,
  module,
  selected,
  busy,
  error,
  onSelectedChange,
  onInstall,
  onResume,
  onRedeploy,
  onDelete,
  onClose,
}: {
  open: boolean;
  user: LabUser | null;
  module: AdminModule | null;
  selected: boolean;
  busy: boolean;
  error: string;
  onSelectedChange: (selected: boolean) => void;
  onInstall: () => void;
  onResume: (kind: ModuleOperationKind) => void;
  onRedeploy: () => void;
  onDelete: () => void;
  onClose: () => void;
}) {
  const titleId = useId();
  const descriptionId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  useDialogFocus(open, busy ? () => undefined : onClose, panelRef, closeRef);

  if (!open || !user || !module) return null;
  const transitioning = ["installing", "redeploying", "deleting"].includes(module.status);
  const recoverableKind = moduleOperationKind(module.status, module.operation_type);
  const resumable = Boolean(recoverableKind && module.operation_id);
  const state = module.status.replaceAll("_", " ");
  return createPortal(
    <div className="lab-manager-overlay">
      <section
        className="lab-manager-modal governance-module-modal"
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={descriptionId}
        aria-busy={busy || transitioning}
        tabIndex={-1}
      >
        <header>
          <div>
            <p className="eyebrow">Global production module</p>
            <h2 id={titleId}>{module.display_name}</h2>
            <p id={descriptionId}>Manage the singleton through administrator {user.email}.</p>
          </div>
          <span className={`lab-state ${module.enabled ? "installed" : module.installed ? "planned" : "unassigned"}`}>
            {state}
          </span>
        </header>
        <div className="governance-module-body">
          <label className="governance-module-option">
            <input
              className="lab-assignment-check"
              type="checkbox"
              checked={module.installed || selected}
              disabled={module.installed || busy || transitioning}
              onChange={(event) => onSelectedChange(event.target.checked)}
            />
            <span>
              <strong>{module.display_name}</strong>
              <small>Creates the global Agent, dedicated AI Compute and governance control tables in the fixed oci_artifacts bucket.</small>
            </span>
          </label>
          <p className="governance-module-note">
            This installation is shared by every administrator. AIDP developers can use the Agent, while only AI Data Platform administrators can modify it.
          </p>
          <p className="governance-module-note kit-version-copy">
            <strong>{module.installed_version ? `Installed ${module.installed_version}` : "Not installed"}</strong>
            <small>Bundled {module.bundled_version || "unknown"}</small>
            {module.installed && (
              <span className={`kit-version-state ${module.update_available ? "update" : "current"}`}>
                {module.update_available ? "Update available" : "Current"}
              </span>
            )}
          </p>
          {module.status === "error" && module.message && (
            <p className="lab-manager-error" role="alert">{module.message}</p>
          )}
          {error && <p className="lab-manager-error" role="alert">{error}</p>}
        </div>
        <footer>
          <button ref={closeRef} className="secondary" type="button" disabled={busy} onClick={onClose}>
            Close
          </button>
          {resumable ? (
            <button
              type="button"
              disabled={busy}
              onClick={() => recoverableKind && onResume(recoverableKind)}
            >
              Resume {recoverableKind === "install" ? "installation" : recoverableKind === "redeploy" ? "redeployment" : "deletion"}
            </button>
          ) : module.installed ? (
            <>
              <button className="secondary destructive" type="button" disabled={busy || transitioning} onClick={onDelete}>
                Delete
              </button>
              <button type="button" disabled={busy || transitioning} onClick={onRedeploy}>
                {module.update_available ? "Update" : "Redeploy"}
              </button>
            </>
          ) : (
            <button type="button" disabled={!selected || busy || transitioning} onClick={onInstall}>
              Install
            </button>
          )}
        </footer>
      </section>
    </div>,
    document.body,
  );
}

function usePublicConfig() {
  const [config, setConfig] = useState<PublicConfig | null>(null);
  useEffect(() => {
    void api<PublicConfig>("/api/config")
      .then(setConfig)
      .catch(() => undefined);
  }, []);
  return config;
}

function useAdminSession() {
  const [session, setSession] = useState<AdminSession | null>(null);
  useEffect(() => {
    void api<AdminSession>("/api/admin/session")
      .then(setSession)
      .catch(() => undefined);
  }, []);
  return session;
}

function OracleMark() {
  return (
    <svg viewBox="0 0 16 16" fill="none" aria-hidden="true">
      <path
        fill="currentColor"
        fillRule="evenodd"
        d="M.1 8c0 2.761 2.237 5 4.997 5h5.806A4.999 4.999 0 0015.9 8c0-2.761-2.237-5-4.997-5H5.097A4.999 4.999 0 00.1 8zm13.911 0a3.235 3.235 0 01-3.234 3.237h-5.55A3.235 3.235 0 011.991 8a3.235 3.235 0 013.234-3.236h5.551A3.235 3.235 0 0114.011 8z"
        clipRule="evenodd"
      />
    </svg>
  );
}

function AdminLoginIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeMiterlimit="10"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
      />
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M2 12.88v-1.76c0-1.04.85-1.9 1.9-1.9 1.81 0 2.55-1.28 1.64-2.85a1.9 1.9 0 0 1 .7-2.59l1.73-.99a1.9 1.9 0 0 1 2.28.6l.11.19c.9 1.57 2.38 1.57 3.29 0l.11-.19a1.9 1.9 0 0 1 2.28-.6l1.73.99a1.9 1.9 0 0 1 .7 2.59c-.91 1.57-.17 2.85 1.64 2.85 1.04 0 1.9.85 1.9 1.9v1.76c0 1.04-.85 1.9-1.9 1.9-1.81 0-2.55 1.28-1.64 2.85a1.9 1.9 0 0 1-.7 2.59l-1.73.99a1.9 1.9 0 0 1-2.28-.6l-.11-.19c-.9-1.57-2.38-1.57-3.29 0l-.11.19a1.9 1.9 0 0 1-2.28.6l-1.73-.99a1.9 1.9 0 0 1-.7-2.59c.91-1.57.17-2.85-1.64-2.85-1.05 0-1.9-.85-1.9-1.9Z"
      />
    </svg>
  );
}

function HomeIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="m3 11 9-8 9 8" />
      <path d="M5 10v10h14V10M9 20v-6h6v6" />
    </svg>
  );
}

function SearchIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <circle cx="10.5" cy="10.5" r="6.5" />
      <path strokeLinecap="round" d="m16 16 4.5 4.5" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      aria-hidden="true"
    >
      <path strokeLinecap="round" d="M12 5v14M5 12h14" />
    </svg>
  );
}

function RefreshIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M20 11a8 8 0 0 0-14.8-4L3 10m0-6v6h6m-5 3a8 8 0 0 0 14.8 4L21 14m0 6v-6h-6"
      />
    </svg>
  );
}

function EditIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M13.5 6.5 17.5 10.5M4 20l4.3-1 10.9-10.9a2.8 2.8 0 0 0-4-4L4.3 15 4 20Z"
      />
    </svg>
  );
}

function TrashIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="M5 7h14m-9 4v6m4-6v6M9 7l.7-3h4.6l.7 3m-8.2 0 .7 13h9.2l.7-13"
      />
    </svg>
  );
}

function AccessReadyIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      aria-hidden="true"
    >
      <path
        strokeLinecap="round"
        strokeLinejoin="round"
        d="m5 12.5 4.1 4.1L19 6.7"
      />
      <circle cx="12" cy="12" r="9" />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M17.5 14H19C20.1 14 21 13.1 21 12V5C21 3.9 20.1 3 19 3H12C10.9 3 10 3.9 10 5v1.5M5 10h7c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H5c-1.1 0-2-.9-2-2v-7c0-1.1.9-2 2-2Z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function OpenExternalIcon() {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      aria-hidden="true"
    >
      <path strokeLinecap="round" strokeLinejoin="round" d="M14 5h5v5m0-5-8 8" />
      <path strokeLinecap="round" strokeLinejoin="round" d="M19 13v5a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1h5" />
    </svg>
  );
}

function LogoutIcon() {
  return (
    <svg viewBox="0 0 20 20" fill="none" aria-hidden="true">
      <path
        fill="currentColor"
        d="M10.24 0a10 10 0 1 0 8.07 16.15.67.67 0 1 0-1.05-.82 8.9 8.9 0 1 1-.08-10.77.67.67 0 1 0 1.05-.82A9.95 9.95 0 0 0 10.24 0Zm6.86 7.16a.67.67 0 0 0-.94.95l1.56 1.53-10.26.01a.65.65 0 1 0 0 1.3l10.31-.01-1.55 1.56a.67.67 0 1 0 .95.94l2.64-2.64a.67.67 0 0 0-.01-.94L17.1 7.16Z"
      />
    </svg>
  );
}

function Brand() {
  return (
    <a className="brand" href="/" aria-label="Oracle AI Data Platform Workbench Starter Kits home">
      <span className="brand-mark">
        <OracleMark />
      </span>
      <span>
        <strong>Oracle AI Data Platform Workbench</strong>
        <small>Starter Kits</small>
      </span>
    </a>
  );
}

function Shell({
  children,
  onSignOut,
  onAdminLogin,
  onHome,
  operatorUsername,
}: {
  children: React.ReactNode;
  onSignOut?: () => void;
  onAdminLogin?: () => void;
  onHome?: () => void;
  operatorUsername?: string;
}) {
  const currentPath = window.location.pathname;
  return (
    <div className="page-shell">
      <div className="header-band">
        <header>
          <Brand />
          {onSignOut && (
            <nav className="admin-nav" aria-label="Admin navigation">
              <a
                href="/admin/users"
                aria-current={
                  currentPath === "/admin/users" ? "page" : undefined
                }
              >
                Users
              </a>
              <a
                href="/admin/settings"
                aria-current={
                  currentPath === "/admin/settings" ? "page" : undefined
                }
              >
                Settings
              </a>
            </nav>
          )}
          <div className="header-actions">
            {operatorUsername && (
              <span className="operator-identity" title={operatorUsername}>
                Deployed by {operatorUsername}
              </span>
            )}
            {onSignOut ? (
              <button
                className="header-signout"
                type="button"
                onClick={onSignOut}
                aria-label="Logout"
                data-tooltip="Logout"
              >
                <LogoutIcon />
              </button>
            ) : onHome ? (
              <button
                className="admin-link"
                type="button"
                onClick={onHome}
                aria-label="Return to starter kit registration"
                title="Return to starter kit registration"
              >
                <HomeIcon />
              </button>
            ) : onAdminLogin ? (
              <button
                className="admin-link"
                type="button"
                onClick={onAdminLogin}
                aria-label="Administrator login"
                title="Administrator login"
              >
                <AdminLoginIcon />
              </button>
            ) : null}
          </div>
        </header>
      </div>
      <main>{children}</main>
      <footer className="app-footer">
        <span>
          Made with{" "}
          <span className="footer-heart" aria-hidden="true">
            &#9829;
          </span>{" "}
          at AI CloudTech
        </span>
        <span className="footer-divider" aria-hidden="true">
          &middot;
        </span>
        <span>Developed by </span>
        <a
          href="https://www.linkedin.com/in/joelgangini"
          target="_blank"
          rel="noopener noreferrer"
        >
          Joel Gangini
        </a>
      </footer>
    </div>
  );
}

function registrationAccessView(
  configLoaded: boolean,
  production: boolean,
  adminLoginVisible: boolean,
) {
  const canSwitch = configLoaded && !production;
  return {
    showAdminLogin: !configLoaded || production || adminLoginVisible,
    showAdminLink: canSwitch && !adminLoginVisible,
    showHomeLink: canSwitch && adminLoginVisible,
  };
}

function RegisterPage({
  initialAdminLogin = false,
}: {
  initialAdminLogin?: boolean;
}) {
  const publicConfig = usePublicConfig();
  const catalog = participantLabCatalog(publicConfig?.labs ?? fallbackCatalog);
  const production = publicConfig?.deployment_mode === "production";
  const configLoaded = publicConfig !== null;
  const [adminLoginVisible, setAdminLoginVisible] = useState(initialAdminLogin);
  const [form, setForm] = useState({ name: "", email: "" });
  const [labIds, setLabIds] = useState<string[]>(["banking"]);
  const [labPickerOpen, setLabPickerOpen] = useState(false);
  const [codeSlots, setCodeSlots] = useState<string[]>(() => Array(8).fill(""));
  const codeInputs = useRef<Array<HTMLInputElement | null>>([]);
  const labPickerRef = useRef<HTMLDivElement>(null);
  const labPickerTriggerRef = useRef<HTMLButtonElement>(null);
  const labPickerLabelId = useId();
  const labPickerMenuId = useId();
  const registrationAbortRef = useRef<AbortController | null>(null);
  const readyDialogRef = useRef<HTMLDivElement>(null);
  const readyCloseRef = useRef<HTMLButtonElement>(null);
  const [state, setState] = useState<{
    status: "idle" | "processing" | "ready" | "error";
    phase?: RegistrationPhaseValue;
    message: string;
    aidpUrl?: string;
  }>({ status: "idle", message: "" });
  const closeReady = () => setState({ status: "idle", message: "" });
  useDialogFocus(
    state.status === "ready",
    closeReady,
    readyDialogRef,
    readyCloseRef,
  );
  useEffect(
    () => () => {
      registrationAbortRef.current?.abort();
    },
    [],
  );
  useEffect(() => {
    if (!labPickerOpen) return undefined;
    const closeOnOutsideClick = (event: MouseEvent) => {
      if (!labPickerRef.current?.contains(event.target as Node))
        setLabPickerOpen(false);
    };
    const closeOnEscape = (event: globalThis.KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setLabPickerOpen(false);
      labPickerTriggerRef.current?.focus();
    };
    document.addEventListener("mousedown", closeOnOutsideClick);
    document.addEventListener("keydown", closeOnEscape);
    return () => {
      document.removeEventListener("mousedown", closeOnOutsideClick);
      document.removeEventListener("keydown", closeOnEscape);
    };
  }, [labPickerOpen]);
  const update = (name: keyof typeof form, value: string) =>
    setForm((current) => ({ ...current, [name]: value }));
  const registrationCode = `${codeSlots.slice(0, 4).join("")}-${codeSlots.slice(4).join("")}`;
  const selectedLabSummary =
    labIds.length === 0
      ? "Choose starter kits"
      : labIds.length === 1
        ? labLabel(catalog, labIds[0])
        : `${labIds.length} starter kits selected`;

  function focusCode(index: number) {
    codeInputs.current[Math.min(index, 7)]?.focus();
  }
  function setCodeSlot(index: number, value: string) {
    const character =
      value.toUpperCase().match(index < 4 ? /[A-Z]/ : /[0-9]/)?.[0] || "";
    setCodeSlots((current) =>
      current.map((slot, slotIndex) =>
        slotIndex === index ? character : slot,
      ),
    );
    if (character && index < 7)
      requestAnimationFrame(() => focusCode(index + 1));
  }
  function pasteCode(value: string) {
    const compact = value.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!/^[A-Z]{1,4}[0-9]{0,4}$/.test(compact)) return;
    const next = Array(8).fill("");
    Array.from(compact).forEach((character, index) => {
      next[index] = character;
    });
    setCodeSlots(next);
    requestAnimationFrame(() => focusCode(Math.min(compact.length, 7)));
  }
  function handleCodeKeyDown(
    index: number,
    event: KeyboardEvent<HTMLInputElement>,
  ) {
    if (event.key !== "Backspace" || codeSlots[index]) return;
    if (index > 0) {
      event.preventDefault();
      setCodeSlots((current) =>
        current.map((slot, slotIndex) => (slotIndex === index - 1 ? "" : slot)),
      );
      requestAnimationFrame(() => focusCode(index - 1));
    }
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (!/^[A-Z]{4}-[0-9]{4}$/.test(registrationCode)) {
      setState({
        status: "error",
        message: "Enter four letters followed by four numbers.",
      });
      focusCode(0);
      return;
    }
    const payload = { ...form, lab_ids: labIds, code: registrationCode };
    registrationAbortRef.current?.abort();
    const controller = new AbortController();
    registrationAbortRef.current = controller;
    setState({
      status: "processing",
      phase: "identity",
      message: "Creating your Identity Domains account…",
    });
    try {
      const result = await pollRegistration({
        signal: controller.signal,
        request: (signal) =>
          api<RegistrationResponse>("/api/register", {
            method: "POST",
            body: JSON.stringify(payload),
            signal,
          }),
        onPending: (pending) =>
          setState({
            status: "processing",
            phase: pending.phase,
            message:
              pending.message ||
              "OCI is reconciling your account. Keep this page open.",
          }),
      });
      setForm({ name: "", email: "" });
      setLabIds(["banking"]);
      setCodeSlots(Array(8).fill(""));
      setState({
        status: "ready",
        message: result.message || "Your starter kit account is ready.",
        aidpUrl: result.aidp_url,
      });
    } catch (error) {
      if (controller.signal.aborted) return;
      setCodeSlots(Array(8).fill(""));
      setState({
        status: "error",
        message:
          error instanceof Error ? error.message : "Registration failed",
      });
    } finally {
      if (registrationAbortRef.current === controller)
        registrationAbortRef.current = null;
    }
  }

  const showAdminLogin = () => {
    setLabPickerOpen(false);
    setAdminLoginVisible(true);
  };
  const showRegistration = () => {
    if (production) return;
    setAdminLoginVisible(false);
    if (window.location.pathname === "/admin/login")
      window.history.replaceState(null, "", "/");
  };
  const accessView = registrationAccessView(
    configLoaded,
    production,
    adminLoginVisible,
  );

  return (
    <Shell
      onAdminLogin={accessView.showAdminLink ? showAdminLogin : undefined}
      onHome={accessView.showHomeLink ? showRegistration : undefined}
    >
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">
            Structured data · notebooks · medallion architecture
          </p>
          <h1>Build in a governed AI data workspace.</h1>
          <p className="lede">
            Register to work with landing, bronze, silver and gold data layers
            in Oracle AI Data Platform Workbench.
          </p>
          <ol className="steps">
            <li className="step-card">
              <span className="step-number">01 · Identity</span>
              <strong>
                <span>Set up</span>
                <span>your account</span>
              </strong>
              <small>Register with your name, email and registration code.</small>
            </li>
            <li className="step-card">
              <span className="step-number">02 · Workbench</span>
              <strong>
                <span>Open AI Data</span>
                <span>Platform Workbench</span>
              </strong>
              <small>Enter the workspace from the Oracle Cloud Console.</small>
            </li>
            <li className="step-card">
              <span className="step-number">03 · Notebooks</span>
              <strong>Start a shared notebook</strong>
              <small>Work across the governed medallion data layers.</small>
            </li>
          </ol>
        </div>
        {accessView.showAdminLogin ? (
          <AdminLoginCard />
        ) : (
          <form
            className="card"
            onSubmit={submit}
            aria-busy={state.status === "processing"}
          >
          <div>
            <p className="eyebrow">Starter kit access</p>
            <h2>Create your account</h2>
            <p>
              Use your work or personal email and the code supplied by the
              instructor.
            </p>
          </div>
          <label>
            Full name
            <input
              autoComplete="name"
              value={form.name}
              onChange={(e) => update("name", e.target.value)}
              minLength={2}
              maxLength={120}
              required
            />
          </label>
          <label>
            Email
            <input
              type="email"
              autoComplete="email"
              value={form.email}
              onChange={(e) => update("email", e.target.value)}
              required
            />
          </label>
          <div className="lab-combobox" ref={labPickerRef}>
            <span className="lab-combobox-label" id={labPickerLabelId}>
              Starter kits
            </span>
            <button
              ref={labPickerTriggerRef}
              type="button"
              className="lab-combobox-trigger"
              role="combobox"
              aria-expanded={labPickerOpen}
              aria-haspopup="listbox"
              aria-controls={labPickerMenuId}
              aria-labelledby={`${labPickerLabelId} ${labPickerMenuId}-summary`}
              onClick={() => setLabPickerOpen((open) => !open)}
            >
              <span id={`${labPickerMenuId}-summary`}>{selectedLabSummary}</span>
            </button>
            {labPickerOpen && (
              <div
                className="lab-combobox-menu"
                id={labPickerMenuId}
                role="listbox"
                aria-labelledby={labPickerLabelId}
                aria-multiselectable="true"
              >
                {catalog.map((lab) => {
                  const selected = labIds.includes(lab.lab_id);
                  return (
                    <button
                      type="button"
                      className={`lab-combobox-option${selected ? " selected" : ""}`}
                      key={lab.lab_id}
                      role="option"
                      aria-selected={selected}
                      disabled={!lab.available}
                      onClick={() =>
                        setLabIds((current) =>
                          current.includes(lab.lab_id)
                            ? current.filter((value) => value !== lab.lab_id)
                            : [...current, lab.lab_id],
                        )
                      }
                    >
                      <span className="lab-combobox-check" aria-hidden="true" />
                      <span className="lab-combobox-copy">
                        <span className="lab-combobox-title">
                          <strong>{lab.display_name}</strong>
                          {!lab.available && <small>Planned</small>}
                        </span>
                        <span className="lab-combobox-description">
                          {labDescription(lab)}
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <fieldset className="registration-code">
            <legend>Registration code</legend>
            <span id="registration-code-help" className="sr-only">
              Enter four letters followed by four numbers.
            </span>
            <div
              className="code-slots"
              onPaste={(event) => {
                event.preventDefault();
                pasteCode(event.clipboardData.getData("text"));
              }}
            >
              {codeSlots.map((value, index) => (
                <span className="code-slot-wrap" key={index}>
                  {index === 4 && (
                    <span className="code-separator" aria-hidden="true">
                      -
                    </span>
                  )}
                  <input
                    ref={(element) => {
                      codeInputs.current[index] = element;
                    }}
                    className="code-slot"
                    aria-label={`Registration code character ${index + 1} of 8`}
                    aria-describedby="registration-code-help"
                    autoComplete="off"
                    autoCapitalize="characters"
                    inputMode={index < 4 ? "text" : "numeric"}
                    maxLength={1}
                    value={value}
                    onChange={(event) => setCodeSlot(index, event.target.value)}
                    onKeyDown={(event) => handleCodeKeyDown(index, event)}
                    onFocus={(event) => event.currentTarget.select()}
                    required
                  />
                </span>
              ))}
            </div>
          </fieldset>
          {state.status === "error" && (
            <p className="notice error" role="alert">
              {state.message}
            </p>
          )}
          <button disabled={state.status === "processing" || !labIds.length}>
            {state.status === "processing"
              ? "Creating account…"
              : "Create account"}
          </button>
          </form>
        )}
      </section>
      {state.status === "processing" && (
        <ProvisioningOverlay phase={state.phase} message={state.message} />
      )}
      {state.status === "ready" && (
        <section className="registration-overlay">
          <div
            className="registration-result registration-result-ready"
            ref={readyDialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="registration-ready-title"
            aria-describedby="registration-ready-message"
            tabIndex={-1}
          >
            <div className="confirm-content">
              <div className="confirm-icon">
                <AccessReadyIcon />
              </div>
              <p className="eyebrow">Access ready</p>
              <h2 id="registration-ready-title">Your starter kit account is ready</h2>
              <p id="registration-ready-message">{state.message}</p>
            </div>
            <footer>
              <button
                className="secondary"
                ref={readyCloseRef}
                type="button"
                onClick={closeReady}
              >
                Return to registration
              </button>
              {state.aidpUrl ? (
                <a
                  className="result-link"
                  href={state.aidpUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                >
                  Open AI Data Platform Workbench
                </a>
              ) : (
                <button className="secondary" type="button" onClick={closeReady}>
                  Close
                </button>
              )}
            </footer>
          </div>
        </section>
      )}
    </Shell>
  );
}

function AdminLoginCard() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [error, setError] = useState("");
  async function submit(event: FormEvent) {
    event.preventDefault();
    setError("");
    try {
      await api("/api/admin/login", {
        method: "POST",
        body: JSON.stringify({ username, password }),
      });
      setPassword("");
      window.location.assign("/admin/users");
    } catch (reason) {
      setPassword("");
      setError(reason instanceof Error ? reason.message : "Login failed");
    }
  }
  return (
    <form className="card" autoComplete="off" onSubmit={submit}>
      <div>
        <p className="eyebrow">Administrator access</p>
        <h2>Sign in</h2>
        <p>Manage starter kit users and application settings.</p>
      </div>
      <label>
        Username
        <input
          autoComplete="off"
          autoFocus
          name="aidp-admin-username"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          required
        />
      </label>
      <div className="login-password-field">
        <label htmlFor="aidp-admin-password">Password</label>
        <span className="password-control login-password-control">
          {/* ponytail: local previews reuse one origin while deployment passwords rotate. */}
          <input
            id="aidp-admin-password"
            type={showPassword ? "text" : "password"}
            autoComplete="new-password"
            name="aidp-admin-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
          <button
            className="password-action"
            type="button"
            aria-label={showPassword ? "Hide password" : "Show password"}
            aria-pressed={showPassword}
            title={showPassword ? "Hide password" : "Show password"}
            onClick={() => setShowPassword((visible) => !visible)}
          >
            {showPassword ? (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68" />
                <path d="M6.61 6.61A13.53 13.53 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61" />
                <line x1="2" x2="22" y1="2" y2="22" />
              </svg>
            ) : (
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M2.06 12.35a1 1 0 0 1 0-.7C3.73 7.6 7.59 5 12 5c4.41 0 8.27 2.6 9.94 6.65a1 1 0 0 1 0 .7C20.27 16.4 16.41 19 12 19c-4.41 0-8.27-2.6-9.94-6.65" />
                <circle cx="12" cy="12" r="3" />
              </svg>
            )}
          </button>
        </span>
      </div>
      {error && (
        <p className="notice error" role="alert">
          {error}
        </p>
      )}
      <button>Sign in</button>
    </form>
  );
}

function AdminUsers() {
  const adminSession = useAdminSession();
  const publicConfig = usePublicConfig();
  const catalog = participantLabCatalog(publicConfig?.labs ?? fallbackCatalog);
  const production = publicConfig?.deployment_mode === "production";
  const [users, setUsers] = useState<LabUser[]>([]);
  const [modules, setModules] = useState<AdminModule[]>([]);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [tableError, setTableError] = useState("");
  const [message, setMessage] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createProgress, setCreateProgress] = useState<RegistrationResponse | null>(null);
  const [draft, setDraft] = useState({ name: "", email: "", lab_ids: ["banking"] as string[] });
  const createAbortRef = useRef<AbortController | null>(null);
  const operationAbortRef = useRef<AbortController | null>(null);
  const moduleAbortRef = useRef<AbortController | null>(null);
  const moduleOperationsRef = useRef(new Map<string, ModuleOperation>());
  const [labManagerUserId, setLabManagerUserId] = useState<string | null>(null);
  const [selectedLabIds, setSelectedLabIds] = useState<string[]>([]);
  const [confirmingLabRemoval, setConfirmingLabRemoval] = useState(false);
  const [labManagerError, setLabManagerError] = useState("");
  const [pendingLabAction, setPendingLabAction] = useState<{
    kind: "redeploy" | "remove";
    user: LabUser;
    lab: AssignedLab;
  } | null>(null);
  const [operating, setOperating] = useState(false);
  const [operationProgress, setOperationProgress] = useState<RegistrationResponse | null>(null);
  const [operationError, setOperationError] = useState("");
  const [moduleManagerUserId, setModuleManagerUserId] = useState<string | null>(null);
  const [moduleSelected, setModuleSelected] = useState(false);
  const [moduleLoadError, setModuleLoadError] = useState("");
  const [moduleOperationError, setModuleOperationError] = useState("");
  const [moduleOperating, setModuleOperating] = useState(false);
  const [moduleProgress, setModuleProgress] = useState<RegistrationResponse | null>(null);
  const [pendingModuleAction, setPendingModuleAction] = useState<ModuleOperationKind | null>(null);
  const [pendingDelete, setPendingDelete] = useState<LabUser | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [logoutOpen, setLogoutOpen] = useState(false);
  async function loadUsers() {
    setTableError("");
    try {
      const loaded = (await api<{ users: LabUser[] }>("/api/admin/users")).users;
      setUsers(loaded);
      return loaded;
    } catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401)
        window.location.assign("/admin/login");
      else
        setTableError(
          reason instanceof Error ? reason.message : "Unable to load users",
        );
    }
  }
  async function loadModules() {
    if (!production) {
      setModules([]);
      setModuleLoadError("");
      return [];
    }
    setModuleLoadError("");
    try {
      const loaded = (await api<{ modules: AdminModule[] }>("/api/admin/modules")).modules;
      setModules(loaded);
      for (const module of loaded) {
        const recoverableKind = moduleOperationKind(module.status, module.operation_type);
        for (const kind of ["install", "redeploy", "delete"] as const) {
          const key = moduleOperationKey(module.module_id, kind);
          if (recoverableKind === kind) {
            if (module.operation_id) {
              const operation = { moduleId: module.module_id, kind, operationId: module.operation_id };
              moduleOperationsRef.current.set(key, operation);
              writeStoredModuleOperation(module.module_id, kind, operation);
            }
            continue;
          }
          moduleOperationsRef.current.delete(key);
          writeStoredModuleOperation(module.module_id, kind);
        }
      }
      return loaded;
    } catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401)
        window.location.assign("/admin/login");
      else
        setModuleLoadError(reason instanceof Error ? reason.message : "Unable to load global modules");
    }
  }
  useEffect(() => {
    void loadUsers();
    return () => {
      createAbortRef.current?.abort();
      operationAbortRef.current?.abort();
      moduleAbortRef.current?.abort();
    };
  }, []);
  useEffect(() => {
    if (production) void loadModules();
  }, [production]);
  const visible = users.filter((user) =>
    `${user.name} ${user.email}`.toLowerCase().includes(query.toLowerCase()),
  );
  const labManagerUser = users.find((user) => user.id === labManagerUserId) ?? null;
  const moduleManagerUser = users.find((user) => user.id === moduleManagerUserId) ?? null;
  const governanceModule = modules.find(({ module_id }) => module_id === "ai_data_governance_vsc_extension") ?? null;
  const pendingLabUpdate = Boolean(
    pendingLabAction?.kind === "redeploy" && pendingLabAction.lab.update_available,
  );
  useEffect(() => {
    if (
      !moduleManagerUserId ||
      !governanceModule ||
      !["installing", "redeploying", "deleting"].includes(governanceModule.status)
    )
      return undefined;
    let cancelled = false;
    let timeout = 0;
    const refresh = async () => {
      await loadModules();
      if (!cancelled) timeout = window.setTimeout(refresh, 2_000);
    };
    timeout = window.setTimeout(refresh, 2_000);
    return () => {
      cancelled = true;
      window.clearTimeout(timeout);
    };
    // An error is terminal until an administrator explicitly resumes its manifest operation.
  }, [moduleManagerUserId, governanceModule?.module_id, governanceModule?.operation_id, governanceModule?.status]);
  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    window.location.assign("/");
  }
  async function createUser(event: FormEvent) {
    event.preventDefault();
    setCreating(true);
    setCreateOpen(false);
    setCreateProgress({
      status: "pending",
      phase: "identity",
      message: "Preparing the participant account.",
    });
    setError("");
    setMessage("");
    createAbortRef.current?.abort();
    const controller = new AbortController();
    createAbortRef.current = controller;
    try {
      const result = await pollRegistration({
        signal: controller.signal,
        request: (signal) =>
          api<RegistrationResponse>("/api/admin/users", {
            method: "POST",
            body: JSON.stringify(draft),
            signal,
          }),
        onPending: setCreateProgress,
      });
      setDraft({ name: "", email: "", lab_ids: ["banking"] });
      setCreateOpen(false);
      setMessage(result.message || "User created and added to the lab.");
      await loadUsers();
    } catch (reason) {
      if (controller.signal.aborted) return;
      setCreateOpen(true);
      setError(
        reason instanceof Error ? reason.message : "Unable to create user",
      );
    } finally {
      if (createAbortRef.current === controller) createAbortRef.current = null;
      setCreateProgress(null);
      setCreating(false);
    }
  }
  function closeCreateUser() {
    if (creating) return;
    setCreateOpen(false);
    setError("");
    setDraft({ name: "", email: "", lab_ids: ["banking"] });
  }
  async function deleteUser() {
    if (!pendingDelete) return;
    setError("");
    setMessage("");
    try {
      await api(`/api/admin/users/${encodeURIComponent(pendingDelete.id)}`, {
        method: "DELETE",
      });
      setPendingDelete(null);
      setMessage("User deleted from the lab.");
      await loadUsers();
    } catch (reason) {
      setDeleteError(
        reason instanceof ApiRequestError &&
          reason.status === 404 &&
          reason.message === "Not Found"
          ? "User deletion is unavailable on the deployed server. Update the AIDP Lab backend and try again."
          : reason instanceof Error
            ? reason.message
            : "Unable to delete user.",
      );
    }
  }
  function operationId(action: NonNullable<typeof pendingLabAction>) {
    const operation = getOrCreateLabOperation(
      loadLabOperation(
        window.localStorage, action.user.id, action.lab.lab_id, action.kind,
      ),
      action.lab.lab_id,
      action.kind,
      () => crypto.randomUUID(),
    );
    persistLabOperation(
      window.localStorage, action.user.id, action.lab.lab_id, action.kind, operation,
    );
    return operation;
  }

  async function requestLabAddition(user: LabUser, labId: string, signal: AbortSignal) {
    return pollRegistration({
      signal,
      request: (requestSignal) => api<RegistrationResponse>(
        `/api/admin/users/${encodeURIComponent(user.id)}/labs`,
        { method: "POST", body: JSON.stringify({ lab_id: labId }), signal: requestSignal },
      ),
      onPending: setOperationProgress,
    });
  }

  async function requestLabAction(
    action: NonNullable<typeof pendingLabAction>,
    signal: AbortSignal,
  ) {
    let operation;
    try {
      operation = operationId(action);
    } catch {
      throw new Error("Browser storage is unavailable; the lab operation was not started.");
    }
    const base = `/api/admin/users/${encodeURIComponent(action.user.id)}/labs/${encodeURIComponent(action.lab.lab_id)}`;
    const result = await pollRegistration({
      signal,
      request: (requestSignal) => action.kind === "redeploy"
        ? api<RegistrationResponse>(`${base}/redeploy`, {
            method: "POST",
            body: JSON.stringify({ operation_id: operation.operationId }),
            signal: requestSignal,
          })
        : api<RegistrationResponse>(`${base}?operation_id=${encodeURIComponent(operation.operationId)}`, {
            method: "DELETE",
            signal: requestSignal,
          }),
      onPending: setOperationProgress,
    });
    persistLabOperation(
      window.localStorage, action.user.id, action.lab.lab_id, action.kind,
    );
    return result;
  }

  function openLabManager(user: LabUser) {
    setLabManagerUserId(user.id);
    setSelectedLabIds(user.labs.map((lab) => lab.lab_id));
    setConfirmingLabRemoval(false);
    setLabManagerError("");
  }

  async function saveLabAssignments() {
    if (!labManagerUser) return;
    const changes = labAssignmentChanges(
      labManagerUser.labs.map((lab) => lab.lab_id),
      selectedLabIds,
    );
    if (!selectedLabIds.length) {
      setLabManagerError("A participant must keep at least one starter kit.");
      return;
    }
    if (changes.remove.length && !confirmingLabRemoval) {
      setConfirmingLabRemoval(true);
      setLabManagerError("");
      return;
    }
    if (!changes.add.length && !changes.remove.length) {
      setLabManagerUserId(null);
      return;
    }
    const controller = new AbortController();
    operationAbortRef.current?.abort();
    operationAbortRef.current = controller;
    setOperating(true);
    setLabManagerError("");
    setMessage("");
    try {
      for (const labId of changes.add) {
        setOperationProgress({
          status: "pending",
          phase: "workspace",
          message: `Adding ${labLabel(catalog, labId)}.`,
        });
        await requestLabAddition(labManagerUser, labId, controller.signal);
      }
      for (const labId of changes.remove) {
        const lab = labManagerUser.labs.find((item) => item.lab_id === labId);
        if (!lab) continue;
        setOperationProgress({
          status: "pending",
          phase: "cleanup",
          message: `Removing ${labLabel(catalog, labId)}.`,
        });
        await requestLabAction({ kind: "remove", user: labManagerUser, lab }, controller.signal);
      }
      await loadUsers();
      setLabManagerUserId(null);
      setConfirmingLabRemoval(false);
      setMessage(`Starter kits updated for ${labManagerUser.email}.`);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const loaded = await loadUsers();
      const refreshed = loaded?.find((user) => user.id === labManagerUser.id);
      if (refreshed) setSelectedLabIds(refreshed.labs.map((lab) => lab.lab_id));
      setConfirmingLabRemoval(false);
      setLabManagerError(reason instanceof Error ? reason.message : "Unable to update the starter kits.");
    } finally {
      if (operationAbortRef.current === controller) operationAbortRef.current = null;
      setOperationProgress(null);
      setOperating(false);
    }
  }

  async function runLabAction() {
    if (!pendingLabAction) return;
    const action = pendingLabAction;
    const controller = new AbortController();
    operationAbortRef.current?.abort();
    operationAbortRef.current = controller;
    setOperating(true);
    setOperationError("");
    setMessage("");
    setOperationProgress({
      status: "pending",
      phase: "cleanup",
      message: `${action.kind === "redeploy" ? action.lab.update_available ? "Updating" : "Reinstalling" : "Removing"} the selected lab resources.`,
    });
    try {
      const result = await requestLabAction(action, controller.signal);
      setPendingLabAction(null);
      setMessage(result.message || "The lab operation completed.");
      await loadUsers();
    } catch (reason) {
      if (controller.signal.aborted) return;
      setOperationError(reason instanceof Error ? reason.message : "Unable to update the lab.");
    } finally {
      if (operationAbortRef.current === controller) operationAbortRef.current = null;
      setOperationProgress(null);
      setOperating(false);
    }
  }

  async function openModuleManager(user: LabUser) {
    if (!production || !user.is_aidp_admin || !governanceModule) return;
    const refreshed = (await loadModules())?.find(
      ({ module_id }) => module_id === governanceModule.module_id,
    ) ?? governanceModule;
    setModuleManagerUserId(user.id);
    setModuleSelected(refreshed.installed);
    setModuleOperationError("");
  }

  async function runModuleAction(kind: ModuleOperationKind) {
    if (!moduleManagerUser || !moduleManagerUser.is_aidp_admin || !governanceModule) return;
    const recoverableKind = moduleOperationKind(governanceModule.status, governanceModule.operation_type);
    const operationKey = moduleOperationKey(governanceModule.module_id, kind);
    let operation;
    try {
      operation = getOrCreateModuleOperation(
        moduleOperationsRef.current.get(operationKey) ??
          readStoredModuleOperation(governanceModule.module_id, kind),
        governanceModule.module_id,
        kind,
        () => crypto.randomUUID(),
        recoverableKind === kind ? governanceModule.operation_id || undefined : undefined,
      );
      moduleOperationsRef.current.set(operationKey, operation);
      writeStoredModuleOperation(governanceModule.module_id, kind, operation);
    } catch (reason) {
      setModuleOperationError(reason instanceof Error ? reason.message : "Unable to prepare the module operation.");
      return;
    }

    const controller = new AbortController();
    moduleAbortRef.current?.abort();
    moduleAbortRef.current = controller;
    setModuleOperating(true);
    setModuleOperationError("");
    setMessage("");
    setModuleProgress({
      status: "pending",
      phase: kind === "delete" ? "cleanup" : "content",
      message: `${kind === "install" ? "Installing" : kind === "redeploy" ? "Redeploying" : "Deleting"} ${governanceModule.display_name}.`,
    });
    let operationId = operation.operationId;
    const moduleBase = `/api/admin/users/${encodeURIComponent(moduleManagerUser.id)}/modules/${encodeURIComponent(governanceModule.module_id)}`;
    try {
      const result = await pollRegistration({
        signal: controller.signal,
        request: async (signal) => {
          const response = await (kind === "delete"
            ? api<AdminModuleOperationResponse>(`${moduleBase}?operation_id=${encodeURIComponent(operationId)}`, {
                method: "DELETE",
                signal,
              })
            : api<AdminModuleOperationResponse>(kind === "redeploy" ? `${moduleBase}/redeploy` : moduleBase, {
                method: "POST",
                body: JSON.stringify({ operation_id: operationId }),
                signal,
              }));
          if (response.operation_id && response.operation_id !== operationId) {
            operationId = response.operation_id;
            const serverOperation = {
              moduleId: governanceModule.module_id,
              kind,
              operationId,
            };
            moduleOperationsRef.current.set(operationKey, serverOperation);
            writeStoredModuleOperation(governanceModule.module_id, kind, serverOperation);
          }
          const complete = kind === "delete"
            ? response.status === "not_installed"
            : response.status === "active";
          const pending = ["installing", "redeploying", "deleting"].includes(response.status);
          return {
            status: complete ? "active" : pending ? "pending" : response.status,
            phase: response.phase,
            message: response.message,
          };
        },
        onPending: setModuleProgress,
      });
      moduleOperationsRef.current.delete(operationKey);
      writeStoredModuleOperation(governanceModule.module_id, kind);
      setPendingModuleAction(null);
      setModuleSelected(false);
      setMessage(result.message || `${governanceModule.display_name} ${kind === "delete" ? "deleted" : "ready"}.`);
      await loadModules();
    } catch (reason) {
      if (controller.signal.aborted) return;
      await loadModules();
      setModuleOperationError(reason instanceof Error ? reason.message : "Unable to update the governance module.");
    } finally {
      if (moduleAbortRef.current === controller) moduleAbortRef.current = null;
      setModuleProgress(null);
      setModuleOperating(false);
    }
  }
  return (
    <>
      <Shell
        onSignOut={() => setLogoutOpen(true)}
        operatorUsername={adminSession?.operator_username || adminSession?.username}
      >
        <section className="admin" aria-busy={operating || creating || moduleOperating} inert={operating || creating || moduleOperating}>
          <div className="admin-panel">
            <div className="admin-panel-heading">
              <h1>Users</h1>
              <button
                className="create-user"
                type="button"
                aria-haspopup="dialog"
                aria-expanded={createOpen}
                onClick={() => {
                  setCreateOpen(true);
                  setError("");
                }}
              >
                <PlusIcon />
                <span>Users</span>
              </button>
            </div>
            <div className="admin-toolbar">
              <form
                className="search"
                onSubmit={(event) => {
                  event.preventDefault();
                  setQuery(search);
                }}
              >
                <label>
                  <span className="sr-only">Search users</span>
                  <input
                    type="search"
                    value={search}
                    onChange={(event) => setSearch(event.target.value)}
                    placeholder="Search by name or email"
                  />
                </label>
                <button
                  className="search-submit"
                  type="submit"
                  aria-label="Search users"
                  title="Search users"
                >
                  <SearchIcon />
                </button>
              </form>
              <div className="toolbar-actions">
                <button
                  className="toolbar-icon"
                  type="button"
                  onClick={() => {
                    void loadUsers();
                    if (production) void loadModules();
                  }}
                  aria-label="Refresh users"
                  title="Refresh users"
                >
                  <RefreshIcon />
                </button>
              </div>
            </div>
            {moduleLoadError && (
              <p className="notice error admin-module-error" role="alert">
                {moduleLoadError}
              </p>
            )}
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Status</th>
                    <th>Starter kits</th>
                    <th>Identity</th>
                    <th className="actions-column">Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {tableError ? (
                    <tr>
                      <td colSpan={6} className="table-error" role="alert">
                        {tableError} Refresh and try again.
                      </td>
                    </tr>
                  ) : (
                    visible.map((user) => (
                    <tr key={user.id}>
                      <td>
                        <span className="row-index">
                          {user.participant_code ?? "--"}
                        </span>
                        {user.name}
                      </td>
                      <td>{user.email}</td>
                      <td>
                        <span className={`badge ${user.status}`}>
                          {user.status}
                        </span>
                      </td>
                      <td>
                        <div className="lab-summary">
                          {user.managed === false ? (
                            <span>
                              <strong>AIDP administrator</strong>
                              <small>Platform administration only</small>
                            </span>
                          ) : (
                            <span>
                              <strong>{user.labs.length} {user.labs.length === 1 ? "starter kit" : "starter kits"}</strong>
                              <small>
                                {user.labs.every((lab) => lab.phase === "active")
                                  ? "All active"
                                  : `${user.labs.filter((lab) => lab.phase === "active").length} active`}
                              </small>
                            </span>
                          )}
                        </div>
                      </td>
                      <td>
                        <span
                          className={`badge ${user.active ? "active" : "inactive"}`}
                        >
                          {user.active ? "Active" : "Inactive"}
                        </span>
                      </td>
                      <td className="row-actions">
                        <span className="row-action-group">
                          {production && user.is_aidp_admin && governanceModule && (
                            <button
                              className="table-action table-module"
                              type="button"
                              aria-haspopup="dialog"
                              aria-expanded={moduleManagerUserId === user.id}
                              onClick={() => void openModuleManager(user)}
                              aria-label={`Manage ${governanceModule.display_name} as ${user.email}`}
                              title={`Manage ${governanceModule.display_name}`}
                            >
                              <AdminLoginIcon />
                            </button>
                          )}
                          {user.managed !== false && (
                            <>
                              <button
                                className="table-action table-edit"
                                type="button"
                                aria-haspopup="dialog"
                                aria-expanded={labManagerUserId === user.id}
                                onClick={() => openLabManager(user)}
                                aria-label={`Manage starter kits for ${user.email}`}
                                title="Manage starter kits"
                              >
                                <EditIcon />
                              </button>
                              <button
                                className="table-action table-delete"
                                type="button"
                                onClick={() => {
                                  setDeleteError("");
                                  setPendingDelete(user);
                                }}
                                aria-label={`Delete ${user.email}`}
                                title="Delete"
                              >
                                <TrashIcon />
                              </button>
                            </>
                          )}
                        </span>
                      </td>
                    </tr>
                    ))
                  )}
                  {!tableError && !visible.length && (
                    <tr>
                      <td colSpan={6} className="empty">
                        No matching lab users.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </section>
      </Shell>
      <CreateUserModal
        open={createOpen}
        catalog={catalog}
        draft={draft}
        creating={creating}
        error={error}
        onDraftChange={setDraft}
        onClose={closeCreateUser}
        onSubmit={createUser}
      />
      {creating && (
        <ProvisioningOverlay
          phase={createProgress?.phase || "identity"}
          message={
            createProgress?.message || "Preparing the participant account."
          }
        />
      )}
      <Toast message={message} onDismiss={() => setMessage("")} />
      <LabManagerModal
        open={Boolean(labManagerUser) && !operating}
        user={labManagerUser}
        catalog={catalog}
        selectedLabIds={selectedLabIds}
        confirmingRemoval={confirmingLabRemoval}
        error={labManagerError}
        onSelectionChange={(labIds) => {
          setSelectedLabIds(labIds);
          setConfirmingLabRemoval(false);
          setLabManagerError("");
        }}
        onRedeploy={(lab) => {
          if (!labManagerUser) return;
          setLabManagerUserId(null);
          setOperationError("");
          setPendingLabAction({ kind: "redeploy", user: labManagerUser, lab });
        }}
        onClose={() => {
          if (confirmingLabRemoval) {
            setConfirmingLabRemoval(false);
            return;
          }
          setLabManagerUserId(null);
          setLabManagerError("");
        }}
        onSave={() => void saveLabAssignments()}
      />
      <GovernanceModuleModal
        open={Boolean(moduleManagerUser) && !moduleOperating && !pendingModuleAction}
        user={moduleManagerUser}
        module={governanceModule}
        selected={moduleSelected}
        busy={moduleOperating}
        error={moduleOperationError}
        onSelectedChange={(selected) => {
          setModuleSelected(selected);
          setModuleOperationError("");
        }}
        onInstall={() => void runModuleAction("install")}
        onResume={(kind) => void runModuleAction(kind)}
        onRedeploy={() => void runModuleAction("redeploy")}
        onDelete={() => {
          setModuleOperationError("");
          setPendingModuleAction("delete");
        }}
        onClose={() => {
          setModuleManagerUserId(null);
          setModuleSelected(false);
          setModuleOperationError("");
        }}
      />
      <ConfirmModal
        open={pendingModuleAction === "delete" && !moduleOperating}
        kind="delete"
        title="Delete global governance module?"
        description={`This permanently deletes ${governanceModule?.display_name ?? "the module"}, its Agent deployment, dedicated AI Compute, credential, notebook, workflow, four Delta tables and only their prefixes in oci_artifacts. The bucket, schema and shared Spark compute are retained.`}
        error={moduleOperationError}
        confirmLabel="Delete module"
        onClose={() => {
          setPendingModuleAction(null);
          setModuleOperationError("");
        }}
        onConfirm={() => void runModuleAction("delete")}
      />
      <ConfirmModal
        open={Boolean(pendingLabAction) && !operating}
        kind={pendingLabAction?.kind === "remove" ? "delete" : "reset"}
        title={pendingLabAction?.kind === "remove" ? "Remove starter kit?" : pendingLabUpdate ? "Update starter kit?" : "Redeploy starter kit?"}
        description={`${pendingLabAction?.kind === "remove" ? "Remove" : pendingLabUpdate ? "Update" : "Reinstall"} only ${pendingLabAction ? labLabel(catalog, pendingLabAction.lab.lab_id) : "this starter kit"} for ${pendingLabAction?.user.email ?? "this participant"}. Other starter kits and Identity access are preserved.`}
        error={operationError}
        confirmLabel={pendingLabAction?.kind === "remove" ? "Remove lab" : pendingLabUpdate ? "Update kit" : "Redeploy lab"}
        onClose={() => {
          setOperationError("");
          setPendingLabAction(null);
        }}
        onConfirm={() => void runLabAction()}
      />
      <ConfirmModal
        open={Boolean(pendingDelete)}
        kind="delete"
        title="Delete user?"
        description={`This will permanently remove ${pendingDelete?.email ?? "this user"} from Identity Domains.`}
        error={deleteError}
        confirmLabel="Delete"
        onClose={() => {
          setDeleteError("");
          setPendingDelete(null);
        }}
        onConfirm={() => void deleteUser()}
      />
      <ConfirmModal
        open={logoutOpen}
        kind="question"
        title="Log out?"
        description="You will need to sign in again to manage lab users."
        confirmLabel="Log out"
        onClose={() => setLogoutOpen(false)}
        onConfirm={() => void logout()}
      />
      {operating && (
        <ProvisioningOverlay
          phase={operationProgress?.phase || "cleanup"}
          message={
            operationProgress?.message || "Updating the participant's starter kit."
          }
        />
      )}
      {moduleOperating && (
        <ProvisioningOverlay
          phase={moduleProgress?.phase}
          label={pendingModuleAction === "delete" ? "Deleting governance module" : "Reconciling governance module"}
          indeterminate
          message={moduleProgress?.message || "Reconciling the global production module."}
        />
      )}
    </>
  );
}

function SettingsRegistrationCodeField({
  value,
  configured,
  onChange,
}: {
  value: string;
  configured: boolean;
  onChange: (value: string) => void;
}) {
  const inputs = useRef<Array<HTMLInputElement | null>>([]);
  const [letters = "", digits = ""] = value.split("-", 2);
  const slots = Array.from({ length: 8 }, (_, index) =>
    index < 4 ? letters[index] ?? "" : digits[index - 4] ?? "",
  );
  const focusSlot = (index: number) => inputs.current[Math.min(Math.max(index, 0), 7)]?.focus();
  const emitSlots = (next: string[]) => {
    const nextLetters = next.slice(0, 4).join("");
    const nextDigits = next.slice(4).join("");
    onChange(nextDigits || nextLetters.length === 4 ? `${nextLetters}-${nextDigits}` : nextLetters);
  };
  const setSlot = (index: number, rawValue: string) => {
    const character = rawValue.toUpperCase().match(index < 4 ? /[A-Z]/ : /[0-9]/)?.[0] ?? "";
    const next = [...slots];
    next[index] = character;
    emitSlots(next);
    if (character && index < 7) requestAnimationFrame(() => focusSlot(index + 1));
  };
  const pasteCode = (rawValue: string) => {
    const compact = rawValue.toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (!/^[A-Z]{4}[0-9]{4}$/.test(compact)) return;
    onChange(`${compact.slice(0, 4)}-${compact.slice(4)}`);
    requestAnimationFrame(() => focusSlot(7));
  };
  const handleKeyDown = (index: number, event: KeyboardEvent<HTMLInputElement>) => {
    if (event.key !== "Backspace" || slots[index] || index === 0) return;
    event.preventDefault();
    const next = [...slots];
    next[index - 1] = "";
    emitSlots(next);
    requestAnimationFrame(() => focusSlot(index - 1));
  };

  return (
    <fieldset className="registration-code settings-registration-code">
      <legend>Lab registration code</legend>
      <div
        className="code-slots"
        onPaste={(event) => {
          event.preventDefault();
          pasteCode(event.clipboardData.getData("text"));
        }}
      >
        {slots.map((slot, index) => (
          <span className="code-slot-wrap" key={index}>
            {index === 4 && <span className="code-separator" aria-hidden="true">-</span>}
            <input
              ref={(element) => {
                inputs.current[index] = element;
              }}
              className="code-slot"
              aria-label={`Lab registration code character ${index + 1} of 8`}
              aria-describedby="registration-code-settings-help"
              autoComplete="off"
              autoCapitalize="characters"
              inputMode={index < 4 ? "text" : "numeric"}
              maxLength={1}
              value={slot}
              onChange={(event) => setSlot(index, event.target.value)}
              onKeyDown={(event) => handleKeyDown(index, event)}
              onFocus={(event) => event.currentTarget.select()}
            />
          </span>
        ))}
      </div>
      <span id="registration-code-settings-help" className="settings-help">
        {configured
          ? "For security, the current code is not displayed. Enter a new AAAA-0000 code to replace it."
          : "Enter an AAAA-0000 code to enable participant registration."}
      </span>
    </fieldset>
  );
}

function ApplicationAccessSettings({
  deploymentMode,
  registrationCode,
  registrationCodeConfigured,
  onRegistrationCodeChange,
  onSave,
}: {
  deploymentMode: "laboratory" | "production";
  registrationCode: string;
  registrationCodeConfigured: boolean;
  onRegistrationCodeChange: (value: string) => void;
  onSave: () => void;
}) {
  if (deploymentMode === "production") {
    return (
      <p className="settings-mode-note">
        Production mode uses administrator access only. Participant registration is disabled.
      </p>
    );
  }

  return (
    <>
      <SettingsRegistrationCodeField
        value={registrationCode}
        configured={registrationCodeConfigured}
        onChange={onRegistrationCodeChange}
      />
      <div className="settings-actions">
        <button
          type="button"
          className="settings-save"
          onClick={onSave}
          disabled={!registrationCode}
        >
          Save Settings
        </button>
      </div>
    </>
  );
}

const applicationUpdateStates = new Set([
  "queued",
  "checking",
  "downloading",
  "building",
  "validating",
  "activating",
]);

function ApplicationReleaseSettings({
  release,
  busy,
  error,
  onUpdate,
}: {
  release: AdminApplicationRelease | null;
  busy: boolean;
  error: string;
  onUpdate: () => void;
}) {
  const operationRunning = Boolean(
    release?.operation && applicationUpdateStates.has(release.operation.status),
  );
  const canUpdate = Boolean(
    release?.updater_available && (release.update_available || operationRunning),
  );
  const statusLabel = operationRunning
    ? "Updating"
    : release?.operation?.status === "failed"
      ? "Update failed"
      : release?.update_available
        ? "Update available"
        : release?.latest_release
          ? "Up to date"
          : "Check unavailable";
  return (
    <section className="application-release" aria-busy={busy}>
      <header className="application-release-heading">
        <div>
          <p className="eyebrow">GitHub release</p>
          <h2>Application version</h2>
          <p>Update the VM in place from the latest immutable release without reinstalling it.</p>
        </div>
        <span className={`release-state ${release?.update_available || operationRunning ? "update" : "current"}`}>
          {statusLabel}
        </span>
      </header>
      {release ? (
        <>
          <dl className="release-summary">
            <div>
              <dt>Installed release</dt>
              <dd>{release.current_release}</dd>
            </div>
            <div>
              <dt>Commit</dt>
              <dd><code title={release.current_commit_sha}>{release.current_commit_sha.slice(0, 12)}</code></dd>
            </div>
            <div>
              <dt>Latest release</dt>
              <dd>
                {release.latest_release_url ? (
                  <a href={release.latest_release_url} target="_blank" rel="noopener noreferrer">
                    {release.latest_release}
                  </a>
                ) : release.latest_release || "Unavailable"}
              </dd>
            </div>
            <div>
              <dt>Source</dt>
              <dd><a href={release.repository} target="_blank" rel="noopener noreferrer">Official repository</a></dd>
            </div>
          </dl>
          {release.update_check_error && (
            <p className="release-warning" role="status">{release.update_check_error}</p>
          )}
          {release.operation?.message && (
            <p
              className={`release-operation ${release.operation.status === "failed" ? "error" : ""}`}
              role={release.operation.status === "failed" ? "alert" : "status"}
              aria-live="polite"
            >
              {release.operation.message}
            </p>
          )}
          {error && <p className="release-operation error" role="alert">{error}</p>}
          <div className="settings-actions release-actions">
            <button
              type="button"
              className="settings-save"
              onClick={onUpdate}
              disabled={!canUpdate || busy}
            >
              {operationRunning ? "Continue update" : "Update from GitHub"}
            </button>
          </div>
          {!release.updater_available && (
            <p className="settings-help">In-place updates are enabled only on the deployed application VM.</p>
          )}
          <div className="release-packages-wrap">
            <table className="release-packages">
              <thead>
                <tr>
                  <th scope="col">Starter kit</th>
                  <th scope="col">Bundled version</th>
                  <th scope="col">Scope</th>
                </tr>
              </thead>
              <tbody>
                {release.packages.map((item) => (
                  <tr key={item.package_id}>
                    <td><strong>{item.display_name}</strong></td>
                    <td>{item.bundled_version}</td>
                    <td>{item.scope === "global" ? "Global module" : "Participant"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="settings-help">
            Updating the application changes the bundled kit versions. Existing participant installations remain unchanged until their Update or Redeploy action is used.
          </p>
        </>
      ) : error ? (
        <p className="release-operation error" role="alert">{error}</p>
      ) : (
        <p className="settings-help" role="status">Loading release metadata…</p>
      )}
    </section>
  );
}

function AdminSettings() {
  const adminSession = useAdminSession();
  type SettingsTab = "workbench" | "application";
  const [activeSettingsTab, setActiveSettingsTab] = useState<SettingsTab>("workbench");
  const [aidpServiceEndpoint, setAidpServiceEndpoint] = useState("");
  const [aidpUrl, setAidpUrl] = useState("");
  const [aidpPlatformId, setAidpPlatformId] = useState("");
  const [deploymentMode, setDeploymentMode] = useState<"laboratory" | "production">("laboratory");
  const [registrationCode, setRegistrationCode] = useState("");
  const [registrationCodeConfigured, setRegistrationCodeConfigured] = useState(false);
  const [applicationRelease, setApplicationRelease] = useState<AdminApplicationRelease | null>(null);
  const [releaseBusy, setReleaseBusy] = useState(false);
  const [releaseError, setReleaseError] = useState("");
  const [releaseProgress, setReleaseProgress] = useState<RegistrationResponse | null>(null);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const releaseAbortRef = useRef<AbortController | null>(null);
  const workbenchTabRef = useRef<HTMLButtonElement>(null);
  const applicationTabRef = useRef<HTMLButtonElement>(null);
  const serviceEndpointRef = useRef<HTMLInputElement>(null);
  const urlRef = useRef<HTMLInputElement>(null);
  const platformIdRef = useRef<HTMLInputElement>(null);
  function applyAdminSettings(result: AdminSettingsResponse) {
    setAidpServiceEndpoint(result.aidp_service_endpoint);
    setAidpUrl(result.aidp_url);
    setAidpPlatformId(result.aidp_platform_id);
    setDeploymentMode(result.deployment_mode);
    setRegistrationCodeConfigured(result.registration_code_configured);
  }
  async function loadApplicationRelease() {
    setReleaseError("");
    try {
      const result = await api<AdminApplicationRelease>("/api/admin/application");
      setApplicationRelease(result);
      return result;
    } catch (reason) {
      if (reason instanceof ApiRequestError && reason.status === 401)
        window.location.assign("/admin/login");
      else
        setReleaseError(
          reason instanceof Error ? reason.message : "Unable to load application release metadata",
        );
      return null;
    }
  }
  useEffect(() => {
    void api<AdminSettingsResponse>("/api/admin/settings")
      .then(applyAdminSettings)
      .catch((reason) => {
        if (reason instanceof ApiRequestError && reason.status === 401)
          window.location.assign("/admin/login");
        else
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to load settings",
          );
      });
    void loadApplicationRelease();
    return () => releaseAbortRef.current?.abort();
  }, []);
  function handleSettingsTabKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
    event.preventDefault();
    const tabs: SettingsTab[] = ["workbench", "application"];
    const currentIndex = tabs.indexOf(activeSettingsTab);
    const nextTab = event.key === "Home"
      ? tabs[0]
      : event.key === "End"
        ? tabs[tabs.length - 1]
        : tabs[(currentIndex + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
    setActiveSettingsTab(nextTab);
    (nextTab === "workbench" ? workbenchTabRef : applicationTabRef).current?.focus();
  }
  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    window.location.assign("/");
  }
  async function copyAidpValue(
    value: string,
    inputRef: RefObject<HTMLInputElement | null>,
    label: string,
  ) {
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch {
      inputRef.current?.select();
      if (!document.execCommand("copy")) {
        setError(`Unable to copy the ${label}.`);
        return;
      }
    }
    setToast(`${label} copied.`);
  }
  async function saveSettings(section: "workbench" | "application") {
    setError("");
    const rotatesRegistrationCode = section === "application" && Boolean(registrationCode);
    if (rotatesRegistrationCode && !/^[A-Z]{4}-[0-9]{4}$/.test(registrationCode)) {
      setError("Enter four letters followed by four numbers.");
      return;
    }
    try {
      const result = await api<AdminSettingsResponse>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          ...(section === "workbench" && aidpUrl ? { aidp_url: aidpUrl } : {}),
          ...(rotatesRegistrationCode ? { registration_code: registrationCode } : {}),
        }),
      });
      applyAdminSettings(result);
      setRegistrationCode("");
      setToast(section === "application" ? "Application settings saved." : "AI Data Platform Workbench settings saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save settings");
    }
  }
  async function updateApplication() {
    const runningOperation = applicationRelease?.operation &&
      applicationUpdateStates.has(applicationRelease.operation.status)
      ? applicationRelease.operation.operation_id
      : undefined;
    const operationId = runningOperation || crypto.randomUUID();
    const previousRelease = applicationRelease?.current_release;
    const controller = new AbortController();
    releaseAbortRef.current?.abort();
    releaseAbortRef.current = controller;
    setReleaseBusy(true);
    setReleaseError("");
    setReleaseProgress({
      status: "pending",
      phase: "queued",
      message: "Waiting for the VM updater.",
    });
    try {
      const result = await pollRegistration({
        signal: controller.signal,
        deadlineMs: 30 * 60 * 1_000,
        request: (signal) => api<RegistrationResponse>("/api/admin/application/update", {
          method: "POST",
          body: JSON.stringify({ operation_id: operationId }),
          signal,
        }),
        onPending: setReleaseProgress,
      });
      const refreshed = await loadApplicationRelease();
      if (refreshed && previousRelease && refreshed.current_release !== previousRelease) {
        window.location.reload();
        return;
      }
      setToast(result.message || "Application release is current.");
    } catch (reason) {
      if (controller.signal.aborted) return;
      await loadApplicationRelease();
      setReleaseError(reason instanceof Error ? reason.message : "Unable to update the application");
    } finally {
      if (releaseAbortRef.current === controller) releaseAbortRef.current = null;
      setReleaseProgress(null);
      setReleaseBusy(false);
    }
  }
  return (
    <Shell
      onSignOut={logout}
      operatorUsername={adminSession?.operator_username || adminSession?.username}
    >
      <section className="settings-page" aria-busy={releaseBusy} inert={releaseBusy}>
        <div className="settings-heading">
          <h1>Settings</h1>
          <p>Review the lab configuration.</p>
        </div>
        <div className="settings-surface">
          <div className="settings-tabs" role="tablist" aria-label="Settings sections">
            <button
              ref={workbenchTabRef}
              id="settings-tab-workbench"
              type="button"
              className="settings-tab"
              role="tab"
              aria-selected={activeSettingsTab === "workbench"}
              aria-controls="settings-panel-workbench"
              tabIndex={activeSettingsTab === "workbench" ? 0 : -1}
              onClick={() => setActiveSettingsTab("workbench")}
              onKeyDown={handleSettingsTabKeyDown}
            >
              AI Data Platform Workbench
            </button>
            <button
              ref={applicationTabRef}
              id="settings-tab-application"
              type="button"
              className="settings-tab"
              role="tab"
              aria-selected={activeSettingsTab === "application"}
              aria-controls="settings-panel-application"
              tabIndex={activeSettingsTab === "application" ? 0 : -1}
              onClick={() => setActiveSettingsTab("application")}
              onKeyDown={handleSettingsTabKeyDown}
            >
              Application
            </button>
          </div>
          <section
            id="settings-panel-workbench"
            className="settings-panel"
            role="tabpanel"
            aria-labelledby="settings-tab-workbench"
            hidden={activeSettingsTab !== "workbench"}
          >
            <div className="settings-intro">
              <span className="settings-icon">
                <AdminLoginIcon />
              </span>
              <div>
                <strong>AI Data Platform Workbench</strong>
                <p>Review the service and open the workspace configured for these starter kits.</p>
              </div>
            </div>
            <label className="settings-field">
              AI Data Platform Workbench Service Endpoint
              <span className="settings-url-control">
                <input
                  ref={serviceEndpointRef}
                  value={aidpServiceEndpoint}
                  readOnly
                  spellCheck={false}
                  aria-label="AI Data Platform Workbench Service Endpoint"
                  placeholder="Not configured"
                />
                <button
                  type="button"
                  className="copy-url"
                  onClick={() => void copyAidpValue(aidpServiceEndpoint, serviceEndpointRef, "AI Data Platform Workbench Service Endpoint")}
                  disabled={!aidpServiceEndpoint}
                  aria-label="Copy AI Data Platform Workbench Service Endpoint"
                  title="Copy AI Data Platform Workbench Service Endpoint"
                >
                  <CopyIcon />
                </button>
              </span>
            </label>
            <label className="settings-field">
              AI Data Platform Workbench URL
              <span className="settings-url-control settings-url-control-actions">
                <input
                  ref={urlRef}
                  value={aidpUrl}
                  onChange={(event) => setAidpUrl(event.target.value)}
                  aria-label="AI Data Platform Workbench URL"
                  placeholder="Loading configuration…"
                />
                <button
                  type="button"
                  className="copy-url"
                  onClick={() => void copyAidpValue(aidpUrl, urlRef, "AI Data Platform Workbench URL")}
                  disabled={!aidpUrl}
                  aria-label="Copy AI Data Platform Workbench URL"
                  title="Copy AI Data Platform Workbench URL"
                >
                  <CopyIcon />
                </button>
                <a
                  className="copy-url open-url"
                  href={aidpUrl || undefined}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label="Open AI Data Platform Workbench"
                  title="Open AI Data Platform Workbench"
                  aria-disabled={!aidpUrl}
                  tabIndex={aidpUrl ? 0 : -1}
                >
                  <OpenExternalIcon />
                </a>
              </span>
            </label>
            <label className="settings-field">
              AI Data Platform Workbench OCID
              <span className="settings-url-control">
                <input
                  ref={platformIdRef}
                  value={aidpPlatformId}
                  readOnly
                  spellCheck={false}
                  aria-label="AI Data Platform Workbench OCID"
                  placeholder="Not configured"
                />
                <button
                  type="button"
                  className="copy-url"
                  onClick={() => void copyAidpValue(aidpPlatformId, platformIdRef, "AI Data Platform Workbench OCID")}
                  disabled={!aidpPlatformId}
                  aria-label="Copy AI Data Platform Workbench OCID"
                  title="Copy AI Data Platform Workbench OCID"
                >
                  <CopyIcon />
                </button>
              </span>
            </label>
            <div className="settings-actions">
              <button type="button" className="settings-save" onClick={() => void saveSettings("workbench")} disabled={!aidpUrl}>
                Save Settings
              </button>
            </div>
          </section>
          <section
            id="settings-panel-application"
            className="settings-panel"
            role="tabpanel"
            aria-labelledby="settings-tab-application"
            hidden={activeSettingsTab !== "application"}
          >
            <div className="settings-intro">
              <span className="settings-icon">
                <AdminLoginIcon />
              </span>
              <div>
                <strong>Application</strong>
                <p>Manage the application release, starter kit versions and participant access.</p>
              </div>
            </div>
            <ApplicationReleaseSettings
              release={applicationRelease}
              busy={releaseBusy}
              error={releaseError}
              onUpdate={() => void updateApplication()}
            />
            <ApplicationAccessSettings
              deploymentMode={deploymentMode}
              registrationCode={registrationCode}
              registrationCodeConfigured={registrationCodeConfigured}
              onRegistrationCodeChange={setRegistrationCode}
              onSave={() => void saveSettings("application")}
            />
          </section>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>
      <Toast message={toast} onDismiss={() => setToast("")} />
      {releaseBusy && (
        <ProvisioningOverlay
          phase={releaseProgress?.phase}
          message={releaseProgress?.message || "The VM is updating the application."}
          label="Updating application"
          indeterminate
        />
      )}
    </Shell>
  );
}

export function App() {
  if (window.location.pathname === "/admin/settings") return <AdminSettings />;
  if (window.location.pathname === "/admin/login")
    return <RegisterPage initialAdminLogin />;
  if (window.location.pathname === "/admin/users") return <AdminUsers />;
  return <RegisterPage />;
}
