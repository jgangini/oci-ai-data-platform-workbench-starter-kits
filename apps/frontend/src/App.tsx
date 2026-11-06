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
import { LoadingIndicator } from "./LoadingIndicator";
import { SearchableCombobox } from "./SearchableCombobox";

import { labAssignmentChanges } from "./labAssignments";

import {
  ApiRequestError,
  getOrCreateLabOperation,
  loadLabOperation,
  parseRetryAfter,
  persistLabOperation,
  pollRegistration,
  registrationProgress,
  type RegistrationPhase,
  type RegistrationPhaseValue,
  type RegistrationResponse,
} from "./registrationPoll";

type ApiError = { detail?: string | { message?: string } };
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
  operator_username: string;
  registration_code_configured: boolean;
  time_zone: string;
  time_zones: string[];
};
type PublicConfig = {
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
  updated_at?: string | null;
  kind: string;
  scope: "participant";
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
  kind: "question" | "save" | "delete" | "reset";
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
        <p className="sr-only">Loading...</p>
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
      (typeof body.detail === 'string' ? body.detail : body.detail?.message) || `Request failed (${response.status})`,
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
              Enter the participant details and select starter kits.
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
                        disabled={user.managed === false || !lab.available}
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
                      {user.managed !== false && <button
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
                      </button>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        {confirmingRemoval && (
          <p className="lab-manager-warning" role="alert">
            {changes.remove.length > 0 && <>Confirm removal of {changes.remove.length} {changes.remove.length === 1 ? "starter kit" : "starter kits"}. Only their jobs, tables, objects and workspace content will be deleted. </>}
          </p>
        )}
        {error && <p className="lab-manager-error" role="alert">{error}</p>}
        <footer>
          <button ref={closeRef} className="secondary" type="button" onClick={onClose}>
            {confirmingRemoval ? "Back" : "Cancel"}
          </button>
          <button type="button" disabled={!hasChanges || user.labs.length > 0 && !selectedLabIds.length} onClick={onSave}>
                  {confirmingRemoval ? "Confirm changes" : "Save"}
          </button>
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
    <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
      <path d="M16.412 4.412h-8.82a7.588 7.588 0 0 0-.008 15.176h8.828a7.588 7.588 0 0 0 0-15.176zm-.193 12.502H7.786a4.915 4.915 0 0 1 0-9.828h8.433a4.914 4.914 0 1 1 0 9.828z" />
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

function InstallIcon() {
  return <svg viewBox="0 0 36 36" fill="currentColor" aria-hidden="true">
    <path d="M30.92,8H26.55a1,1,0,0,0,0,2H31V30H5V10H9.38a1,1,0,0,0,0-2H5.08A2,2,0,0,0,3,10V30a2,2,0,0,0,2.08,2H30.92A2,2,0,0,0,33,30V10A2,2,0,0,0,30.92,8Z" />
    <path d="M10.3,18.87l7,6.89a1,1,0,0,0,1.4,0l7-6.89a1,1,0,0,0-1.4-1.43L19,22.65V4a1,1,0,0,0-2,0V22.65l-5.3-5.21a1,1,0,0,0-1.4,1.43Z" />
  </svg>;
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
  adminLoginVisible: boolean,
) {
  return {
    showAdminLogin: !configLoaded || adminLoginVisible,
    showAdminLink: configLoaded && !adminLoginVisible,
    showHomeLink: configLoaded && adminLoginVisible,
  };
}

function RegisterPage({
  initialAdminLogin = false,
}: {
  initialAdminLogin?: boolean;
}) {
  const publicConfig = usePublicConfig();
  const catalog = publicConfig?.labs ?? fallbackCatalog;
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
    setAdminLoginVisible(false);
    if (window.location.pathname === "/admin/login")
      window.history.replaceState(null, "", "/");
  };
  const accessView = registrationAccessView(
    configLoaded,
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
  const catalog = publicConfig?.labs ?? fallbackCatalog;
  const [users, setUsers] = useState<LabUser[]>([]);
  const [usersLoading, setUsersLoading] = useState(true);
  const usersRequestRef = useRef(0);
  const [search, setSearch] = useState("");
  const [query, setQuery] = useState("");
  const [error, setError] = useState("");
  const [tableError, setTableError] = useState("");
  const [message, setMessage] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createProgress, setCreateProgress] = useState<RegistrationResponse | null>(null);
  const [draft, setDraft] = useState<UserDraft>({ name: "", email: "", lab_ids: [] });
  const createAbortRef = useRef<AbortController | null>(null);
  const operationAbortRef = useRef<AbortController | null>(null);
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
  const [pendingDelete, setPendingDelete] = useState<LabUser | null>(null);
  const [deleteError, setDeleteError] = useState("");
  const [logoutOpen, setLogoutOpen] = useState(false);
  async function loadUsers() {
    const request = ++usersRequestRef.current;
    setUsersLoading(true);
    setTableError("");
    try {
      const loaded = (await api<{ users: LabUser[] }>("/api/admin/users")).users;
      if (request !== usersRequestRef.current) return;
      setUsers(loaded);
      return loaded;
    } catch (reason) {
      if (request !== usersRequestRef.current) return;
      if (reason instanceof ApiRequestError && reason.status === 401)
        window.location.assign("/admin/login");
      else
        setTableError(
          reason instanceof Error ? reason.message : "Unable to load users",
        );
    } finally {
      if (request === usersRequestRef.current) setUsersLoading(false);
    }
  }
  useEffect(() => {
    void loadUsers();
    return () => {
      usersRequestRef.current++;
      createAbortRef.current?.abort();
      operationAbortRef.current?.abort();
    };
  }, []);
  const visible = users.filter((user) =>
    `${user.name} ${user.email}`.toLowerCase().includes(query.toLowerCase()),
  );
  const labManagerUser = users.find((user) => user.id === labManagerUserId) ?? null;
  const pendingLabUpdate = Boolean(
    pendingLabAction?.kind === "redeploy" && pendingLabAction.lab.update_available,
  );
  async function logout() {
    await api("/api/admin/logout", { method: "POST" });
    window.location.assign("/");
  }
  async function createUser(event: FormEvent) {
    event.preventDefault();
    if (createAbortRef.current) return;
    if (!draft.lab_ids.length) {
      setError("Select at least one starter kit.");
      return;
    }
    setCreating(true);
    setCreateOpen(false);
    setCreateProgress({
      status: "pending",
      phase: "identity",
      message: "Preparing the participant account.",
    });
    setError("");
    setMessage("");
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
      setDraft({ name: "", email: "", lab_ids: [] });
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
    setDraft({ name: "", email: "", lab_ids: [] });
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
    if (!labManagerUser || operationAbortRef.current) return;
    const changes = labAssignmentChanges(
      labManagerUser.labs.map((lab) => lab.lab_id),
      labManagerUser.managed === false ? labManagerUser.labs.map(lab => lab.lab_id) : selectedLabIds,
    );
    if (labManagerUser.labs.length > 0 && !selectedLabIds.length) {
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
      setMessage(`Access updated for ${labManagerUser.email}.`);
    } catch (reason) {
      if (controller.signal.aborted) return;
      await loadUsers();
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

  return (
    <>
      <Shell
        onSignOut={() => setLogoutOpen(true)}
        operatorUsername={adminSession?.operator_username || adminSession?.username}
      >
        <section className="admin" aria-busy={operating || creating} inert={operating || creating}>
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
                  onClick={() => void loadUsers()}
                  aria-label="Refresh users"
                  title="Refresh users"
                >
                  <RefreshIcon />
                </button>
              </div>
            </div>
            <div className="table-wrap">
              <table aria-busy={usersLoading}>
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
                  {usersLoading ? (
                    <tr><td colSpan={6}><LoadingIndicator label="Loading users…" /></td></tr>
                  ) : tableError ? (
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
                              <strong>{user.is_aidp_admin ? "AIDP administrator" : "Existing OCI user"}</strong>
                              <small>Identity domain account</small>
                            </span>
                          ) : (
                            <span>
                              <strong>{user.labs.length} {user.labs.length === 1 ? "starter kit" : "starter kits"}</strong>
                              {user.labs.length > 0 && <small>
                                {user.labs.every((lab) => lab.phase === "active")
                                  ? "All active"
                                  : `${user.labs.filter((lab) => lab.phase === "active").length} active`}
                              </small>}
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
                          {user.managed !== false && (
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
                          )}
                        </span>
                      </td>
                    </tr>
                    ))
                  )}
                  {!tableError && !visible.length && !usersLoading && (
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
    </>
  );
}

function SettingsRegistrationCodeField({
  value,
  configured,
  onChange,
  busy,
  action,
}: {
  value: string;
  configured: boolean;
  onChange: (value: string) => void;
  busy: boolean;
  action: ReactNode;
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
    <fieldset className="registration-code settings-registration-code" disabled={busy} aria-busy={busy}>
      <legend>Lab registration code</legend>
      <div className="settings-registration-controls">
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
        {action}
      </div>
      <span id="registration-code-settings-help" className="settings-help">
        {configured
          ? "For security, the current code is not displayed. Enter a new AAAA-0000 code to replace it."
          : "Enter an AAAA-0000 code to enable participant registration."}
      </span>
    </fieldset>
  );
}

function RegistrationAccessSettings({
  registrationCode,
  registrationCodeConfigured,
  onRegistrationCodeChange,
  onSave,
  busy,
}: {
  registrationCode: string;
  registrationCodeConfigured: boolean;
  onRegistrationCodeChange: (value: string) => void;
  onSave: () => void;
  busy: boolean;
}) {
  return (
    <SettingsRegistrationCodeField
      value={registrationCode}
      configured={registrationCodeConfigured}
      onChange={onRegistrationCodeChange}
      busy={busy}
      action={
        <button
          type="button"
          className="settings-save"
          onClick={onSave}
          disabled={busy || !/^[A-Z]{4}-[0-9]{4}$/.test(registrationCode)}
          aria-haspopup="dialog"
        >
          Save Code
        </button>
      }
    />
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

function packageVersionDate(value: string | null | undefined, timeZone: string) {
  if (!value) return "Not available";
  try {
    const parts = new Intl.DateTimeFormat("en", { year: "numeric", month: "2-digit", day: "2-digit", timeZone }).formatToParts(new Date(value));
    return ["year", "month", "day"].map(type => parts.find(part => part.type === type)?.value).join("-");
  } catch { return "Not available"; }
}

function packageVersionTimestamp(value: string | null | undefined) {
  const timestamp = Date.parse(value || "");
  return Number.isFinite(timestamp) ? timestamp : -Infinity;
}

const settingsUpdatePositionKey = "aidp-settings-update-position";

function ApplicationReleaseSettings({
  release,
  busy,
  error,
  showOperationMessage = true,
  displayTimeZone = "UTC",
}: {
  release: AdminApplicationRelease | null;
  busy: boolean;
  error: string;
  showOperationMessage?: boolean;
  displayTimeZone?: string;
}) {
  const operationRunning = Boolean(
    release?.operation && applicationUpdateStates.has(release.operation.status),
  );
  const operationMessage = showOperationMessage || operationRunning || release?.operation?.status === "failed"
    ? release?.operation?.message || (operationRunning ? "Updating the application."
      : release?.operation?.status === "failed" ? "The application update failed." : "")
    : "";
  const noticeMessage = error || operationMessage || release?.update_check_error;
  const noticeFailed = Boolean(error || operationMessage && release?.operation?.status === "failed");
  const packages = [...(release?.packages || [])].sort((a, b) =>
    packageVersionTimestamp(b.updated_at) - packageVersionTimestamp(a.updated_at) ||
    a.display_name.localeCompare(b.display_name, "en") || a.package_id.localeCompare(b.package_id, "en"));
  return (
    <section className="application-release application-version" aria-busy={busy}>
      <header className="application-release-heading">
        <div>
          <p className="eyebrow">GitHub release</p>
          <h2>Application version</h2>
          <p>Update the VM in place from the latest immutable release without reinstalling it.</p>
        </div>
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
          {noticeMessage && (
            <p
              className={noticeFailed ? "release-operation error" : operationMessage ? "release-operation" : "release-warning"}
              role={noticeFailed ? "alert" : "status"}
              aria-live="polite"
            >
              {noticeMessage}
            </p>
          )}
          {!release.updater_available && (
            <p className="settings-help">In-place updates are enabled only on the deployed application VM.</p>
          )}
          <p className="settings-help release-table-hint" id="release-table-hint">Scroll horizontally to view all columns.</p>
          <div className="release-packages-wrap" role="region" aria-label="Starter kit versions" aria-describedby="release-table-hint" tabIndex={0}>
            <table className="release-packages">
              <thead>
                <tr>
                  <th scope="col">Starter kit</th>
                  <th scope="col">Scope</th>
                  <th scope="col">Version</th>
                  <th scope="col">Updated</th>
                </tr>
              </thead>
              <tbody>
                {packages.map((item) => (
                  <tr key={item.package_id}>
                    <td><strong>{item.display_name}</strong></td>
                    <td>Participant</td>
                    <td>{item.bundled_version}</td>
                    <td>{item.updated_at ? <time dateTime={item.updated_at} title={`Last change to this package version · ${displayTimeZone}`}>{packageVersionDate(item.updated_at, displayTimeZone)}</time> : "Not available"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      ) : error ? (
        <p className="release-operation error" role="alert">{error}</p>
      ) : (
        <LoadingIndicator label="Loading release metadata…" />
      )}
    </section>
  );
}

function AdminSettings() {
  const adminSession = useAdminSession();
  type SettingsTab = "workbench" | "application";
  const [activeSettingsTab, setActiveSettingsTab] = useState<SettingsTab>(window.location.hash === "#application" ? "application" : "workbench");
  useEffect(() => {
    const syncTab = () => setActiveSettingsTab(window.location.hash === "#application" ? "application" : "workbench");
    window.addEventListener("hashchange", syncTab);
    return () => window.removeEventListener("hashchange", syncTab);
  }, []);
  const [updatePosition] = useState(() => {
    try { return JSON.parse(window.sessionStorage.getItem(settingsUpdatePositionKey) || "null"); }
    catch { return null; }
  });
  const updatePositionRestored = useRef(false);
  const [aidpServiceEndpoint, setAidpServiceEndpoint] = useState("");
  const [aidpUrl, setAidpUrl] = useState("");
  const [settingsLoading, setSettingsLoading] = useState(true);
  const [aidpPlatformId, setAidpPlatformId] = useState("");
  const [registrationCode, setRegistrationCode] = useState("");
  const [registrationCodeConfigured, setRegistrationCodeConfigured] = useState(false);
  const [confirmRegistrationSave, setConfirmRegistrationSave] = useState(false);
  const [settingsSaving, setSettingsSaving] = useState(false);
  const registrationCodeToSave = useRef<string | null>(null);
  const settingsSaveRef = useRef(false);
  const [timeZone, setTimeZone] = useState('America/Bogota');
  const [savedTimeZone, setSavedTimeZone] = useState('America/Bogota');
  const [timeZones, setTimeZones] = useState<{ value: string; label: string }[]>([]);
  const [confirmTimeZoneSave, setConfirmTimeZoneSave] = useState(false);
  const timeZoneToSave = useRef<{ value: string; label: string } | null>(null);
  const selectedTimeZone = timeZones.find(zone => zone.value === timeZone);
  const [applicationRelease, setApplicationRelease] = useState<AdminApplicationRelease | null>(null);
  const [expiredReleaseOperationId, setExpiredReleaseOperationId] = useState("");
  const successfulReleaseOperationId = applicationRelease?.operation && ["succeeded", "up_to_date"].includes(applicationRelease.operation.status) ? applicationRelease.operation.operation_id : "";
  useEffect(() => {
    if (!successfulReleaseOperationId) return;
    const timeout = window.setTimeout(() => setExpiredReleaseOperationId(successfulReleaseOperationId), 8_000);
    return () => window.clearTimeout(timeout);
  }, [successfulReleaseOperationId]);
  const [releaseBusy, setReleaseBusy] = useState(false);
  const [confirmReleaseUpdate, setConfirmReleaseUpdate] = useState(false);
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
  function applyAdminSettings(result: AdminSettingsResponse, section?: "registration" | "timezone") {
    setAidpServiceEndpoint(result.aidp_service_endpoint);
    setAidpUrl(result.aidp_url);
    setAidpPlatformId(result.aidp_platform_id);
    setRegistrationCodeConfigured(result.registration_code_configured);
    if (!section || section === "timezone") setTimeZone(result.time_zone);
    setSavedTimeZone(result.time_zone);
    setTimeZones([...new Set([result.time_zone, ...result.time_zones])].flatMap(value => {
      try {
        const offset = new Intl.DateTimeFormat('en', { timeZone: value, timeZoneName: 'longOffset' }).formatToParts(new Date()).find(part => part.type === 'timeZoneName')?.value.replace('GMT', 'UTC').replace(/^UTC$/, 'UTC+00:00');
        return [{ value, label: `${value.replaceAll('_', ' ')} (${offset})` }];
      } catch { return []; }
    }));
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
    if (settingsLoading || !applicationRelease || activeSettingsTab !== "application" || !updatePosition || updatePositionRestored.current) return;
    try {
      if (updatePosition.release !== applicationRelease.current_release || !Number.isFinite(updatePosition.y) || updatePosition.y < 0) {
        updatePositionRestored.current = true;
        window.sessionStorage.removeItem(settingsUpdatePositionKey);
        return;
      }
      const timer = window.setTimeout(() => {
        window.scrollTo(0, updatePosition.y);
        updatePositionRestored.current = true;
        try { window.sessionStorage.removeItem(settingsUpdatePositionKey); } catch { /* Optional browser storage. */ }
      }, 0);
      return () => window.clearTimeout(timer);
    } catch { /* Browser storage is optional; the URL still restores the selected tab. */ }
  }, [settingsLoading, applicationRelease?.current_release, activeSettingsTab, updatePosition]);
  useEffect(() => {
    void api<AdminSettingsResponse>("/api/admin/settings")
      .then(result => applyAdminSettings(result))
      .catch((reason) => {
        if (reason instanceof ApiRequestError && reason.status === 401)
          window.location.assign("/admin/login");
        else
          setError(
            reason instanceof Error
              ? reason.message
              : "Unable to load settings",
          );
      }).finally(() => setSettingsLoading(false));
    void loadApplicationRelease();
    return () => { releaseAbortRef.current?.abort(); };
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
    selectSettingsTab(nextTab);
    (nextTab === "workbench" ? workbenchTabRef : applicationTabRef).current?.focus();
  }
  function selectSettingsTab(tab: SettingsTab) {
    window.location.hash = tab;
    setActiveSettingsTab(tab);
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
  function requestRegistrationSave() {
    if (settingsSaveRef.current || settingsLoading) return;
    if (!/^[A-Z]{4}-[0-9]{4}$/.test(registrationCode)) {
      setError("Enter four letters followed by four numbers.");
      return;
    }
    setError("");
    registrationCodeToSave.current = registrationCode;
    setConfirmRegistrationSave(true);
  }
  function requestTimeZoneSave() {
    if (settingsSaveRef.current || settingsLoading || !selectedTimeZone || timeZone === savedTimeZone) return;
    setError("");
    timeZoneToSave.current = selectedTimeZone;
    setConfirmTimeZoneSave(true);
  }
  async function saveSettings(section: "registration" | "timezone", value: string) {
    if (settingsSaveRef.current) return;
    setError("");
    const rotatesRegistrationCode = section === "registration";
    if (rotatesRegistrationCode && !/^[A-Z]{4}-[0-9]{4}$/.test(value)) {
      setError("Enter four letters followed by four numbers.");
      return;
    }
    settingsSaveRef.current = true;
    setSettingsSaving(true);
    try {
      const result = await api<AdminSettingsResponse>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          ...(rotatesRegistrationCode ? { registration_code: value } : { time_zone: value }),
        }),
      });
      applyAdminSettings(result, section);
      if (rotatesRegistrationCode) setRegistrationCode(current => current === value ? "" : current);
      setToast(section === "registration" ? "Registration code saved." : "Time zone saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save settings");
    } finally {
      settingsSaveRef.current = false;
      setSettingsSaving(false);
    }
  }
  async function updateApplication() {
    setConfirmReleaseUpdate(false);
    if (!applicationRelease?.updater_available || releaseAbortRef.current) return;
    const runningOperation = applicationRelease?.operation &&
      applicationUpdateStates.has(applicationRelease.operation.status)
      ? applicationRelease.operation.operation_id
      : undefined;
    const operationId = runningOperation || crypto.randomUUID();
    const previousRelease = applicationRelease?.current_release;
    const controller = new AbortController();
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
        window.location.hash = "application";
        try {
          window.sessionStorage.setItem(settingsUpdatePositionKey, JSON.stringify({ release: refreshed.current_release, y: window.scrollY }));
        } catch { /* Keep navigation working when browser storage is unavailable. */ }
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
              onClick={() => selectSettingsTab("workbench")}
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
              onClick={() => selectSettingsTab("application")}
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
                {settingsLoading ? <LoadingIndicator label="Loading configuration…" inline /> : <input
                  ref={urlRef}
                  value={aidpUrl}
                  readOnly
                  spellCheck={false}
                  aria-label="AI Data Platform Workbench URL"
                  placeholder="Not configured"
                />}
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
            <RegistrationAccessSettings
              registrationCode={registrationCode}
              registrationCodeConfigured={registrationCodeConfigured}
              onRegistrationCodeChange={setRegistrationCode}
              onSave={requestRegistrationSave}
              busy={settingsSaving}
            />
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
                <p>Manage the application release and starter kit versions.</p>
              </div>
              {applicationRelease && (applicationRelease.update_available || (applicationRelease.operation && applicationUpdateStates.has(applicationRelease.operation.status))) &&
                <button type="button" className="settings-save application-update" disabled={!applicationRelease.updater_available || releaseBusy} onClick={() => setConfirmReleaseUpdate(true)}>
                  <InstallIcon />{releaseBusy ? "Updating…" : "Update"}
                </button>}
            </div>
            <ApplicationReleaseSettings
              release={applicationRelease}
              showOperationMessage={!successfulReleaseOperationId || expiredReleaseOperationId !== successfulReleaseOperationId}
              displayTimeZone={savedTimeZone || "UTC"}
              busy={releaseBusy}
              error={releaseError}
            />
            <div className="settings-time-zone-controls">
              <SearchableCombobox className="settings-field time-zone-picker" label="Display time zone"
                placeholder="Search city or UTC offset" value={timeZone} options={timeZones}
                disabled={settingsLoading || settingsSaving} onChange={setTimeZone} />
              <button type="button" className="settings-save" aria-haspopup="dialog" disabled={settingsLoading || settingsSaving || !selectedTimeZone || timeZone === savedTimeZone} onClick={requestTimeZoneSave}>Save time zone</button>
            </div>
          </section>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>
      <Toast message={toast} onDismiss={() => setToast("")} />
      <ConfirmModal
        open={confirmRegistrationSave}
        kind="question"
        title="Save registration code?"
        description={registrationCodeConfigured
          ? "Replace the current registration code? The previous code will stop working for new registrations. Existing participants keep their access."
          : "Save this code to enable participant registration? Share it only with the people you want to register for the lab."}
        confirmLabel="Save Code"
        onClose={() => {
          registrationCodeToSave.current = null;
          setConfirmRegistrationSave(false);
        }}
        onConfirm={() => {
          const code = registrationCodeToSave.current;
          registrationCodeToSave.current = null;
          setConfirmRegistrationSave(false);
          if (code) void saveSettings("registration", code);
        }}
      />
      <ConfirmModal
        open={confirmTimeZoneSave}
        kind="save"
        title="Save time zone?"
        description={`Display dates and times in ${timeZoneToSave.current?.label || "the selected time zone"}?`}
        confirmLabel="Save"
        onClose={() => {
          timeZoneToSave.current = null;
          setConfirmTimeZoneSave(false);
        }}
        onConfirm={() => {
          const zone = timeZoneToSave.current;
          timeZoneToSave.current = null;
          setConfirmTimeZoneSave(false);
          if (zone) void saveSettings("timezone", zone.value);
        }}
      />
      <ConfirmModal
        open={confirmReleaseUpdate}
        kind="question"
        title="Update application?"
        description="Update this VM to the latest published release and its bundled kit versions? Existing participant installations remain unchanged until their Update or Redeploy action is used."
        confirmLabel="Accept"
        onClose={() => setConfirmReleaseUpdate(false)}
        onConfirm={() => void updateApplication()}
      />
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
