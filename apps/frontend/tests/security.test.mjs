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
  assert.match(source, /type=\{showPassword \? "text" : "password"\}/);
  assert.match(source, /Registration code/);
  assert.match(source, /function AdminLogin/);
  assert.match(source, /Starter kits/);
  assert.match(source, /lab_id: "banking"/);
  assert.match(source, /Planned/);
  assert.match(source, /role="combobox"/);
  assert.match(source, /aria-multiselectable="true"/);
  assert.match(source, /role="option"/);
  assert.match(source, /className="lab-combobox-description"/);
  assert.match(source, /\{labDescription\(lab\)\}/);
  assert.match(source, /event\.key !== "Escape"/);
  assert.match(source, /labPickerRef\.current\?\.contains/);
  assert.match(styles, /\.lab-combobox-menu \{[^}]*position: absolute/);
  assert.match(styles, /\.lab-combobox-menu \{[^}]*max-height: 320px/);
  assert.match(styles, /\.lab-combobox-menu \{[^}]*overflow-y: auto/);
  assert.doesNotMatch(source, /Generate password/);
});

test("deployment mode selects registration or inline administrator login", () => {
  assert.match(source, /function RegisterPage\(\{[\s\S]*initialAdminLogin = false/);
  assert.match(source, /const \[adminLoginVisible, setAdminLoginVisible\] = useState\(initialAdminLogin\)/);
  assert.match(source, /const production = publicConfig\?\.deployment_mode === "production"/);
  assert.match(source, /function registrationAccessView[\s\S]*showAdminLogin: !configLoaded \|\| production \|\| adminLoginVisible/);
  assert.match(source, /onAdminLogin=\{accessView\.showAdminLink \? showAdminLogin : undefined\}/);
  assert.match(source, /onHome=\{accessView\.showHomeLink \? showRegistration : undefined\}/);
  assert.match(source, /accessView\.showAdminLogin \? \([\s\S]*<AdminLoginCard \/>/);
  assert.match(source, /aria-label="Administrator login"/);
  assert.match(source, /aria-label="Return to starter kit registration"/);
  assert.doesNotMatch(source, /href="\/admin\/login"/);
  assert.match(source, /window\.history\.replaceState\(null, "", "\/"\)/);
  assert.match(source, /window\.location\.pathname === "\/admin\/login"\)[\s\S]*<RegisterPage initialAdminLogin \/>/);
  assert.match(source, /autoComplete="off" onSubmit=\{submit\}/);
  assert.match(source, /autoComplete="new-password"/);
  assert.match(source, /aria-pressed=\{showPassword\}/);
  assert.match(source, /title=\{showPassword \? "Hide password" : "Show password"\}/);
  assert.match(source, /<circle cx="12" cy="12" r="3" \/>/);
  assert.doesNotMatch(source, /\{showPassword \? "Hide" : "Show"\}/);
  assert.match(source, /htmlFor="aidp-admin-password"/);
  assert.match(source, /id="aidp-admin-password"/);
  assert.match(styles, /\.admin-link \{[^}]*background: transparent/);
  assert.match(styles, /\.login-password-control \{[^}]*46px/);
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
  assert.match(source, /Open AI Data Platform Workbench/);
  assert.match(source, /Oracle AI Data Platform Workbench/);
  assert.match(source, /Starter Kits/);
  assert.match(source, /function Toast/);
  assert.match(source, /window\.setTimeout\(onDismiss, 4_000\)/);
  assert.match(source, /className="toast"/);
  assert.match(source, /className="toast-dismiss"/);
  assert.match(source, /aria-label="Dismiss notification"/);
  assert.match(source, /function CopyIcon/);
  assert.match(source, /function copyAidpValue/);
  assert.match(source, /navigator\.clipboard\.writeText\(value\)/);
  assert.match(source, /className="settings-url-control"/);
  assert.match(source, /aria-label="Copy AI Data Platform Workbench URL"/);
  assert.match(source, /function OpenExternalIcon/);
  assert.match(source, /className="settings-url-control settings-url-control-actions"/);
  assert.match(source, /aria-label="Open AI Data Platform Workbench"/);
  assert.match(styles, /\.settings-url-control-actions/);
  assert.match(source, /AI Data Platform Workbench OCID/);
  assert.match(source, /aria-label="Copy AI Data Platform Workbench OCID"/);
  assert.match(source, /className="confirm-error"/);
});

test("administrator adds a user from an accessible catalog-driven lab table", () => {
  assert.match(source, /function CreateUserModal/);
  assert.match(source, /<span>Users<\/span>/);
  assert.match(source, /aria-haspopup="dialog"/);
  assert.match(source, /Select initial starter kits/);
  assert.match(source, /<th scope="col">Description<\/th>/);
  assert.match(source, /labDescription\(lab\)/);
  assert.match(source, /aria-label={`Select \$\{lab\.display_name\} starter kit`}/);
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
  assert.match(source, /function SettingsRegistrationCodeField/);
  assert.match(source, /Lab registration code/);
  assert.match(source, /registration_code_configured/);
  assert.match(source, /registration_code: registrationCode/);
  assert.match(source, /className="registration-code settings-registration-code"/);
  assert.match(source, /Lab registration code character \$\{index \+ 1\} of 8/);
  assert.match(source, /\^\[A-Z\]\{4\}-\[0-9\]\{4\}\$/);
  assert.doesNotMatch(source, /(?:localStorage|sessionStorage).*registrationCode/);
});

test("settings separate Workbench, application and governance details into accessible tabs", () => {
  assert.match(source, /className="settings-tabs" role="tablist"/);
  assert.match(source, /role="tab"[\s\S]*aria-controls="settings-panel-workbench"/);
  assert.match(source, /role="tab"[\s\S]*aria-controls="settings-panel-application"/);
  assert.match(source, /role="tab"[\s\S]*aria-controls="settings-panel-governance"/);
  assert.match(source, /role="tabpanel"[\s\S]*aria-labelledby="settings-tab-workbench"/);
  assert.match(source, /role="tabpanel"[\s\S]*aria-labelledby="settings-tab-application"/);
  assert.match(source, /role="tabpanel"[\s\S]*aria-labelledby="settings-tab-governance"/);
  assert.match(source, /ArrowLeft[\s\S]*ArrowRight[\s\S]*Home[\s\S]*End/);
  assert.match(source, /\["workbench", "application", "governance"\]/);

  const workbenchPanel = source.indexOf('id="settings-panel-workbench"');
  const applicationPanel = source.indexOf('id="settings-panel-application"');
  const governancePanel = source.indexOf('id="settings-panel-governance"');
  const panelsEnd = source.indexOf("{error && (", governancePanel);
  assert.ok(workbenchPanel > 0 && applicationPanel > workbenchPanel && governancePanel > applicationPanel && panelsEnd > governancePanel);

  const workbenchSource = source.slice(workbenchPanel, applicationPanel);
  const applicationSource = source.slice(applicationPanel, governancePanel);
  const governanceSource = source.slice(governancePanel, panelsEnd);
  const serviceEndpoint = workbenchSource.indexOf("AI Data Platform Workbench Service Endpoint");
  const workbenchUrl = workbenchSource.indexOf("AI Data Platform Workbench URL");
  const workbenchOcid = workbenchSource.indexOf("AI Data Platform Workbench OCID");
  assert.ok(serviceEndpoint < workbenchUrl && workbenchUrl < workbenchOcid);
  assert.doesNotMatch(workbenchSource, /Shared compute/);
  assert.match(applicationSource, /<ApplicationAccessSettings/);
  assert.match(source, /function ApplicationAccessSettings[\s\S]*<SettingsRegistrationCodeField/);
  assert.doesNotMatch(applicationSource, /AIDP JDBC driver/);
  assert.match(source, /function ApplicationAccessSettings[\s\S]*>\s*Save Settings\s*</);
  assert.match(governanceSource, /Governance artifact bucket/);
  assert.match(governanceSource, /Governance Delta schema/);
  assert.match(governanceSource, /AIDP JDBC driver/);
  assert.match(governanceSource, /Install JDBC Driver/);
  assert.match(source, /const governanceGatewayInstalled = jdbcDriverAvailable && Boolean\(governanceGatewayUrl\)/);
  assert.match(governanceSource, /governanceGatewayInstalled \? \(/);
  assert.match(governanceSource, /jdbcDriverAvailable \? \(/);
  assert.match(governanceSource, /Deployment pending/);
  assert.match(governanceSource, /connect-compute\.html#GUID-B7F594C8-724D-4733-B80D-7A4C9A0CB0A2/);
  assert.match(governanceSource, /jdbcDriverDownloadImage/);
  assert.match(source, /title="Install JDBC driver\?"/);
  assert.match(source, /label="Installing JDBC driver"/);
  assert.match(styles, /\.settings-tab\[aria-selected="true"\]/);
  assert.match(styles, /\.settings-panel\[hidden\] \{ display: none; \}/);
  assert.match(styles, /\.settings-tabs \{[^}]*flex-wrap: wrap;[^}]*overflow: visible;/);
  assert.doesNotMatch(styles, /\.settings-tabs \{[^}]*overflow-x: auto;/);
});

test("administrator mutates one participant laboratory at a time", () => {
  assert.match(source, /<th>Starter kits<\/th>/);
  assert.match(source, /user\.labs\.map/);
  assert.match(source, /\/labs\/\$\{encodeURIComponent\(action\.lab\.lab_id\)\}/);
  assert.match(source, /getOrCreateLabOperation/);
  assert.match(source, /persistLabOperation/);
  assert.match(source, /operation_id: operation\.operationId/);
  assert.match(source, /Other starter kits and Identity access are preserved/);
  assert.match(source, /open=\{Boolean\(pendingLabAction\) && !operating\}/);
  assert.match(source, /aria-busy=\{operating \|\| creating\} inert=\{operating \|\| creating\}/);
  assert.match(source, /operationAbortRef\.current\?\.abort\(\)/);
  assert.match(source, /A participant must keep at least one starter kit/);
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
  assert.match(source, /aria-label={`Manage starter kits for \$\{user\.email\}`}/);
  assert.doesNotMatch(source, /className="manage-labs-button"/);
  assert.match(source, /Participant starter kits/);
  assert.match(source, /confirmingRemoval \? "Confirm changes" : "Save"/);
  assert.match(source, /Confirm changes/);
  assert.match(source, /lab-assignment-check/);
  assert.match(source, /Redeploy \$\{lab\.display_name\} for \$\{user\.email\}/);
  assert.match(source, /pendingLabAction\.lab\.lab_id === "agent"/);
  assert.match(source, /replaces the participant's Agent customizations/);
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
