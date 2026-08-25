import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm } from "node:fs/promises";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";
import test, { after } from "node:test";

const frontendRoot = dirname(fileURLToPath(new URL("../package.json", import.meta.url)));
const output = await mkdtemp(join(tmpdir(), "aidp-registration-poll-"));
await promisify(execFile)(
  process.execPath,
  [
    join(frontendRoot, "node_modules", "typescript", "bin", "tsc"),
    join(frontendRoot, "src", "registrationPoll.ts"),
    "--target",
    "ES2022",
    "--module",
    "CommonJS",
    "--lib",
    "ES2022,DOM",
    "--strict",
    "--skipLibCheck",
    "--outDir",
    output,
  ],
  { cwd: frontendRoot },
);
const require = createRequire(import.meta.url);
const {
  ApiRequestError,
  getOrCreateModuleOperation,
  getOrCreateLabOperation,
  loadModuleOperation,
  loadLabOperation,
  moduleOperationKind,
  persistModuleOperation,
  persistLabOperation,
  RegistrationPollingTimeout,
  parseRetryAfter,
  pollRegistration,
  registrationProgress,
} = require(join(output, "registrationPoll.js"));

after(() => rm(output, { recursive: true, force: true }));

test("polling reconciles pending responses until active", async () => {
  const responses = [
    { status: "pending", phase: "schemas", message: "creating schemas" },
    { status: "active", aidp_url: "https://example.invalid/aidp" },
  ];
  const phases = [];
  const delays = [];
  const result = await pollRegistration({
    request: async () => responses.shift(),
    onPending: ({ phase }) => phases.push(phase),
    sleep: async (delay) => delays.push(delay),
    deadlineMs: 1_000,
  });
  assert.equal(result.status, "active");
  assert.deepEqual(phases, ["schemas"]);
  assert.deepEqual(delays, [2_000]);
});

test("registration progress counts completed provisioning phases", () => {
  assert.deepEqual(registrationProgress("identity"), {
    step: 1,
    total: 6,
    percent: 0,
  });
  assert.deepEqual(registrationProgress("cleanup"), {
    step: 1,
    total: 6,
    percent: 0,
  });
  assert.deepEqual(registrationProgress("schemas"), {
    step: 4,
    total: 6,
    percent: 50,
  });
  assert.deepEqual(registrationProgress("permissions"), {
    step: 6,
    total: 6,
    percent: 83,
  });
  assert.deepEqual(registrationProgress("future-phase"), {
    step: 1,
    total: 6,
    percent: 0,
  });
});

test("polling retries rate limits and gateway timeouts", async () => {
  let attempts = 0;
  const delays = [];
  const result = await pollRegistration({
    request: async () => {
      attempts += 1;
      if (attempts === 1) throw new ApiRequestError("limited", 429, 7_000);
      if (attempts === 2) throw new ApiRequestError("gateway timeout", 504);
      return { status: "active" };
    },
    sleep: async (delay) => delays.push(delay),
    deadlineMs: 1_000,
  });
  assert.equal(result.status, "active");
  assert.equal(attempts, 3);
  assert.deepEqual(delays, [7_000, 4_000]);
  assert.equal(parseRetryAfter("7", 0), 7_000);
});

test("polling retries a transient network failure", async () => {
  let attempts = 0;
  const result = await pollRegistration({
    request: async () => {
      attempts += 1;
      if (attempts === 1) throw new TypeError("Failed to fetch");
      return { status: "active" };
    },
    sleep: async () => undefined,
    deadlineMs: 1_000,
  });
  assert.equal(result.status, "active");
  assert.equal(attempts, 2);
});

test("polling preserves application service errors", async () => {
  const unavailable = new ApiRequestError("AIDP rejected the operation", 503);
  await assert.rejects(
    pollRegistration({
      request: async () => {
        throw unavailable;
      },
      deadlineMs: 1_000,
    }),
    (error) => error === unavailable,
  );
});

test("polling aborts an in-flight request at the deadline", async () => {
  const request = (signal) =>
    new Promise((_, reject) =>
      signal.addEventListener("abort", () => reject(signal.reason), { once: true }),
    );
  await assert.rejects(
    pollRegistration({ request, deadlineMs: 10 }),
    RegistrationPollingTimeout,
  );
});

test("polling propagates caller aborts without reporting a timeout", async () => {
  const controller = new AbortController();
  const request = (signal) =>
    new Promise((_, reject) =>
      signal.addEventListener("abort", () => reject(signal.reason), { once: true }),
    );
  const pending = pollRegistration({
    request,
    signal: controller.signal,
    deadlineMs: 1_000,
  });
  controller.abort(new DOMException("closed", "AbortError"));
  await assert.rejects(pending, (error) => {
    assert.equal(error.name, "AbortError");
    assert.notEqual(error.name, "RegistrationPollingTimeout");
    return true;
  });
});

test("polling preserves an incompatible-operation 409", async () => {
  const conflict = new ApiRequestError("another lab operation is active", 409);
  await assert.rejects(
    pollRegistration({
      request: async () => {
        throw conflict;
      },
      deadlineMs: 1_000,
    }),
    (error) => error === conflict && error.message === "another lab operation is active",
  );
});

test("lab operations reuse one UUID until completion", () => {
  let created = 0;
  const createId = () => `operation-${++created}`;
  const first = getOrCreateLabOperation(undefined, "banking", "redeploy", createId);
  const retry = getOrCreateLabOperation(first, "banking", "redeploy", createId);

  assert.deepEqual(first, { labId: "banking", kind: "redeploy", operationId: "operation-1" });
  assert.equal(retry, first);
  assert.equal(created, 1);
  assert.throws(
    () => getOrCreateLabOperation(first, "retail", "redeploy", createId),
    /Finish the pending AIDP lab operation/,
  );
  assert.equal(created, 1);
  assert.deepEqual(getOrCreateLabOperation(undefined, "retail", "remove", createId), {
    labId: "retail",
    kind: "remove",
    operationId: "operation-2",
  });
});

test("lab operations survive a page reload until completion", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const operation = {
    labId: "healthcare",
    kind: "redeploy",
    operationId: "4ab88c5e-c9e3-47bf-8dca-97f7eb7d0d43",
  };

  persistLabOperation(storage, "user-1", "healthcare", "redeploy", operation);
  assert.deepEqual(loadLabOperation(storage, "user-1", "healthcare", "redeploy"), operation);
  persistLabOperation(storage, "user-1", "healthcare", "redeploy");
  assert.equal(loadLabOperation(storage, "user-1", "healthcare", "redeploy"), undefined);
  assert.doesNotThrow(() =>
    persistLabOperation({ ...storage, removeItem: () => { throw new Error("blocked"); } }, "user-1", "healthcare", "redeploy"),
  );
  storage.setItem(
    "aidp-lab.operation.redeploy.user-1.healthcare",
    JSON.stringify({ labId: "unknown", kind: "redeploy", operationId: operation.operationId }),
  );
  assert.equal(loadLabOperation(storage, "user-1", "healthcare", "redeploy"), undefined);
});

test("global module operations reuse the server UUID and survive reloads", () => {
  const values = new Map();
  const storage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  const moduleId = "ai_data_governance_vsc_extension";
  const serverOperationId = "b0dc0b1d-351b-4416-acff-cbeb9e2d82c9";
  const operation = getOrCreateModuleOperation(
    undefined,
    moduleId,
    "install",
    () => "eea2956a-fdff-454b-9e33-76d72af966b4",
    serverOperationId,
  );

  assert.equal(operation.operationId, serverOperationId);
  persistModuleOperation(storage, moduleId, "install", operation);
  assert.deepEqual(loadModuleOperation(storage, moduleId, "install"), operation);
  assert.equal(
    getOrCreateModuleOperation(operation, moduleId, "install", () => crypto.randomUUID()),
    operation,
  );
  assert.equal(loadModuleOperation(storage, moduleId, "redeploy"), undefined);
  persistModuleOperation(storage, moduleId, "install");
  assert.equal(loadModuleOperation(storage, moduleId, "install"), undefined);
});

test("global module error states retain the exact recoverable operation kind", () => {
  assert.equal(moduleOperationKind("installing"), "install");
  assert.equal(moduleOperationKind("redeploying"), "redeploy");
  assert.equal(moduleOperationKind("deleting"), "delete");
  assert.equal(moduleOperationKind("error", "install"), "install");
  assert.equal(moduleOperationKind("error", "redeploy"), "redeploy");
  assert.equal(moduleOperationKind("error", "delete"), "delete");
  assert.equal(moduleOperationKind("error", "unknown"), undefined);
  assert.equal(moduleOperationKind("active", "redeploy"), undefined);
});

test("blocked browser storage never prevents an in-memory module operation", () => {
  const blockedStorage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
    removeItem: () => { throw new Error("blocked"); },
  };
  const moduleId = "ai_data_governance_vsc_extension";
  const operationId = "d3f4d6bc-1e10-4bce-a982-26dd90ad8d80";

  assert.equal(loadModuleOperation(blockedStorage, moduleId, "delete"), undefined);
  const operation = getOrCreateModuleOperation(
    undefined,
    moduleId,
    "delete",
    () => operationId,
  );
  assert.doesNotThrow(() =>
    persistModuleOperation(blockedStorage, moduleId, "delete", operation),
  );
  assert.equal(operation.operationId, operationId);
});
