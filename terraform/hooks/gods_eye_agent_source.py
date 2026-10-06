"""Assemble the one readable AIDP agent file at deployment, never at runtime."""
import ast
import io
import zipfile

from gods_eye_sources import configured_source, source_fragments


def agent_source(config, bundle):
    fields = ("region", "model_id", "compartment_id", "oci_credential_name", "oci_identity_sha256", "catalog", "gold_query_compute_id")
    if not isinstance(config, dict) or any(not isinstance(config.get(key), str) or not config[key] for key in fields):
        raise ValueError("God’s Eye View Gold agent configuration incomplete")
    from app.lab_packs import module_runtime_source
    source = module_runtime_source("gods_eye_view", "notebooks/40_report/ai_gods_eye_view.py").decode("utf-8")
    return configured_source(source, {key: config[key] for key in fields})


def render_agent_source(bundle):
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        source = archive.read("gods_eye_view/agent.py").decode("utf-8").replace("\r\n", "\n")
    lines = source.splitlines(keepends=True)
    # The deployed agent is self-contained. These imports are only for the backend package.
    removed = set()
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "__package__"
                or isinstance(node, ast.ImportFrom) and (node.module or "").startswith("gods_eye_view.")):
            removed.update(range(node.lineno, node.end_lineno + 1))
    source = "".join(line for number, line in enumerate(lines, 1) if number not in removed)
    source = source.replace("from __future__ import annotations\n", "")
    source = source.replace("# AIDP loads the published entrypoint outside its bundled gods_eye_view package.\n", "")
    helpers = [
        ("Scope and provenance", "area", ("parse_bbox",)),
        ("Legacy provenance compatibility", "core", ("canonical_mode",)),
        ("Shared OCI credential", "runtime_secrets", ("SHARED_OCI_CREDENTIAL_NAME", "OCI_CREDENTIALS",
            "identity_hash", "values", "signer", "_oci_values", "_signer")),
        ("Read-only Gold Spark queries", "gold_reader", ("statement", "query")),
    ]
    sections = []
    for title, module, names in helpers:
        fragment = source_fragments(bundle, module, names)
        if module == "gold_reader":
            fragment = fragment.replace("def query(", "def gold_query(")
        sections.append("# %% " + title + "\n" + fragment)
    result = ('"""God\'s Eye View agent: fixed Gold queries and shared AIDP authentication."""\n'
        "from __future__ import annotations\n\n" + "\n\n".join(sections)
        + "\n\n# %% Evidence-grounded conversation\n" + source
        + "\n# %% Deployment configuration (identifiers only; secrets stay in AIDP)\nRUNTIME_CONFIG = " + "{}\n")
    result = result.replace("\r\n", "\n")
    compile(result, "ai_gods_eye_view.py", "exec")
    return result
