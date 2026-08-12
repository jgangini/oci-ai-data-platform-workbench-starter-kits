export type RegistrationPhase =
  | "identity"
  | "workspace"
  | "schemas"
  | "content"
  | "permissions";

export type RegistrationPhaseValue = RegistrationPhase | (string & {});

export const registrationPhases: readonly RegistrationPhase[] = [
  "identity",
  "workspace",
  "schemas",
  "content",
  "permissions",
];

export function registrationProgress(phase?: RegistrationPhaseValue) {
  const index = phase
    ? registrationPhases.indexOf(phase as RegistrationPhase)
    : -1;
  const step = index >= 0 ? index + 1 : 1;
  const total = registrationPhases.length;
  return {
    step,
    total,
    percent: Math.round(((step - 1) / total) * 100),
  };
}

export type RegistrationResponse = {
  status: "pending" | "active" | (string & {});
  phase?: RegistrationPhaseValue;
  message?: string;
  aidp_url?: string;
};

export type LabOperation = {
  labId: string;
  kind: "redeploy" | "remove";
  operationId: string;
};

type LabOperationStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
const labOperationId = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;

function labOperationStorageKey(userId: string, labId: string, kind: LabOperation["kind"]) {
  return `aidp-lab.operation.${kind}.${userId}.${labId}`;
}

export function loadLabOperation(
  storage: LabOperationStorage,
  userId: string,
  labId: string,
  kind: LabOperation["kind"],
): LabOperation | undefined {
  try {
    const value = JSON.parse(storage.getItem(labOperationStorageKey(userId, labId, kind)) || "null");
    return value &&
      value.labId === labId && value.kind === kind &&
      typeof value.operationId === "string" &&
      labOperationId.test(value.operationId)
      ? value
      : undefined;
  } catch {
    return undefined;
  }
}

export function persistLabOperation(
  storage: LabOperationStorage,
  userId: string,
  labId: string,
  kind: LabOperation["kind"],
  operation?: LabOperation,
) {
  if (operation)
    storage.setItem(labOperationStorageKey(userId, labId, kind), JSON.stringify(operation));
  else {
    try {
      storage.removeItem(labOperationStorageKey(userId, labId, kind));
    } catch {
      // ponytail: a stale UUID only replays an idempotent completed lab operation.
    }
  }
}

export function getOrCreateLabOperation(
  current: LabOperation | undefined,
  labId: string,
  kind: LabOperation["kind"],
  createId: () => string,
): LabOperation {
  if (!current) return { labId, kind, operationId: createId() };
  if (current.labId !== labId || current.kind !== kind)
    throw new Error("Finish the pending AIDP lab operation first.");
  return current;
}

export const registrationRetryDelaysMs = [
  2_000, 4_000, 8_000, 16_000, 30_000,
] as const;
export const registrationDeadlineMs = 10 * 60 * 1_000;

export class ApiRequestError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly retryAfterMs?: number,
  ) {
    super(message);
  }
}

export class RegistrationPollingTimeout extends Error {
  constructor() {
    super("OCI did not finish reconciling your access within 10 minutes. Please try again.");
    this.name = "RegistrationPollingTimeout";
  }
}

export function parseRetryAfter(value: string | null, now = Date.now()) {
  if (!value) return undefined;
  const seconds = Number(value);
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1_000;
  const date = Date.parse(value);
  return Number.isNaN(date) ? undefined : Math.max(0, date - now);
}

export function waitForRegistrationRetry(delayMs: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      globalThis.clearTimeout(timeout);
      reject(signal.reason);
    };
    const timeout = globalThis.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, delayMs);
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

type PollRegistrationOptions = {
  request: (signal: AbortSignal) => Promise<RegistrationResponse>;
  onPending?: (result: RegistrationResponse) => void;
  signal?: AbortSignal;
  deadlineMs?: number;
  delaysMs?: readonly number[];
  sleep?: (delayMs: number, signal: AbortSignal) => Promise<void>;
};

export async function pollRegistration({
  request,
  onPending,
  signal: parentSignal,
  deadlineMs = registrationDeadlineMs,
  delaysMs = registrationRetryDelaysMs,
  sleep = waitForRegistrationRetry,
}: PollRegistrationOptions): Promise<RegistrationResponse> {
  const controller = new AbortController();
  let deadlineReached = false;
  const abortFromParent = () => controller.abort(parentSignal?.reason);
  if (parentSignal?.aborted) abortFromParent();
  else parentSignal?.addEventListener("abort", abortFromParent, { once: true });
  const deadline = globalThis.setTimeout(() => {
    deadlineReached = true;
    controller.abort();
  }, deadlineMs);

  try {
    for (let attempt = 0; ; attempt += 1) {
      if (controller.signal.aborted) throw controller.signal.reason;
      try {
        const result = await request(controller.signal);
        if (result.status === "active") return result;
        if (result.status !== "pending")
          throw new Error(result.message || "Registration failed");
        onPending?.(result);
      } catch (error) {
        if (deadlineReached) throw new RegistrationPollingTimeout();
        if (controller.signal.aborted) throw error;
        if (!(error instanceof ApiRequestError) || error.status !== 429)
          throw error;
        const retryDelay =
          error.retryAfterMs ??
          delaysMs[Math.min(attempt, delaysMs.length - 1)];
        await sleep(retryDelay, controller.signal);
        continue;
      }
      await sleep(
        delaysMs[Math.min(attempt, delaysMs.length - 1)],
        controller.signal,
      );
    }
  } catch (error) {
    if (deadlineReached) throw new RegistrationPollingTimeout();
    throw error;
  } finally {
    globalThis.clearTimeout(deadline);
    parentSignal?.removeEventListener("abort", abortFromParent);
  }
}
