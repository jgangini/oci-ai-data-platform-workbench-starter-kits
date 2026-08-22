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

type ApiError = { detail?: string };
type LabUser = {
  id: string;
  name: string;
  email: string;
  status: "active" | "pending";
  labs: AssignedLab[];
  active: boolean;
  managed?: boolean;
  participant_code?: number | null;
};

type AssignedLab = {
  lab_id: string;
  pack_version: string;
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
  aidp_url: string;
  aidp_platform_id: string;
  compute_name: string;
  jdbc_url: string;
  jdbc_authentication: string;
  jdbc_driver_available: boolean;
  governance_gateway_url: string;
  governance_control_bucket: string;
  registration_code_configured: boolean;
};
const fallbackCatalog: CatalogLab[] = [
  { lab_id: "banking", display_name: "Banking", description: "Explore customer accounts, branches and transactions through a governed medallion pipeline.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "telecommunications", display_name: "Telecommunications", description: "Analyze subscribers, plans, network sites and usage events for service and network insights.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "telco_lineage", display_name: "Telco Customer 360 Lineage", description: "Test end-to-end data lineage for prepaid, postpaid and home services, from Landing through Gold with entity and column relationships.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "retail", display_name: "Retail", description: "Transform customers, products, orders and order items into sales and customer analytics.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "healthcare", display_name: "Healthcare", description: "Prepare patients, providers, appointments and encounters for operational healthcare analysis.", pack_version: "2.0.0", status: "available", available: true },
  { lab_id: "agent", display_name: "Data Governance Agent", description: "Use an editable participant-scoped agent for catalog inventory, lineage and governed metrics.", pack_version: "1.0.0", status: "available", available: true },
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
    document.body.style.overflow = "hidden";
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
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
}: {
  phase?: RegistrationPhaseValue;
  message?: string;
}) {
  const phaseId = useId();
  const progress = registrationProgress(phase);
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
          {registrationPhaseLabel(phase)}
        </p>
        <div
          className="registration-progress-track"
          role="progressbar"
          aria-labelledby={phaseId}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress.percent}
          aria-valuetext={`${progress.percent}% completed, step ${progress.step} of ${progress.total}: ${registrationPhaseLabel(phase)}`}
        >
          <span style={{ width: `${progress.percent}%` }} />
        </div>
        <div className="registration-progress-meta">
          <strong>{progress.percent}% completed</strong>
          <span>
            Step {progress.step} of {progress.total}
          </span>
        </div>
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
              Enter the participant details and select one or more initial laboratories.
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
              <caption className="sr-only">Select initial laboratories</caption>
              <thead>
                <tr>
                  <th scope="col">Select</th>
                  <th scope="col">Laboratory</th>
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
                          aria-label={`Select ${lab.display_name} laboratory`}
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
            <p className="eyebrow">Participant laboratories</p>
            <h2 id={titleId}>Manage laboratories</h2>
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
                <th scope="col">Laboratory</th>
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
                    <td>{installed?.pack_version ?? lab.pack_version}</td>
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
                        aria-label={`Redeploy ${lab.display_name} for ${user.email}`}
                        title={installed ? "Redeploy lab" : "Assign the lab before redeploying"}
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
            Confirm removal of {changes.remove.length} {changes.remove.length === 1 ? "laboratory" : "laboratories"}. Only their jobs, tables, objects and workspace content will be deleted.
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

function useLabCatalog() {
  const [catalog, setCatalog] = useState<CatalogLab[]>(fallbackCatalog);
  useEffect(() => {
    void api<{ labs: CatalogLab[] }>("/api/config")
      .then(({ labs }) => setCatalog(labs))
      .catch(() => undefined);
  }, []);
  return catalog;
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
    <a className="brand" href="/" aria-label="AI Data Platform Workbench home">
      <span className="brand-mark">
        <OracleMark />
      </span>
      <span>
        <strong>AI Data Platform Workbench</strong>
        <small>Cloud Migration Lab</small>
      </span>
    </a>
  );
}

function Shell({
  children,
  adminLink = true,
  onSignOut,
}: {
  children: React.ReactNode;
  adminLink?: boolean;
  onSignOut?: () => void;
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
            ) : (
              adminLink && (
                <a
                  className="admin-link"
                  href="/admin/login"
                  aria-label="Administrator login"
                  title="Administrator login"
                >
                  <AdminLoginIcon />
                </a>
              )
            )}
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

function RegisterPage() {
  const catalog = useLabCatalog();
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
      ? "Choose laboratories"
      : labIds.length === 1
        ? labLabel(catalog, labIds[0])
        : `${labIds.length} laboratories selected`;

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
        message: result.message || "Your lab account is ready.",
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

  return (
    <Shell>
      <section className="hero-grid">
        <div className="hero-copy">
          <p className="eyebrow">
            Structured data · notebooks · medallion architecture
          </p>
          <h1>Build in a governed AI data workspace.</h1>
          <p className="lede">
            Register for this temporary lab to work with landing, bronze, silver
            and gold data layers in Oracle AI Data Platform.
          </p>
          <ol className="steps">
            <li className="step-card">
              <span className="step-number">01 · Identity</span>
              <strong>
                <span>Set up</span>
                <span>your account</span>
              </strong>
              <small>Register with your name, email and lab code.</small>
            </li>
            <li className="step-card">
              <span className="step-number">02 · Workbench</span>
              <strong>
                <span>Open AI Data</span>
                <span>Platform</span>
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
        <form
          className="card"
          onSubmit={submit}
          aria-busy={state.status === "processing"}
        >
          <div>
            <p className="eyebrow">Lab access</p>
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
              Laboratories
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
              <h2 id="registration-ready-title">Your lab account is ready</h2>
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
                  Open AI Data Platform
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

function AdminLogin() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
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
    <Shell adminLink={false}>
      <section className="centered">
        <form className="card narrow" onSubmit={submit}>
          <h1>Login</h1>
          <label>
            Username
            <input
              autoComplete="username"
              value={username}
              onChange={(e) => setUsername(e.target.value)}
              required
            />
          </label>
          <label>
            Password
            <input
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
            />
          </label>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
          <button>Sign in</button>
          <a className="quiet-link" href="/">
            Return to registration
          </a>
        </form>
      </section>
    </Shell>
  );
}

function AdminUsers() {
  const catalog = useLabCatalog();
  const [users, setUsers] = useState<LabUser[]>([]);
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
  useEffect(() => {
    void loadUsers();
    return () => {
      createAbortRef.current?.abort();
      operationAbortRef.current?.abort();
    };
  }, []);
  const visible = users.filter((user) =>
    `${user.name} ${user.email}`.toLowerCase().includes(query.toLowerCase()),
  );
  const labManagerUser = users.find((user) => user.id === labManagerUserId) ?? null;
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
      setLabManagerError("A participant must keep at least one laboratory.");
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
      setMessage(`Laboratories updated for ${labManagerUser.email}.`);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const loaded = await loadUsers();
      const refreshed = loaded?.find((user) => user.id === labManagerUser.id);
      if (refreshed) setSelectedLabIds(refreshed.labs.map((lab) => lab.lab_id));
      setConfirmingLabRemoval(false);
      setLabManagerError(reason instanceof Error ? reason.message : "Unable to update the laboratories.");
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
      message: `${action.kind === "redeploy" ? "Reinstalling" : "Removing"} the selected lab resources.`,
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
      <Shell adminLink={false} onSignOut={() => setLogoutOpen(true)}>
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
              <table>
                <thead>
                  <tr>
                    <th>Name</th>
                    <th>Email</th>
                    <th>Status</th>
                    <th>Laboratories</th>
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
                          <span>
                            <strong>{user.labs.length} {user.labs.length === 1 ? "laboratory" : "laboratories"}</strong>
                            <small>
                              {user.labs.every((lab) => lab.phase === "active")
                                ? "All active"
                                : `${user.labs.filter((lab) => lab.phase === "active").length} active`}
                            </small>
                          </span>
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
                            aria-label={`Manage laboratories for ${user.email}`}
                            title="Manage laboratories"
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
      <ConfirmModal
        open={Boolean(pendingLabAction) && !operating}
        kind={pendingLabAction?.kind === "remove" ? "delete" : "reset"}
        title={pendingLabAction?.kind === "remove" ? "Remove laboratory?" : "Redeploy laboratory?"}
        description={pendingLabAction?.kind === "redeploy" && pendingLabAction.lab.lab_id === "agent"
          ? `Reinstall ${labLabel(catalog, "agent")} for ${pendingLabAction.user.email}. This replaces the participant's Agent customizations. Data laboratories and Identity access are preserved.`
          : `${pendingLabAction?.kind === "remove" ? "Remove" : "Reinstall"} only ${pendingLabAction ? labLabel(catalog, pendingLabAction.lab.lab_id) : "this lab"} for ${pendingLabAction?.user.email ?? "this participant"}. Other laboratories and Identity access are preserved.`}
        error={operationError}
        confirmLabel={pendingLabAction?.kind === "remove" ? "Remove lab" : "Redeploy lab"}
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
            operationProgress?.message || "Updating the participant's laboratory."
          }
        />
      )}
    </>
  );
}

function AdminSettings() {
  const [aidpUrl, setAidpUrl] = useState("");
  const [aidpPlatformId, setAidpPlatformId] = useState("");
  const [computeName, setComputeName] = useState("");
  const [jdbcUrl, setJdbcUrl] = useState("");
  const [jdbcAuthentication, setJdbcAuthentication] = useState("");
  const [jdbcDriverAvailable, setJdbcDriverAvailable] = useState(false);
  const [governanceGatewayUrl, setGovernanceGatewayUrl] = useState("");
  const [governanceControlBucket, setGovernanceControlBucket] = useState("");
  const [registrationCode, setRegistrationCode] = useState("");
  const [registrationCodeConfigured, setRegistrationCodeConfigured] = useState(false);
  const [uploadingDriver, setUploadingDriver] = useState(false);
  const [error, setError] = useState("");
  const [toast, setToast] = useState("");
  const urlRef = useRef<HTMLInputElement>(null);
  const platformIdRef = useRef<HTMLInputElement>(null);
  const jdbcUrlRef = useRef<HTMLInputElement>(null);
  const gatewayUrlRef = useRef<HTMLInputElement>(null);
  const driverInputRef = useRef<HTMLInputElement>(null);
  useEffect(() => {
    void api<AdminSettingsResponse>("/api/admin/settings")
      .then((result) => {
        setAidpUrl(result.aidp_url);
        setAidpPlatformId(result.aidp_platform_id);
        setComputeName(result.compute_name);
        setJdbcUrl(result.jdbc_url);
        setJdbcAuthentication(result.jdbc_authentication);
        setJdbcDriverAvailable(result.jdbc_driver_available);
        setGovernanceGatewayUrl(result.governance_gateway_url);
        setGovernanceControlBucket(result.governance_control_bucket);
        setRegistrationCodeConfigured(result.registration_code_configured);
      })
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
  }, []);
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
  async function saveSettings() {
    setError("");
    const rotatesRegistrationCode = Boolean(registrationCode);
    try {
      const result = await api<AdminSettingsResponse>("/api/admin/settings", {
        method: "PUT",
        body: JSON.stringify({
          ...(aidpUrl ? { aidp_url: aidpUrl } : {}),
          ...(rotatesRegistrationCode ? { registration_code: registrationCode } : {}),
        }),
      });
      setAidpUrl(result.aidp_url);
      setAidpPlatformId(result.aidp_platform_id);
      setComputeName(result.compute_name);
      setJdbcUrl(result.jdbc_url);
      setJdbcAuthentication(result.jdbc_authentication);
      setJdbcDriverAvailable(result.jdbc_driver_available);
      setGovernanceGatewayUrl(result.governance_gateway_url);
      setGovernanceControlBucket(result.governance_control_bucket);
      setRegistrationCode("");
      setRegistrationCodeConfigured(result.registration_code_configured);
      setToast(rotatesRegistrationCode ? "Lab settings saved. Registration code updated." : "AI Data Platform URL saved.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to save settings");
    }
  }
  async function uploadJdbcDriver(file?: File) {
    if (!file) return;
    setError("");
    if (!file.name.toLowerCase().endsWith(".zip")) {
      setError("Select the ZIP downloaded from AIDP Workbench.");
      return;
    }
    setUploadingDriver(true);
    try {
      const result = await api<{ jdbc_driver_available: boolean }>(
        "/api/admin/aidp/jdbc-driver",
        {
          method: "PUT",
          headers: { "Content-Type": "application/zip" },
          body: file,
        },
      );
      setJdbcDriverAvailable(result.jdbc_driver_available);
      setToast("AIDP JDBC driver synchronized to oci_control and the lab VM.");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Unable to store the JDBC driver");
    } finally {
      setUploadingDriver(false);
      if (driverInputRef.current) driverInputRef.current.value = "";
    }
  }
  return (
    <Shell adminLink={false} onSignOut={logout}>
      <section className="settings-page">
        <div className="settings-heading">
          <h1>Settings</h1>
          <p>Review the lab configuration.</p>
        </div>
        <div className="settings-surface">
          <div className="settings-tabs">
            <span>Application</span>
          </div>
          <div className="settings-intro">
            <span className="settings-icon">
              <AdminLoginIcon />
            </span>
            <div>
              <strong>AI Data Platform</strong>
              <p>Open the workspace configured for this lab.</p>
            </div>
          </div>
          <label className="settings-field">
            AI Data Platform URL
            <span className="settings-url-control">
              <input
                ref={urlRef}
                value={aidpUrl}
                onChange={(event) => setAidpUrl(event.target.value)}
                aria-label="AI Data Platform URL"
                placeholder="Loading configuration…"
              />
              <button
                type="button"
                className="copy-url"
                onClick={() =>
                  void copyAidpValue(aidpUrl, urlRef, "AI Data Platform URL")
                }
                disabled={!aidpUrl}
                aria-label="Copy AI Data Platform URL"
                title="Copy AI Data Platform URL"
              >
                <CopyIcon />
              </button>
            </span>
            {aidpUrl && (
              <a
                className="settings-link"
                href={aidpUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                Open AI Data Platform
              </a>
            )}
            {jdbcDriverAvailable ? (
              <span className="settings-driver-actions">
                <a className="settings-link" href="/api/admin/aidp/jdbc-driver" download>
                  Download AIDP JDBC driver
                </a>
                <button type="button" className="quiet-link" onClick={() => driverInputRef.current?.click()} disabled={uploadingDriver}>
                  Replace driver
                </button>
              </span>
            ) : (
              <span className="settings-driver-actions">
                <span className="settings-help">Download the driver from AIDP Workbench once, then store it on this lab VM.</span>
                <button type="button" className="secondary" onClick={() => driverInputRef.current?.click()} disabled={uploadingDriver}>
                  {uploadingDriver ? "Uploading…" : "Import JDBC driver"}
                </button>
              </span>
            )}
            <input
              ref={driverInputRef}
              className="sr-only"
              type="file"
              accept=".zip,application/zip"
              aria-label="Import AIDP JDBC driver ZIP"
              onChange={(event) => void uploadJdbcDriver(event.target.files?.[0])}
            />
          </label>
          <label className="settings-field">
            AI Data Platform OCID
            <span className="settings-url-control">
              <input
                ref={platformIdRef}
                value={aidpPlatformId}
                readOnly
                spellCheck={false}
                aria-label="AI Data Platform OCID"
                placeholder="Not configured"
              />
              <button
                type="button"
                className="copy-url"
                onClick={() =>
                  void copyAidpValue(
                    aidpPlatformId,
                    platformIdRef,
                    "AI Data Platform OCID",
                  )
                }
                disabled={!aidpPlatformId}
                aria-label="Copy AI Data Platform OCID"
                title="Copy AI Data Platform OCID"
              >
                <CopyIcon />
              </button>
            </span>
          </label>
          <div className="settings-intro settings-connection-intro">
            <div>
              <strong>Connection access</strong>
              <p>Share these non-secret endpoints with authorized lab users.</p>
            </div>
          </div>
          <label className="settings-field">
            Shared compute
            <input value={computeName} readOnly spellCheck={false} aria-label="Shared compute" placeholder="Not available" />
          </label>
          <label className="settings-field">
            JDBC URL
            <span className="settings-url-control">
              <input ref={jdbcUrlRef} value={jdbcUrl} readOnly spellCheck={false} aria-label="JDBC URL" placeholder="Not available" />
              <button type="button" className="copy-url" onClick={() => void copyAidpValue(jdbcUrl, jdbcUrlRef, "JDBC URL")} disabled={!jdbcUrl} aria-label="Copy JDBC URL" title="Copy JDBC URL">
                <CopyIcon />
              </button>
            </span>
          </label>
          <label className="settings-field">
            Authentication
            <input value={jdbcAuthentication} readOnly spellCheck={false} aria-label="JDBC authentication" />
          </label>
          <label className="settings-field">
            AI Data Governance Gateway URL
            <span className="settings-url-control">
              <input ref={gatewayUrlRef} value={governanceGatewayUrl} readOnly spellCheck={false} aria-label="AI Data Governance Gateway URL" placeholder="Not installed" />
              <button type="button" className="copy-url" onClick={() => void copyAidpValue(governanceGatewayUrl, gatewayUrlRef, "AI Data Governance Gateway URL")} disabled={!governanceGatewayUrl} aria-label="Copy AI Data Governance Gateway URL" title="Copy AI Data Governance Gateway URL">
                <CopyIcon />
              </button>
            </span>
          </label>
          <label className="settings-field">
            Governance control bucket
            <input value={governanceControlBucket} readOnly spellCheck={false} aria-label="Governance control bucket" placeholder="Not installed" />
            <span className="settings-help">Stores the oci_control Delta tables and the private JDBC driver under separate prefixes.</span>
          </label>
          <label className="settings-field">
            Lab registration code
            <input
              type="text"
              value={registrationCode}
              onChange={(event) => setRegistrationCode(event.target.value.toUpperCase())}
              aria-describedby="registration-code-settings-help"
              aria-label="Lab registration code"
              placeholder={registrationCodeConfigured ? "Configured — enter a new code to replace it" : "AAAA-0000"}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={9}
            />
            <span id="registration-code-settings-help" className="settings-help">
              {registrationCodeConfigured
                ? "For security, the current code is not displayed. Enter a new AAAA-0000 code to replace it."
                : "Enter an AAAA-0000 code to enable participant registration."}
            </span>
          </label>
          <button type="button" className="settings-save" onClick={() => void saveSettings()}>
            Save settings
          </button>
          {error && (
            <p className="notice error" role="alert">
              {error}
            </p>
          )}
        </div>
      </section>
      <Toast message={toast} onDismiss={() => setToast("")} />
    </Shell>
  );
}

export function App() {
  if (window.location.pathname === "/admin/settings") return <AdminSettings />;
  if (window.location.pathname === "/admin/login") return <AdminLogin />;
  if (window.location.pathname === "/admin/users") return <AdminUsers />;
  return <RegisterPage />;
}
