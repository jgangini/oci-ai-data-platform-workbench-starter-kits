"""Synthetic search input; its clock, filtering and cursors are shared by local and OCI VM producers."""
import re

from .core import PLATFORMS, folded, simulation_events


def query_lines(value):
    """Each nonempty line is an independent search; preserve its operators and spelling."""
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 10 or any(len(line) > 512 for line in lines):
        raise ValueError("Use up to 10 searches, with at most 512 characters per line")
    return list(dict.fromkeys(lines)) or [""]


def validate_source(source):
    """Apply the same connector/query boundary to local and cloud administration."""
    queries = query_lines(source["query"])
    if source["mode"] == "real":
        if source["platform"] != "x":
            raise ValueError("Only X supports real capture; other platforms support simulation")
        if queries == [""]:
            raise ValueError("Real X capture requires a query")
    else:
        for query in queries:
            search_terms(query)


def search_terms(query):
    """Small explicit grammar: implicit AND, OR, phrases, parentheses and -term; no API emulation."""
    tokens = re.findall(r'-?"[^"\n]+"|[()]|[^\s()]+', query)
    position = 0
    def group():
        nonlocal position
        alternatives, terms = [], []
        while position < len(tokens):
            token = tokens[position]
            if token == ")":
                break
            position += 1
            if token.upper() == "OR":
                if not terms:
                    raise ValueError("Invalid synthetic query: OR requires terms")
                alternatives.append(terms)
                terms = []
            elif token.upper() == "AND":
                if not terms or position >= len(tokens) or tokens[position].upper() in {"OR", "AND", ")"}:
                    raise ValueError("Invalid synthetic query: AND requires terms")
            elif token == "(":
                nested = group()
                if nested == [[]] or position >= len(tokens) or tokens[position] != ")":
                    raise ValueError("Invalid synthetic query: expected closing parenthesis")
                position += 1
                terms.append(nested)
            else:
                if token == "-" or (":" in token and token != "-is:retweet"):
                    raise ValueError("Synthetic searches support terms, phrases, OR, parentheses, -term and -is:retweet")
                terms.append(token)
        if not terms and alternatives:
            raise ValueError("Invalid synthetic query: OR requires terms")
        return alternatives + [terms]
    expression = group()
    if position != len(tokens) or query.count('"') % 2:
        raise ValueError("Invalid synthetic query")
    return expression


def matches(text, expression):
    text = folded(text)
    def term(value):
        if isinstance(value, list):
            return any(all(term(item) for item in branch) for branch in value)
        if value == "-is:retweet":
            return not text.startswith("rt @")
        negative = value.startswith("-")
        word = folded(value.lstrip("-").strip('"').lstrip("#"))
        found = bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", text))
        return not found if negative else found
    return term(expression)


def batch(source, state, cursor, now, force=False):
    if not state.get("run_id") or state["status"] == "idle":
        return None
    same = cursor.get("run_id") == state["run_id"] and cursor.get("query") == source["query"]
    start = cursor.get("elapsed", -1) if same else -1
    elapsed = state["elapsed_seconds"]
    if same and elapsed <= start and not force:
        return None
    if elapsed == 0 and state["status"] == "paused":
        return None
    final = elapsed >= 600 and start < 600
    if same and cursor.get("interval_minutes") == source["interval_minutes"] and not force and not final and now < cursor.get("next_due", 0):
        return None
    events = window_events(source, state["run_id"], state.get("anchor_at"), start, elapsed)
    return events, {"run_id": state["run_id"], "query": source["query"], "elapsed": elapsed,
                    "interval_minutes": source["interval_minutes"],
                    "batch_key": {"platform": source["platform"], "run_id": state["run_id"], "query": source["query"],
                                  "from_seconds": start, "to_seconds": elapsed},
                    "next_due": now + source["interval_minutes"] * 60}


def window_events(source, run_id, anchor_at, start, end):
    """Search one scenario window independently of its replay/continuous scheduling policy."""
    expressions = [(query, search_terms(query)) for query in query_lines(source["query"])]
    events = {}
    for event in simulation_events(end, anchor_at):
        if event["platform"] != source["platform"] or event["raw_metadata"]["offset_seconds"] <= start:
            continue
        matched = [query for query, expression in expressions if matches(event["text"], expression)]
        if matched:
            events[event["source_id"]] = {**event, "source_id": f"{run_id}:{event['source_id']}",
                "raw_metadata": {**event["raw_metadata"], "matched_queries": matched, "scenario_run_id": run_id, "producer": "vm_search"}}
    return list(events.values())


def sources(configured):
    return [source for source in configured if source["enabled"] and source["mode"] == "simulation"] + [
        {"platform": name, "query": "", "interval_minutes": 1} for name in ("sensor", "sire", "linea123")]


def inputs(configured, controls, legacy):
    active = [item for item in configured if item["enabled"] and item["mode"] == "simulation"]
    continuous = any(item.get("capture_running", False) for item in active)
    for source in sources(active):
        institutional = source["platform"] not in {item["platform"] for item in configured}
        if not institutional and source.get("capture_paused", False):
            continue
        running = continuous if institutional else source.get("capture_running", False)
        control = controls.get("institutional" if institutional else source["platform"], {})
        if running and control.get("run_id"):
            yield source, control, True
        elif legacy.get("run_id") and legacy["status"] != "idle":
            yield source, legacy, False


def continuous_batch(source, control, cursor, now, force=False):
    """Repeat the scenario with unique cycle IDs; a saved cursor never skips an overdue window."""
    same = cursor.get("run_id") == control["run_id"] and cursor.get("query") == source["query"]
    start = cursor.get("elapsed", -1) if same else -1
    elapsed = max(0, now - control["anchor_at"])
    if same and not force and (elapsed <= start or (cursor.get("interval_minutes") == source["interval_minutes"] and now < cursor.get("next_due", 0))):
        return None
    # ponytail: catch up at most one hour per VM tick, retaining the rest in the cursor instead of dropping records.
    end = min(elapsed, max(0, start) + 3600)
    generation = continuous_generation(source, control, cursor)
    events = []
    for cycle in range(max(0, int(max(0, start) // 600) - 1), int(end // 600) + 1):
        cycle_start = cycle * 600
        part = continuous_window(source, control, generation, cycle, start - cycle_start, min(600, end - cycle_start))
        for event in part:
            event["raw_metadata"]["capture_run_id"] = control["run_id"]
        events.extend(part)
    return events, {"run_id": control["run_id"], "query": source["query"], "elapsed": end, **generation,
        "interval_minutes": source["interval_minutes"],
        "batch_key": {"platform": source["platform"], "run_id": control["run_id"], "query": source["query"], "from_seconds": start, "to_seconds": end, **generation},
        "next_due": now + (1 if end < elapsed else source["interval_minutes"] * 60)}


def continuous_generation(source, control, cursor):
    if source["platform"] not in PLATFORMS:
        return {}
    if cursor.get("run_id") == control["run_id"]:
        # Existing unversioned checkpoints finish with their original generator; never replay them as a new corpus.
        return {"dataset_version": cursor["dataset_version"], "dataset_seed": cursor.get("dataset_seed", 0)} if cursor.get("dataset_version") else {}
    return {"dataset_version": control.get("dataset_version", "bogota-v1"), "dataset_seed": control.get("seed", 0)}


def continuous_window(source, control, generation, cycle, start, end):
    anchor = control["anchor_at"] + cycle * 600
    if not generation.get("dataset_version"):
        return window_events(source, f"{control['run_id']}:{cycle}", anchor, start, end)
    # Corpus files live on the capture VM; importing query_lines in an AIDP X worker never loads them.
    from .corpus import events
    expressions = [(query, search_terms(query)) for query in query_lines(source["query"])]
    result = []
    for event in events(source["platform"], control["run_id"], cycle, anchor, start, end,
                        seed=generation["dataset_seed"], version=generation["dataset_version"]):
        searchable = event["text"] + " @" + event["username"]
        matched = [query for query, expression in expressions if matches(searchable, expression)]
        if matched:
            event["raw_metadata"]["matched_queries"] = matched
            result.append(event)
    return result
