"""Small, cited relationship views; no model-authored topology or executable UI."""

import json
import re
from urllib.parse import unquote
from uuid import uuid4

from .governance_graph import validate_consultation_evidence
from .inventory import identity


def graph_label(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 1024:
        raise ValueError("Invalid relationship label")
    # Text supports Markdown. Treat catalog names as literals, not formatting.
    value = " ".join(value.split())
    value = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", value)


def governance_relationship_graph(state):
    """Project admitted, cited direct edges, preserving their direction and basis."""
    if state["intent"]["intent"] not in {"lineage", "impact", "usage"}:
        return None
    if state["response"]["result_state"] not in {"answered", "partial"}:
        return None
    validate_consultation_evidence(state["evidence"], state["assets"])
    cited = state["response"]["cited_evidence_ids"]
    supported = {key for claim in state["review"]["supported_claims"] for key in claim["evidence_ids"]}
    known = {proof["evidence_id"] for proof in state["evidence"]}
    if len(set(cited)) != len(cited) or not set(cited).issubset(known & supported):
        raise ValueError("A relationship view requires reviewed answer sources")
    nodes, edges, partial = {}, [], False
    for proof in state["evidence"]:
        if proof["evidence_id"] not in cited or proof["kind"] not in {"RUNTIME_LINEAGE", "STATIC_CODE"}:
            continue
        local_nodes, local_edges = relationship_candidates(proof)
        partial |= relationship_coverage_partial(proof)
        for source, target, label in local_edges:
            # Namespace native IDs by observation, never merge on a display name.
            ids = [identity("graph-node", proof["evidence_id"], key)[:20] for key in (source, target)]
            if len(edges) >= 10 or len(set(nodes).union(ids)) > 8:
                partial = True
                continue
            edge_key = (ids[0], ids[1], label, proof["evidence_id"])
            if any(edge["key"] == edge_key for edge in edges):
                continue
            for key, node_id in zip((source, target), ids):
                nodes[node_id] = {"id": node_id, **local_nodes[key], "evidence_id": proof["evidence_id"],
                    "asset_version_id": proof["version_id"]}
            edges.append({"key": edge_key, "source": ids[0], "target": ids[1], "label": label,
                "evidence_id": proof["evidence_id"], "basis": proof["kind"]})
    if not edges:
        return None
    return {"nodes": list(nodes.values()), "edges": [{k:v for k,v in edge.items() if k != "key"} for edge in edges],
        "partial": partial, "execution_proven": False,
        "notice": "Vista acotada de relaciones observadas; no acredita ejecución ni ausencia de otras dependencias."
            + (" Cobertura parcial." if partial else "")}


def relationship_coverage_partial(proof):
    facts = proof["facts"]
    anchor = relationship_native_name(facts.get("anchor"))
    anchor_present = isinstance(anchor, str) and any(
        isinstance(row.get("qualifiedName"), str) and (
            relationship_native_name(row["qualifiedName"]) == anchor
            or relationship_native_name(row["qualifiedName"]).startswith(anchor + "."))
        for row in facts.get("nodes", []))
    return (proof["coverage"] != "complete" or proof["kind"] == "STATIC_CODE"
        or facts.get("pagination_complete") is not True or facts.get("output_truncated") is True
        or not anchor_present or bool(facts.get("omitted_nodes")) or bool(facts.get("unresolved_links")))


def relationship_native_name(value):
    if isinstance(value, str) and re.match(r"^aidp://catalogs@[^/]+/o/", value):
        return unquote(value.partition("/o/")[2])
    return value


def relationship_candidates(proof):
    facts = proof["facts"]
    if proof["kind"] == "STATIC_CODE":
        return static_relationship_candidates(facts)
    return lineage_relationship_candidates(facts)


def static_relationship_candidates(facts):
    target = graph_label(facts["target"])
    if not isinstance(facts["references"], list) or len(facts["references"]) > 128:
        raise ValueError("Static references exceed their budget")
    nodes = {"asset": {"label": target, "kind": "Activo"}}
    edges = []
    for index, ref in enumerate(facts["references"]):
        if type(ref["line"]) is not int or not 1 <= ref["line"] <= 2097152:
            raise ValueError("Invalid static relationship locator")
        file_id, task_id = f"file-{index}", f"task-{index}"
        nodes[file_id] = {"label": graph_label(ref["source_path"]) + f" · línea {ref['line']}", "kind": "Código"}
        task_label = ref["task_key"]
        if ref.get('job_name') is not None:
            task_label = ref['job_name'] + ' · ' + task_label
        nodes[task_id] = {"label": graph_label(task_label), "kind": "Tarea"}
        edges.extend([(task_id, file_id, "archivo de la tarea"), (file_id, "asset", "referencia estática")])
    return nodes, edges


def lineage_relationship_candidates(facts):
    rows, links = facts["nodes"], facts["links"]
    if not isinstance(rows, list) or len(rows) > 16 or not isinstance(links, list) or len(links) > 32:
        raise ValueError("Relationship observation exceeds its budget")
    nodes = {}
    for row in rows:
        key = row["id"]
        if not isinstance(key, str) or not key or key in nodes:
            raise ValueError("Ambiguous native relationship identity")
        nodes[key] = {"label": graph_label(relationship_native_name(row.get("qualifiedName")) or row.get("displayName") or key),
            "kind": graph_label(row.get("type") or "Componente")}
    edges = []
    for link in links:
        source, target = link["fromNodeId"], link["toNodeId"]
        if source not in nodes or target not in nodes:
            raise ValueError("Unresolved relationship endpoint")
        edges.append((source, target, "relación observada"))
    return nodes, edges


def governance_a2ui_enabled(kwargs):
    """Accept only the known v0.9 catalog; absent/foreign capabilities use text."""
    metadata = kwargs.get("metadata")
    if isinstance(metadata, dict) and "a2uiClientCapabilities" in metadata:
        caps = metadata["a2uiClientCapabilities"]
    else:
        variables = kwargs.get("session_variables") or {}
        if not isinstance(variables, dict):
            return False
        caps = variables.get("a2uiClientCapabilities", variables.get("sessionvariables.a2uiClientCapabilities"))
        if isinstance(caps, dict) and "value" in caps:
            caps = caps["value"]
        if isinstance(caps, str) and len(caps) <= 8192:
            try:
                caps = json.loads(caps)
            except ValueError:
                return False
    catalog = "/a2ui_specification/2.0.0/agent_hub_a2ui_custom_component_catalog.json"
    return (isinstance(caps, dict) and caps.get("enabled") is not False
        and isinstance(caps.get("supportedCatalogIds"), list) and catalog in caps["supportedCatalogIds"]
        and not caps.get("inlineCatalogs") and caps.get("version", "v0.9") in ("v0.9", "0.9"))


def relationship_text(graph, cited):
    nodes = {node["id"]: node for node in graph["nodes"]}
    lines = ["Relaciones observadas:"]
    for edge in graph["edges"]:
        source, target = nodes[edge["source"]], nodes[edge["target"]]
        lines.append(f"- {source['kind']}: {source['label']} → {target['kind']}: {target['label']}"
            + f" ({edge['label']}; fuente [{cited.index(edge['evidence_id']) + 1}]).")
    return "\n".join([*lines, graph["notice"]])


def governance_a2ui_content(text, graph, cited, surface_key):
    """Use the AIDP A2A-parts text transport, with a fixed basic component set."""
    nodes = {node["id"]: node for node in graph["nodes"]}
    rows = [f"edge-{index}" for index in range(len(graph["edges"]))]
    components = [{"id": "root", "component": "Column", "children": ["title", *rows, "coverage"]},
        {"id": "title", "component": "Text", "text": "Relaciones observadas", "variant": "h5"}]
    for index, edge in enumerate(graph["edges"]):
        prefix = rows[index]
        components.append({"id": prefix, "component": "Column", "children": [prefix + "-flow", prefix + "-source"]})
        components.append({"id": prefix + "-flow", "component": "Row", "align": "center",
            "children": [prefix + "-left", prefix + "-arrow", prefix + "-right"]})
        for side, key in (("left", edge["source"]), ("right", edge["target"])):
            component_id = prefix + "-" + side
            node = nodes[key]
            components.extend([{"id": component_id, "component": "Card", "child": component_id + "-text"},
                {"id": component_id + "-text", "component": "Text", "text": node["kind"] + ": " + node["label"]}])
        components.extend([{"id": prefix + "-arrow", "component": "Text", "text": "→"},
            {"id": prefix + "-source", "component": "Text", "variant": "caption",
                "text": edge["label"] + f" · fuente [{cited.index(edge['evidence_id']) + 1}]"}])
    components.append({"id": "coverage", "component": "Text", "text": graph["notice"], "variant": "caption"})
    surface_id = "dg-" + identity("relationship-surface", surface_key, uuid4().hex)[:24]
    operations = [{"version": "v0.9", "createSurface": {"surfaceId": surface_id,
        "catalogId": "/a2ui_specification/2.0.0/agent_hub_a2ui_custom_component_catalog.json"}},
        {"version": "v0.9", "updateComponents": {"surfaceId": surface_id, "components": components}}]
    parts = [{"root": {"kind": "text", "text": text}}]
    parts.extend({"root": {"kind": "data", "data": operation,
        "metadata": {"mimeType": "application/json+a2ui"}}} for operation in operations)
    return "A2UI\n" + json.dumps(parts, ensure_ascii=True, separators=(",", ":"))


def governance_visual_response(state, text, kwargs, surface_key):
    # A malformed optional visual must not replace the validated textual answer.
    try:
        graph = governance_relationship_graph(state)
    except (KeyError, TypeError, ValueError):
        return text, None
    if graph is None:
        return text, None
    cited = state["response"]["cited_evidence_ids"]
    if governance_a2ui_enabled(kwargs):
        return governance_a2ui_content(text, graph, cited, surface_key), graph
    return text + "\n\n" + relationship_text(graph, cited), graph
