# Terraform function and resource audit

This ledger maps infrastructure and lifecycle ownership to executable checks. It describes source contracts, not a live deployment certificate. Keep per-environment plans, logs, timings and resource identifiers in protected operational records. The [implementation limits](../docs/getting-started.md#current-implementation-limits), including participant notebook/provisioner alignment, remain acceptance requirements.

## Module resource ownership

New Deploy Studio bases prepare the HTTPS portal and module network without creating the optional viewer VM. The form has no per-VM/TLS switches. A confirmed **Settings → Application → Install module** updates `enabled_vm_modules` in the original Resource Manager stack; it does not call Compute to create an unmanaged instance. Only `gods_eye_view` is supported. Additional modules need explicit source, package and resource-allowlist support, not arbitrary VM uploads.

| Boundary | Contract | Reproducible check |
| --- | --- | --- |
| Base network | Reserve portal/viewer endpoints, subnets, NAT and security rules before module installation; keep portal metadata stable. | [Mock transition](tests/portal_modules.tftest.hcl) |
| Module plan | Permit only viewer VM, dynamic group and two IAM policy creates or managed no-ops; reject drift and unrelated writes. | [Exact delta check](tests/check_module_plan.py), [installer tests](../apps/backend/tests/test_gods_eye_view_installation.py) |
| Source and ownership | Verify stack identity, source commit/archive, variables and checked plan before apply. | [Installer](../apps/backend/app/gods_eye_view/installation.py) |
| Durable recovery | Store base receipt and operation/job IDs in the selected artifacts bucket, separately from Gold controls. Closing observes no cancellation; stopped workers need confirmed Resume. | [Post-apply tests](tests/test_post_apply.py), [dialog tests](../apps/frontend/tests/gods-eye-view-module-manager.test.mjs) |
| Native resources | Bootstrap managed AIDP workflows, compute and agent, then verify tasks, publication and private viewer; conversation acceptance is separate. | [Bootstrap tests](tests/test_gods_eye_view_bootstrap.py), [module tests](../apps/backend/tests/test_gods_eye_view_module.py) |
| Destroy | The original stack owns VM2 and its IAM resources. Verify the destroy plan/state and final inventory; the installer creates no separate VM to orphan. | Terraform resource addresses in [h_gods_eye_view.tf](h_gods_eye_view.tf) |

The installation path adds no general AIDP teardown hook. Existing participant/Governance cleanup and bucket-retention boundaries are unchanged; infrastructure destruction alone does not prove all API-created content or retained objects were cleaned. Existing unmanaged bases require their compatibility settings and do not acquire the new receipt/network contract through an application-image update.

## End-to-end chains

| Step | Inputs | Call chain | Automated evidence | Live evidence | Decision and reason |
|---|---|---|---|---|---|
| Release | `deploy-studio.json`, source context, plan JSON | `release_gate.main → validate_context → validate_source → validate_plan` | `tests/test_release_gate.py`, `tests/test_manifest.py` | Pending baseline/candidate artifacts | Keep: schema-v1/fresh-only trust boundary. |
| Preflight | OCI config/key paths and Deploy Studio context | `m_preflight.main → _load_sdk_config → select_inputs → compartment/capacity/key validators` | `tests/test_preflight.py` | Pending preflight events | Keep: validates compartment, home region, capacity and unencrypted key before apply. |
| Terraform | runtime inputs | naming → network → bucket → VM/bootstrap → Identity/IAM → AIDP → outputs | Terraform validate/test and the HCL tests listed below | Native acceptance required | Keep addresses unchanged; module installation preserves the base portal. |
| Regional discovery | uploaded OCI profile and effective region | `CloudTechNext.oci_inventory → list_region_subscriptions → regional probes → model filter → deployment revalidation` | CloudTechNext `test_oci_inventory.py`, `test_deployment_dependencies.py`, frontend build | v3 live evidence pending | Keep: config region is an initial choice; only compatible READY subscriptions and active Chat models are selectable. |
| Autonomous and AI | database mode/profile, ECPU count, model | `m_preflight._require_ready_region → _require_agent_model → _require_autonomous → Terraform → post_apply.ensure_ai_features` | `test_preflight.py`, `test_manifest.py`, `test_post_apply.py`, Terraform contract | v3 live evidence pending | Keep: one-region contract, 26ai DW validation and default 4 ECPU. |
| Bootstrap VM | commit-pinned source and Terraform outputs | `user_data.sh → release download → verified image → one-use credential → HTTPS health` | `tests/test_local_bootstrap.py`, `tests/test_identity_runtime.py`, `tests/test_manifest.py` | Native acceptance required | Keep: application and credential-consumption boundary. |
| Post-apply | Terraform outputs and operator credential files | `post_apply.main → reconcile → installation receipt → deliver_operator_credentials → health → build_success_result` | `tests/test_post_apply.py` | Native acceptance required | Keep: idempotent data-plane reconciliation and protected stack receipt. |
| VM module installation | confirmed administrator request and original stack receipt | `GodsEyeViewModule → ModuleInstallation → checked plan/apply → AIDP bootstrap → native verification` | `apps/backend/tests/test_gods_eye_view_installation.py`, `tests/check_module_plan.py` | Native acceptance required | Keep: one tracked module in the original stack; no portal replacement. |
| Registration | `lab_ids[]`, canonical packs | `main.provision_user → Identity pending → AidpClient.provision_user → _provision_lab(each) → permissions → activation` | `apps/backend/tests/test_api.py`, `test_aidp.py`, `test_lab_packs.py` | Participant layout acceptance required | Keep: multi-lab assignment and partial-failure recovery. |
| Lab administration | user, lab, operation UUID | `add_lab/redeploy_lab/delete_lab → per-lab journal → _provision_lab/_cleanup_lab` | `apps/backend/tests/test_api.py`, `test_aidp.py` | Native acceptance required | Keep: preserve the lab container during redeploy; exact-root deletion on removal. |
| Global governance module | selected `AI_DATA_PLATFORM_ADMIN`, production mode and selected model | `admin module API → AidpClient.install/redeploy/delete_governance_module → control tables/workflow/dedicated AI compute/Agent → exact RBAC` | `apps/backend/tests/test_governance.py`, `test_aidp.py`, `test_api.py` | v3 live evidence pending | Keep candidate: one singleton Agent exposes only catalog inventory and lineage; `AIDP_DEVELOPER` receives `USE`, `AI_DATA_PLATFORM_ADMIN` receives `ADMIN`, and participants receive neither source editing nor direct dedicated-compute access. |
| Participant deletion | exact Identity user and layout-v4 manifest | `main.admin_delete_user → AidpClient.cleanup_user → _cleanup_lab → catalog/data resources → IdentityClient.delete_lab_user` | `apps/backend/tests/test_api.py`, `test_aidp.py`, `test_autonomous.py`, `test_governance.py` | v3 live evidence pending | Keep: Identity deletion occurs only after exact participant-lab cleanup, and pending cleanup is retryable; the global governance singleton is independent of participant deletion. |
| Notebook parameters | task parameters in each canonical notebook | AIDP-injected `oidlUtils → parameters.getParameter → required_parameter` | `apps/backend/tests/test_lab_packs.py` | Participant layout acceptance required | Keep native parameterization and package hashes aligned with the job contract. |

## Terraform resources and data sources

| Address | Caller / consumer | Automated evidence | Live evidence | Decision and reason |
|---|---|---|---|---|
| `random_string.suffix` | All deterministic names | Terraform validate/test | Pending output/name inventory | Keep: collision-safe shared naming. |
| `oci_core_vcn.lab` | subnet, gateway, routes | `tests/test_manifest.py` | Pending VCN OCID | Keep: VM network. |
| `oci_core_subnet.public` | VM VNIC and route table | `tests/test_manifest.py` | Pending subnet OCID | Keep: public HTTPS endpoint. |
| `oci_core_security_list.web` | public subnet | `tests/test_manifest.py` | Pending ingress inventory | Keep: explicit HTTPS/egress boundary. |
| `oci_core_internet_gateway.lab` | public route | Terraform validate | Pending gateway OCID | Keep: VM/package reachability. |
| `oci_core_route_table.public` | public subnet | Terraform validate | Pending route inventory | Keep: internet route. |
| `oci_objectstorage_bucket.data` | post-apply and participant medallion paths | `tests/test_release_gate.py`, `tests/test_manifest.py` | Pending bucket OCID/prefixes | Keep: private Oracle-managed-key medallion bucket. |
| `oci_objectstorage_bucket.artifacts` | selected bucket for governance Delta tables and runtime artifacts | `tests/test_manifest.py`, `contract.tftest.hcl` | Live AIDP external-table acceptance pending | Keep: use a dedicated bucket per environment, defaulting to `oci_artifacts`; the governance module owns its exact `oci_artifacts/data_governance_*` table prefixes within that bucket. |
| `data.oci_identity_availability_domains.lab` | VM placement | `tests/test_preflight.py` | Pending selected AD | Keep: capacity-aware placement. |
| `terraform_data.vm_release` | instance replacement trigger | Terraform validate | Pending release SHA | Keep: pins bootstrap to immutable commit. |
| `data.oci_core_images.oracle_linux` | VM source image | Terraform validate | Pending image OCID | Keep: supported VM image lookup. |
| `oci_identity_tag_namespace.vm_bootstrap` | dynamic-group match | `tests/test_identity_runtime.py` | Pending tag namespace OCID | Keep: exact bootstrap identity scope. |
| `oci_identity_tag.vm_bootstrap` | VM tag and dynamic group | `tests/test_identity_runtime.py` | Pending tag OCID | Keep: exact bootstrap identity scope. |
| `oci_identity_dynamic_group.vm` | bootstrap/run-command policies | `tests/test_identity_runtime.py` | Pending dynamic-group OCID | Keep: no technical user or embedded key. |
| `oci_identity_policy.vm_bootstrap` | one-use credential object | `tests/test_identity_runtime.py`, `tests/test_post_apply.py` | Pending policy statements | Keep: least-privilege credential delivery. |
| `oci_core_instance.lab` | application/bootstrap host | `tests/test_identity_runtime.py`, `tests/test_manifest.py` | Pending instance OCID and health | Keep: registration/admin endpoint. |
| `oci_core_instance.gods_eye_view[0]`, `oci_identity_dynamic_group.gods_eye_view[0]`, `oci_identity_policy.gods_eye_view[0]`, `oci_identity_policy.gods_eye_view_run_command[0]` | portal-managed module installation | `portal_modules.tftest.hcl`, `tests/check_module_plan.py` | Native acceptance required | Keep: exact four-resource module allowlist in the original stack. |
| `oci_identity_policy.vm_run_command` | post-apply credential delivery | `tests/test_post_apply.py` | Pending run-command logs | Keep: encrypted one-use bootstrap channel. |
| `data.oci_core_vnic_attachments.lab` | public-IP output | Terraform validate | Pending VNIC attachment | Keep: resolves endpoint. |
| `data.oci_core_vnic.lab` | public-IP output | Terraform validate | Pending public IP | Keep: resolves endpoint. |
| `data.oci_identity_domains.default` | groups/domain URL | `tests/test_identity_runtime.py` | Pending domain OCID | Keep: reuses the tenancy domain. |
| `oci_identity_domains_group.developers` | active participant membership/RBAC | `tests/test_identity_runtime.py`, `tests/test_post_apply.py` | Pending exact members/roles | Keep: participant access boundary. |
| `oci_identity_domains_group.pending` | registration transaction | `tests/test_identity_runtime.py`, `apps/backend/tests/test_api.py` | Pending transition events | Keep: prevents partial activation. |
| `oci_identity_policy.developer_console` | participant console access | `tests/test_identity_runtime.py` | Pending policy statements | Keep: required console entry. |
| `oci_identity_policy.aidp_service` | AIDP control/data plane | `tests/test_identity_runtime.py` | Pending policy statements | Keep: required service permissions, optional policies rejected. |
| `oci_ai_data_platform_ai_data_platform.lab` | post-apply workspace/catalog/compute | `tests/test_post_apply.py` | Pending platform OCID/state | Keep: shared stage-1 platform. |
| `oci_database_autonomous_database.agent` | AI enablement and global Agent checkpointer | `tests/test_manifest.py`, `tests/test_preflight.py`, `contract.tftest.hcl` | v3 live evidence pending | Keep candidate: 26ai DW, ECPU, default 4, no autoscaling. |
| `data.oci_database_autonomous_database.existing_agent` | existing-database validation | `tests/test_manifest.py`, `tests/test_preflight.py`, Terraform validate | v3 live evidence pending | Keep candidate: reads only the explicitly selected OCID. |
| `terraform_data.validate_autonomous_inputs` | new/existing database input contract | Terraform 1.5.7/1.15.7 validate and `contract.tftest.hcl` | v3 live evidence pending | Keep candidate: cross-variable preconditions remain compatible with Terraform 1.5.7. |
| `terraform_data.validate_existing_autonomous_database` | VM precondition | `tests/test_identity_runtime.py`, Terraform validate | v3 live evidence pending | Keep: rejects a non-26ai or non-DW existing database before VM bootstrap. |
| `data.oci_identity_tenancy.current` | tenancy output/artifact | Terraform validate | Pending tenancy name | Keep: auditable result metadata. |

## Outputs

All outputs remain because `deploy-studio.json`, `post_apply.py`, cloud-init or the final
artifact consumes them. Live values must be stored only in the sanitized deployment artifact.

| Output | Consumer | Automated evidence | Live evidence | Decision |
|---|---|---|---|---|
| `application_url` | Deploy Studio/app health | `tests/test_manifest.py`, `tests/test_post_apply.py` | Pending | Keep. |
| `portal_managed_modules`, `enabled_vm_modules`, `source_commit_sha`, `deployment_suffix` | original-stack installation receipt and module lifecycle | `tests/test_post_apply.py`, `portal_modules.tftest.hcl` | Native acceptance required | Keep: capability, immutable source and deterministic ownership. |
| `admin_url` | access email/artifact | `tests/test_manifest.py` | Pending | Keep. |
| `aidp_workbench_url` | UI settings/artifact | `tests/test_post_apply.py` | Pending | Keep. |
| `aidp_web_socket_endpoint` | Workbench URL resolution | `tests/test_post_apply.py` | Pending | Keep. |
| `aidp_alias_key` | AIDP alias endpoint | `tests/test_post_apply.py` | Pending | Keep. |
| `tenancy_name` | artifact | `tests/test_manifest.py` | Pending | Keep. |
| `identity_domain_name` | artifact | `tests/test_manifest.py` | Pending | Keep. |
| `compartment_ocid` | post-apply/inventory | `tests/test_manifest.py` | Pending | Keep. |
| `bucket_name` | app/post-apply/notebook job parameters | `tests/test_manifest.py`, `apps/backend/tests/test_lab_packs.py` | Pending | Keep. |
| `objectstorage_namespace` | app/notebook job parameters | `tests/test_manifest.py`, `apps/backend/tests/test_lab_packs.py` | Pending | Keep. |
| `medallion_prefixes` | artifact/validation | `tests/test_manifest.py` | Pending | Keep. |
| `ai_data_platform_id` | app/post-apply | `tests/test_post_apply.py` | Pending | Keep. |
| `autonomous_database_id`, `autonomous_database_mode`, `autonomous_database_version`, `autonomous_database_workload`, `autonomous_database_compute_count` | post-apply/app/audit | `tests/test_manifest.py`, Terraform contract | v3 live evidence pending | Keep candidate: exact database contract without secret values. |
| `agent_model_id` | production-only global governance Agent source | `tests/test_manifest.py`, `tests/test_preflight.py`, `apps/backend/tests/test_governance.py` | v3 live evidence pending | Keep candidate: selected active regional Chat model; only a platform administrator can install or redeploy the singleton. |
| `default_workspace_name` | app/post-apply | `tests/test_post_apply.py` | Pending | Keep. |
| `developer_group_ocid` | post-apply/app Identity | `tests/test_post_apply.py` | Pending | Keep. |
| `pending_group_ocid` | app Identity transaction | `tests/test_identity_runtime.py` | Pending | Keep. |
| `operator_user_ocid` | post-apply RBAC | `tests/test_post_apply.py` | Pending | Keep. |
| `home_region` | post-apply/Identity Domains | `tests/test_preflight.py` | Pending | Keep. |
| `aidp_catalog_name` | app/post-apply contract | `tests/test_post_apply.py` | Pending | Keep. |
| `aidp_shared_compute_name` | app/post-apply contract | `tests/test_post_apply.py` | Pending | Keep. |
| `aidp_external_volume_count` | fresh-only safety assertion | `tests/test_manifest.py`, `tests/test_post_apply.py` | Pending | Keep: must remain zero. |
| `identity_domain_url` | app | `tests/test_manifest.py` | Pending | Keep. |
| `instance_id` | run command/artifact | `tests/test_post_apply.py` | Pending | Keep. |
| `public_ip` | application URL/health | `tests/test_post_apply.py` | Pending | Keep. |
| `vm_shape` | artifact/capacity evidence | `tests/test_preflight.py` | Pending | Keep. |

## Python and shell functions

The following rows identify the principal callables in `terraform/*.py`,
`terraform/hooks/*.py` and `templatefile/user_data.sh`.
Methods on the internal `AidpApi` transport are exercised through the listed post-apply tests
and are not independent deployment entrypoints.

| Function | Caller | Automated evidence | Live evidence | Decision and reason |
|---|---|---|---|---|
| `release_gate._compartment_name` | `compartment_target` | `test_release_gate.py` | Pending | Keep: trust-boundary validation. |
| `release_gate._compartment_mode` | `compartment_target` | `test_release_gate.py` | Pending | Keep: explicit new/existing contract. |
| `release_gate.compartment_target` | preflight/context validation | `test_release_gate.py` | Pending | Keep. |
| `release_gate.validate_context` | preflight and CLI | `test_release_gate.py` | Pending | Keep: repository/ref/SHA/region gate. |
| `release_gate._deployment_files` | `validate_source` | `test_release_gate.py` | Pending | Keep. |
| `release_gate._technical_identity_finding` | source/plan validation | `test_release_gate.py` | Pending | Keep: forbids technical users. |
| `release_gate._forbidden_finding` | source/plan validation | `test_release_gate.py` | Pending | Keep. |
| `release_gate._control_name_finding` | Terraform/manifest/plan validation | `test_release_gate.py` | Pending | Keep: rejects retired gateway, OKE, Vault, KMS, API Gateway and JDBC declarations without scanning prose. |
| `release_gate._forbidden_control_resource` | Terraform declaration validation | `test_release_gate.py` | Pending | Keep: examines deployable HCL only. |
| `release_gate._manifest_control_finding` | `deploy-studio.json` field/output validation | `test_release_gate.py` | Pending | Keep: rejects retired positive controls while allowing explanatory text. |
| `release_gate.validate_source` | preflight and CLI | `test_release_gate.py` | Pending | Keep. |
| `release_gate._has_nonempty_key` | plan validation | `test_release_gate.py` | Pending | Keep: rejects customer-managed Object Storage keys. |
| `release_gate._planned_values` | plan validation | `test_release_gate.py` | Pending | Keep. |
| `release_gate._forbidden_plan_type` | plan validation | `test_release_gate.py` | Pending | Keep. |
| `release_gate.validate_plan` | CLI/Deploy Studio gate | `test_release_gate.py` | Pending | Keep: create-only candidate. |
| `release_gate.main` | CLI/Deploy Studio | `test_release_gate.py` | Pending | Keep: entrypoint. |
| `m_preflight._safe_error_message` | `main` | `test_preflight.py` | Pending | Keep: prevents secret leakage. |
| `m_preflight._home_region` | `select_inputs` | `test_preflight.py` | Pending | Keep. |
| `m_preflight._require_ready_region` | `select_inputs` | `test_preflight.py` | v3 live evidence pending | Keep: effective region must remain subscribed and READY. |
| `m_preflight._model_is_selectable` | `_require_agent_model` | `test_preflight.py` | v3 live evidence pending | Keep: rejects inactive, non-Chat, deprecated or retired models. |
| `m_preflight._require_agent_model` | `select_inputs` | `test_preflight.py` | v3 live evidence pending | Keep: revalidates the selected model in the effective region. |
| `m_preflight._require_autonomous` | `select_inputs` | `test_preflight.py` | v3 live evidence pending | Keep: validates 26ai DW availability or the selected existing database. |
| `m_preflight._candidate_shapes` | `select_inputs` | `test_preflight.py` | Pending | Keep: E5/E4/E3 fallback. |
| `m_preflight._list_all` | compartment/work-request discovery | `test_preflight.py` | Pending | Keep: pagination. |
| `m_preflight._has_active_aidp_work_request` | compartment validator | `test_preflight.py` | Pending | Keep: recovery/idempotence. |
| `m_preflight._require_compartment_target` | `select_inputs` | `test_preflight.py` | Pending | Keep. |
| `m_preflight.select_inputs` | `main` | `test_preflight.py` | Pending | Keep: main selection logic. |
| `m_preflight._read_json_env` | `main` | `test_preflight.py` | Pending | Keep: Deploy Studio input. |
| `m_preflight._write_result` | `main` | `test_preflight.py` | Pending | Keep: Deploy Studio output. |
| `m_preflight._require_unencrypted_private_key` | SDK config loader | `test_preflight.py` | Pending | Keep: fail closed. |
| `m_preflight._load_sdk_config` | `main` | `test_preflight.py` | Pending | Keep: credential boundary. |
| `m_preflight.main` | Deploy Studio | `test_preflight.py` | Pending | Keep: entrypoint. |
| `post_apply._sleep` | retry/wait helpers | `test_post_apply.py` | Pending | Keep: global deadline aware. |
| `post_apply.read_json_env` | `main` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.write_result` | `main` | `test_manifest.py` | Pending | Keep: artifact contract. |
| `post_apply.exact_one` | reconciliation helpers | `test_post_apply.py` | Pending | Keep: ambiguity guard. |
| `post_apply.assert_fields` | resource reconciliation | `test_post_apply.py` | Pending | Keep: drift guard. |
| `post_apply.is_active_or_raise` | resource waits | `test_post_apply.py` | Pending | Keep: terminal-state guard. |
| `post_apply.ensure_resource` | `reconcile` | `test_post_apply.py` | Pending | Keep: idempotence. |
| `post_apply.wait_for_existing_active` | `reconcile` | `test_post_apply.py` | Pending | Keep: async recovery. |
| `post_apply.role_has_member` | role checks | `test_post_apply.py` | Pending | Keep. |
| `post_apply.role_has_group` | role checks | `test_post_apply.py` | Pending | Keep. |
| `post_apply.assert_role_members_exact` | `reconcile` | `test_post_apply.py` | Pending | Keep: least privilege. |
| `post_apply.assert_operator_platform_admin` | `reconcile` | `test_post_apply.py` | Pending | Keep. |
| `post_apply._admin_permission_is_assigned` | permission verification | `test_post_apply.py` | Pending | Keep. |
| `post_apply.permission_is_assigned` | permission verification | `test_post_apply.py` | Pending | Keep: pagination/correlation. |
| `post_apply.assert_role_permissions_exact` | `reconcile` | `test_post_apply.py` | Pending | Keep: rejects broad grants. |
| `post_apply.ensure_action` | role/permission mutation | `test_post_apply.py` | Pending | Keep: observable idempotence. |
| `post_apply.load_oci_config` | `main` | `test_post_apply.py` | Pending | Keep: credential boundary. |
| `post_apply.render_runtime_oci_config` | credential delivery | `test_post_apply.py` | Pending | Keep: sanitized runtime config. |
| `post_apply.build_signer` | `main` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.describe_object_prefixes` | `reconcile` | `test_post_apply.py` | Pending | Keep: virtual prefixes only. |
| `post_apply.workspace_object` | folder/permission helpers | `test_post_apply.py` | Pending | Keep. |
| `post_apply.workspace_object_key` | folder/permission helpers | `test_post_apply.py` | Pending | Keep: exact path correlation. |
| `post_apply.ensure_workspace_folder` | `reconcile` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.ensure_role` | `reconcile` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.ensure_role_permission` | `reconcile` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.assert_fresh_catalog` | `reconcile` | `test_post_apply.py` | Pending | Keep: blocks incompatible legacy resources. |
| `post_apply.parse_public_key_output` | credential delivery | `test_post_apply.py` | Pending | Keep. |
| `post_apply.fetch_bootstrap_public_key` | credential delivery | `test_post_apply.py` | Pending | Keep: retry/IAM propagation. |
| `post_apply.encrypt_bootstrap_credentials` | credential delivery | `test_post_apply.py` | Pending | Keep: RSA-OAEP/AES-GCM. |
| `post_apply.reconcile` | `main` | `test_post_apply.py` | Pending | Keep: stage-1 data-plane orchestrator. |
| `post_apply._module_installation_stack`, `_store_module_installation_context`, `write_module_installation_context` | `main` | `test_post_apply.py` | Native acceptance required | Keep: verify exact original stack/archive and persist create-only deployment receipt without credentials. |
| `post_apply.workbench_url` | URL resolver | `test_post_apply.py` | Pending | Keep. |
| `post_apply.aidp_alias_endpoint` | URL resolver | `test_post_apply.py` | Pending | Keep. |
| `post_apply.wait_for_application` | `main` | `test_post_apply.py` | Pending | Keep: HTTPS readiness. |
| `post_apply.resolve_workbench_url` | `main` | `test_post_apply.py` | Pending | Keep. |
| `post_apply.deliver_operator_credentials` | `main` | `test_post_apply.py` | Pending | Keep: one-use encrypted delivery. |
| `post_apply.delete_bootstrap_object` | bootstrap completion | `test_post_apply.py` | Pending | Keep: secret cleanup. |
| `post_apply.wait_for_bootstrap_consumed` | `main` | `test_post_apply.py` | Pending | Keep: consumption proof. |
| `post_apply.build_success_result` | `main` | `test_manifest.py` | Pending | Keep: sanitized artifact. |
| `post_apply._wait_for_autonomous_available` | `main` | `test_post_apply.py` | v3 live evidence pending | Keep: prevents wallet and AI operations before AVAILABLE. |
| `post_apply._response_bytes` | wallet preparation | `test_post_apply.py` | v3 live evidence pending | Keep: normalizes SDK wallet responses. |
| `post_apply._validate_wallet` | wallet preparation | `test_post_apply.py` | v3 live evidence pending | Keep: ZIP path and size boundary. |
| `post_apply.prepare_autonomous_wallet` | `main` | `test_post_apply.py` | v3 live evidence pending | Keep: generated wallet for new DB or exact uploaded wallet for existing DB. |
| `post_apply.ensure_ai_features` | `main` | `test_post_apply.py` | v3 live evidence pending | Keep: idempotent AI enablement and work-request wait; ADMIN remains post-apply-only. |
| `post_apply.bootstrap_autonomous_governance` | `deliver_operator_credentials` | `test_post_apply.py`, `apps/backend/tests/test_autonomous.py` | v3 live evidence pending | Keep: installs an ADMIN-owned allowlisted package and rotates an EXECUTE-only operator without persisting ADMIN for the global Agent checkpointer; metadata synchronization reads Master Catalog directly. |
| `autonomous.AutonomousGovernanceClient.ensure_participant/drop_participant` | no current module caller | `apps/backend/tests/test_autonomous.py`, `test_governance.py` | Not required by v2.2.0 | Retire candidate: v2.2.0 assumes no previous participant-Agent installations, and the global singleton lifecycle must not invoke participant mirror helpers. |
| `post_apply.main` | Deploy Studio | `test_post_apply.py` | Pending | Keep: entrypoint. |
| `user_data.retry` | all network/bootstrap commands | `test_local_bootstrap.py` | Pending | Keep: transient recovery. |
| `user_data.use_reachable_base_images` | Docker build bootstrap | `test_local_bootstrap.py` | Pending | Keep: registry fallback. |

## Deletion rule

A row may change to `remove` only when it has no caller/entrypoint role, protects none of
security, migration, idempotence, pagination or recovery, is outside the Deploy Studio
contract, has no automated or live evidence, and removal leaves Python/frontend/Terraform,
Graphify, Sentrux and candidate deployment gates green. Ambiguous evidence means `keep`.
