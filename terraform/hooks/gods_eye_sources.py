"""Render two readable, self-contained Spark programs from explicitly selected source."""
import ast
import io
import json
import pprint
import tokenize
import zipfile


SENSOR_EXPORTS = {
    "core": "utc_text",
    "sensors": "SENSOR_TYPES validate_record",
    "control_store": "DOCUMENT_NAME ControlConflict ObjectControlStore _valid_name read_document write_document mutate_document sensor_reset_version reset_version",
    "landing": "ensure_volumes stream_progress",
    "runtime_secrets": "SHARED_OCI_CREDENTIAL_NAME OCI_CREDENTIALS identity_hash values _oci_values _signer runtime_auth",
    "sensor_pipeline": "MAX_BATCH_RECORDS MAX_VISIBLE_SENSORS SENSOR_SCHEMA decode_batch SensorLake reset_barrier run",
}
SOCIAL_EXPORTS = {
    "media": "photos",
    "core": "PLATFORMS SYNTHETIC_MODES LOCALITIES CATEGORIES SEVERITIES CATEGORY_NAMES canonical_mode publication_revisions default_source aidp_credential_name utc_text simulation_state folded _event_location normalize_event image_hashes corroboration legacy_groups review_location incident_summary build_snapshot",
    "correlation": "timestamp identity nearby distance_km expanded_claims retained_memberships nearest_group group_events activity relations jurisdiction same_jurisdiction social_context sensor_context sensor_index add_context",
    "classification": "PROMPT_VERSION _NON_LITERAL_QUOTE _DUPLICATE_RISK _LABEL_PROPERTIES _CLASSIFICATION_SCHEMA _labels _claims classify",
    "control_store": "DOCUMENT_NAME ControlConflict ObjectControlStore _valid_name read_document write_document mutate_document publish reset_version sensor_reset_version publications validate_replacement replace_synthetic_publication replace_sensor_publication upsert_posts",
    "post_index": "POST_HEAD POST_RANK published_time _post_json _post_hash _post_integer _post_head _post_append _post_document upsert_posts append_purge",
    "landing": "ensure_volumes stream_progress page decode_record records write_objects",
    "runtime_secrets": "SHARED_OCI_CREDENTIAL_NAME OCI_CREDENTIALS identity_hash values signer _oci_values _signer runtime_auth",
    "capture": "schedule schedule_at query_lines",
    "x": "THIRD_PARTY XFailure retry_time fetch_page _post_event query_checkpoint poll_queries _poll_status",
    "sensors": "SENSOR_TYPES validate_record apply_locations",
    "sensor_capture": "_family_change",
    "sensor_pipeline": "MAX_BATCH_RECORDS MAX_VISIBLE_SENSORS SENSOR_SCHEMA decode_batch SensorLake",
    "synthetic_reset": "HISTORY_PREFIX VERSION HISTORY_BATCH_BYTES encoded _synthetic_ids prune_publication _versioned_replacement object_keys object_body delete_object clean_landing _clean_controls _save_clean_history _history_batches _delete_history_object _history_objects clean_history execute",
    "sensor_reset": "family _validate_rows _remaining_body _clear_controls clean_landing execute",
    "scheduling": "JOB_FIELDS RUN_SUCCESS RUN_FAILED TASK_RUN_QUERY SOCIAL_TASK_KEYS run_state task_outcome needs_schedule job_path set_schedule active_run submit_run keep_streams_running workbench_request reconcile_after_tick",
    "pipeline": "encoded DeltaLake _put_object install_post_views install_gold_views ingest_page start_landing consume_landing _consume_format _status _due _source_token _poll_x poll_source publish_snapshot _finish_enrichment enrich_pending _tick process_reset stop_streams run_persistent run",
}
RENAMES = {
    "post_index": {"upsert_posts": "append_posts", "append_purge": "purge_synthetic_posts"},
    "pipeline": {"encoded": "encode_publication", "run": "run_social_network"},
    "sensor_pipeline": {"run": "run_sensor_stream"},
    "synthetic_reset": {"encoded": "encode_reset_publication", "clean_landing": "clean_social_landing", "execute": "execute_social_reset"},
    "sensor_reset": {"clean_landing": "clean_sensor_landing", "execute": "execute_sensor_reset"},
}
MODULE_ALIASES = {"synthetic_reset": {"database", "landing"},
                  "sensor_reset": {"database", "sensor_capture", "sensors", "synthetic_reset"}}
RUNTIME_FIELDS = frozenset("namespace bucket catalog region model_id compartment_id streaming_mode pipeline_revision analytics_store oci_credential_name oci_identity_sha256 workbench_base landing_bucket landing_prefix landing_volume_path checkpoint_volume_path sensor_landing_prefix sensor_landing_volume_path sensor_checkpoint_volume_path workspace_key job_key sensor_job_key gold_query_compute_id agent_compute_id".split())


def source_fragments(bundle, module, names):
    """Copy exact source ranges; source archives are opened only by deployment tooling."""
    names = names.split() if isinstance(names, str) else list(names)
    if len(names) != len(set(names)):
        raise ValueError("Repeated standalone export")
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        path = "gods_eye_view/" + module + ".py"
        if archive.namelist().count(path) != 1:
            raise ValueError("Standalone source must have one exact archive member")
        source = archive.read(path).decode("utf-8")
    lines, found, pieces = source.splitlines(keepends=True), set(), []
    previous = 0
    for node in ast.parse(source, filename=path).body:
        defined = {node.name} if isinstance(node, (ast.FunctionDef, ast.ClassDef)) else {
            target.id for target in getattr(node, "targets", []) if isinstance(target, ast.Name)}
        external_import = isinstance(node, ast.Import) or isinstance(node, ast.ImportFrom) and not node.level and node.module != "__future__"
        if external_import or defined.intersection(names):
            start = min([node.lineno] + [item.lineno for item in getattr(node, "decorator_list", [])]) - 1
            while start > previous and (not lines[start - 1].strip() or lines[start - 1].lstrip().startswith("#")):
                start -= 1
            pieces.append("".join(lines[start:node.end_lineno]).rstrip())
            found.update(defined.intersection(names))
        previous = node.end_lineno
    if found != set(names):
        raise ValueError("Standalone export no longer matches its source")
    return "\n\n".join(pieces) + "\n"


def _standalone_fragment(bundle, module, names, exports):
    source = source_fragments(bundle, module, names)
    lines = source.splitlines(keepends=True)
    # These imports name definitions already printed in this same file. Preserve
    # branch-local aliases (notably the two different reset executors).
    tree = ast.parse(source)
    targets = {(origin, name): RENAMES.get(origin, {}).get(name, name)
               for origin, selected in exports.items() for name in selected.split()}
    # The installed package keeps a legacy database facade; native programs print
    # only its Object Storage implementation, never the Oracle migration branch.
    targets.update({("database", name): target for (origin, name), target in list(targets.items()) if origin == "control_store"})
    if "post_index" in exports:
        targets["database", "purge_synthetic_posts"] = "purge_synthetic_posts"
    single_imports = {id(value[0]) for node in ast.walk(tree) for _, value in ast.iter_fields(node)
                      if isinstance(value, list) and len(value) == 1 and isinstance(value[0], ast.ImportFrom)}
    for node in sorted((node for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.level), key=lambda node: node.lineno, reverse=True):
        if node.level != 1:
            raise ValueError("Unexpected standalone runtime dependency")
        assignments = []
        for alias in node.names:
            if (node.module, alias.name) not in targets:
                raise ValueError("Missing standalone runtime dependency")
            target = targets[node.module, alias.name]
            if (alias.asname or alias.name) != target:
                assignments.append((alias.asname or alias.name) + " = " + target)
        replacement = "; ".join(assignments) or ("pass" if id(node) in single_imports else "")
        lines[node.lineno - 1:node.end_lineno] = [" " * node.col_offset + replacement + "\n"] if replacement else []
    source = "".join(lines)
    offsets, position = [], 0
    for line in source.splitlines(keepends=True):
        offsets.append(position)
        position += len(line)
    tokens = list(tokenize.generate_tokens(io.StringIO(source).readline))
    changes, index = [], 0
    while index < len(tokens):
        token = tokens[index]
        replacement, last = None, token
        if token.type == tokenize.NAME and (index == 0 or tokens[index - 1].string != "."):
            if token.string in MODULE_ALIASES.get(module, ()) and index + 2 < len(tokens) and tokens[index + 1].string == ".":
                member = tokens[index + 2]
                if member.type != tokenize.NAME or (token.string, member.string) not in targets:
                    raise ValueError("Unexpected standalone module member: " + token.string + "." + member.string)
                replacement = targets[token.string, member.string]
                last = member
                index += 2
            elif token.string in RENAMES.get(module, {}):
                replacement = RENAMES[module][token.string]
        if replacement is not None:
            changes.append((offsets[token.start[0] - 1] + token.start[1], offsets[last.end[0] - 1] + last.end[1], replacement))
        index += 1
    for start, end, replacement in reversed(changes):
        source = source[:start] + replacement + source[end:]
    return source


def workflow_source(module, config, bundle):
    if module not in {"pipeline", "sensor_pipeline"}:
        raise ValueError("Unknown standalone workflow")
    required = ("namespace", "bucket", "pipeline_revision", "oci_credential_name", "oci_identity_sha256")
    if not isinstance(config, dict) or any(not isinstance(config.get(key), str) or not config[key] for key in required):
        raise ValueError("Standalone workflow requires explicit deployment configuration")
    config = {key: value for key, value in config.items() if key in RUNTIME_FIELDS}
    json.dumps(config, allow_nan=False)
    exports = SENSOR_EXPORTS if module == "sensor_pipeline" else SOCIAL_EXPORTS
    workflow = "sensor_stream" if module == "sensor_pipeline" else "social_network"
    parts = ['"""God\'s Eye View: readable native ' + workflow + ' processing. No project imports."""\nfrom __future__ import annotations\n',
             "# Deployment configuration contains credential names, never credential values.\nRUNTIME_CONFIG = " + pprint.pformat(config, sort_dicts=True, width=100) + "\n"]
    for name, selected in exports.items():
        parts.append("\n# ---- " + name.replace("_", " ") + " ----\n" + _standalone_fragment(bundle, name, selected, exports))
    parts.append('''
def main():
    import argparse
    from pyspark.sql import SparkSession

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-runtime", action="store_true")
    args = parser.parse_args()
    session = SparkSession.builder.getOrCreate()
    # AIDP Python tasks inject aidputils; it is not an importable Spark package.
    secret_get = aidputils.secrets.get
    if not callable(secret_get):
        raise RuntimeError("Native AIDP secret access is unavailable")
    print(json.dumps({"workflow": "WORKFLOW", "stage": "runtime", "status": "ready",
                      "revision": RUNTIME_CONFIG["pipeline_revision"]}), flush=True)
    if not args.check_runtime:
        RUN_WORKFLOW(session, secret_get, RUNTIME_CONFIG)


if __name__ == "__main__":
    main()
'''.replace("WORKFLOW", workflow).replace("RUN_" + workflow, RENAMES[module]["run"]))
    source = "\n".join(parts)
    compile(source, workflow + ".py", "exec")
    return source
