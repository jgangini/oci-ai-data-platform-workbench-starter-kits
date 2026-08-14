import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { labAssignmentChanges } from "../src/labAssignments.ts";

const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
const pollingSource = await readFile(new URL("../src/registrationPoll.ts", import.meta.url), "utf8");
const styles = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");
const viteConfig = await readFile(new URL("../vite.config.ts", import.meta.url), "utf8");
const labCatalog = JSON.parse(await readFile(new URL("../../backend/app/labs/catalog.json", import.meta.url), "utf8"));

test("browser storage is limited to the non-secret lab operation", () => {
  assert.doesNotMatch(source, /localStorage\.setItem|sessionStorage/);
  assert.match(pollingSource, /aidp-lab\.operation\.\$\{kind\}\.\$\{userId\}\.\$\{labId\}/);
  assert.match(pollingSource, /JSON\.stringify\(operation\)/);
  assert.doesNotMatch(pollingSource, /password|registrationCode|email/i);
});

test("registration has no password field while administrator login remains protected", () => {
  assert.match(source, /type="password"/);
  assert.match(source, /Registration code/);
  assert.match(source, /function AdminLogin/);
  assert.match(source, /Laboratories/);
  assert.match(source, /lab_id: "banking"/);
  assert.match(source, /Planned/);
  assert.doesNotMatch(source, /Generate password/);
});

test("registration code uses eight accessible segmented inputs", () => {
  assert.match(source, /className="code-slots"/);
  assert.match(source, /Registration code character \$\{index \+ 1\} of 8/);
  assert.match(source, /\^\[A-Z\]\{4\}-\[0-9\]\{4\}\$/);
});

test("development API proxy can target the deployed lab without committing a URL", () => {
  assert.match(viteConfig, /AIDP_API_PROXY_TARGET/);
  assert.match(viteConfig, /secure: false/);
  assert.match(viteConfig, /aidp_lab_admin_dev/);
});

test("administrator UI manages lab users through protected API routes", () => {
  assert.match(source, /\/api\/admin\/users/);
  assert.match(source, /Delete \$\{user\.email\}/);
  assert.match(source, /onSignOut=\{\(\) => setLogoutOpen\(true\)\}/);
  assert.match(source, /className="search-submit"/);
  assert.match(source, /<PlusIcon \/>/);
  assert.match(source, /<h1>Users<\/h1>/);
  assert.match(source, /<span>Users<\/span>/);
  assert.match(source, /data-tooltip="Logout"/);
  assert.match(source, /className="header-band"/);
  assert.match(source, /aria-label="Admin navigation"/);
  assert.match(source, /href="\/admin\/settings"/);
  assert.match(source, /currentPath === "\/admin\/users"/);
  assert.match(source, /title="Delete user\?"/);
  assert.match(source, /title="Log out\?"/);
  assert.match(source, /<th>Identity<\/th>/);
  assert.match(source, /user\.active \? "Active" : "Inactive"/);
  assert.match(source, /\/api\/admin\/settings/);
  assert.match(source, /\{tableError\} Refresh and try again\./);
  assert.match(source, /className="table-error"/);
  assert.match(source, /!tableError && !visible\.length/);
  assert.match(source, /user\.participant_code \?\? "--"/);
  assert.doesNotMatch(source, /String\(index \+ 1\)\.padStart/);
  assert.match(source, /Open AI Data Platform/);
  assert.match(source, /function Toast/);
  assert.match(source, /window\.setTimeout\(onDismiss, 4_000\)/);
  assert.match(source, /className="toast"/);
  assert.match(source, /className="toast-dismiss"/);
  assert.match(source, /aria-label="Dismiss notification"/);
  assert.match(source, /function CopyIcon/);
  assert.match(source, /navigator\.clipboard\.writeText\(aidpUrl\)/);
  assert.match(source, /className="settings-url-control"/);
  assert.match(source, /aria-label="Copy AI Data Platform URL"/);
  assert.match(source, /className="confirm-error"/);
});

test("administrator adds a user from an accessible catalog-driven lab table", () => {
  assert.match(source, /function CreateUserModal/);
  assert.match(source, /<span>Users<\/span>/);
  assert.match(source, /aria-haspopup="dialog"/);
  assert.match(source, /Select initial laboratories/);
  assert.match(source, /<th scope="col">Description<\/th>/);
  assert.match(source, /labDescription\(lab\)/);
  assert.match(source, /aria-label={`Select \$\{lab\.display_name\} laboratory`}/);
  assert.doesNotMatch(source, /className="admin-create"/);
  assert.match(styles, /\.create-user-modal/);
  assert.match(styles, /\.create-user-fields/);
  assert.match(source, /setCreateOpen\(false\);[\s\S]*setCreateProgress/);
  assert.match(source, /\{creating && \([\s\S]*<ProvisioningOverlay/);
  assert.match(source, /setCreateOpen\(true\);[\s\S]*Unable to create user/);
  assert.deepEqual(Object.keys(labCatalog.descriptions), labCatalog.labs);
  assert.ok(Object.values(labCatalog.descriptions).every((description) => description.length >= 40));
});

test("settings can rotate the registration code without exposing or persisting it", () => {
  assert.match(source, /Lab registration code/);
  assert.match(source, /registration_code_configured/);
  assert.match(source, /registration_code: registrationCode/);
  assert.match(source, /Configured — enter a new code to replace it/);
  assert.match(source, /aria-label="Lab registration code"/);
  assert.doesNotMatch(source, /(?:localStorage|sessionStorage).*registrationCode/);
});

test("administrator mutates one participant laboratory at a time", () => {
  assert.match(source, /<th>Laboratories<\/th>/);
  assert.match(source, /user\.labs\.map/);
  assert.match(source, /\/labs\/\$\{encodeURIComponent\(action\.lab\.lab_id\)\}/);
  assert.match(source, /getOrCreateLabOperation/);
  assert.match(source, /persistLabOperation/);
  assert.match(source, /operation_id: operation\.operationId/);
  assert.match(source, /Other laboratories and Identity access are preserved/);
  assert.match(source, /open=\{Boolean\(pendingLabAction\) && !operating\}/);
  assert.match(source, /aria-busy=\{operating \|\| creating\} inert=\{operating \|\| creating\}/);
  assert.match(source, /operationAbortRef\.current\?\.abort\(\)/);
  assert.match(source, /A participant must keep at least one laboratory/);
  assert.match(source, /disabled=\{!hasChanges \|\| !selectedLabIds\.length\}/);
  assert.match(source, /select:not\(\[disabled\]\)/);
  assert.match(source, /function ProvisioningOverlay/);
  assert.match(styles, /\.table-action \{[^}]*width: 44px;[^}]*min-height: 44px;/);
});

test("laboratory manager derives only the requested assignment changes", () => {
  assert.deepEqual(
    labAssignmentChanges(
      ["banking", "retail"],
      ["banking", "telecommunications", "healthcare"],
    ),
    {
      add: ["telecommunications", "healthcare"],
      remove: ["retail"],
    },
  );
  assert.match(source, /function LabManagerModal/);
  assert.match(source, /function EditIcon/);
  assert.match(source, /aria-label={`Manage laboratories for \$\{user\.email\}`}/);
  assert.doesNotMatch(source, /className="manage-labs-button"/);
  assert.match(source, /Participant laboratories/);
  assert.match(source, /confirmingRemoval \? "Confirm changes" : "Save"/);
  assert.match(source, /Confirm changes/);
  assert.match(source, /lab-assignment-check/);
  assert.match(source, /Redeploy \$\{lab\.display_name\} for \$\{user\.email\}/);
  assert.match(source, /labPhaseLabel\(installed\.phase\)/);
  assert.match(source, /lab\.available \? "Pending" : "Planned"/);
  assert.match(styles, /\.lab-manager-modal \{[^}]*1120px/);
});

test("registration retries OCI reconciliation with phases, backoff, and a real deadline", () => {
  assert.match(pollingSource, /"identity"[\s\S]*"workspace"[\s\S]*"schemas"[\s\S]*"content"[\s\S]*"permissions"/);
  assert.match(pollingSource, /2_000, 4_000, 8_000, 16_000, 30_000/);
  assert.match(pollingSource, /10 \* 60 \* 1_000/);
  assert.match(pollingSource, /\[408, 429, 502, 504\]\.includes\(error\.status\)/);
  assert.match(pollingSource, /error instanceof TypeError/);
  assert.match(source, /pollRegistration\(\{/);
  assert.match(source, /registrationAbortRef\.current\?\.abort\(\)/);
  assert.match(source, /phase: pending\.phase/);
  assert.match(source, /Loading\.\.\./);
  assert.doesNotMatch(source, /Preparing your lab/);
  assert.doesNotMatch(source, /Aligning governed schemas/);
  assert.match(source, /role="progressbar"/);
  assert.match(source, /aria-valuetext=/);
  assert.match(source, /Step \{progress\.step\} of \{progress\.total\}/);
  assert.match(source, /registration-progress-detail/);
  assert.match(source, /Open AI Data Platform/);
  assert.match(source, /function AccessReadyIcon/);
  assert.match(source, /aria-labelledby="registration-ready-title"/);
  assert.match(source, /useDialogFocus\(/);
  assert.match(source, /ref=\{readyCloseRef\}/);
  assert.match(source, /className="registration-result registration-result-ready"/);
  assert.match(source, /error instanceof Error[\s\S]*error\.message/);
});
