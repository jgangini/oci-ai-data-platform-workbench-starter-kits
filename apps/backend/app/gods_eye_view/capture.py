"""Synthetic search input; its clock, filtering and cursors are shared by local and OCI VM producers."""
import re
import random
from datetime import datetime, timezone

from .core import SYNTHETIC_MODES, PLATFORMS, folded, simulation_events

INSTITUTIONAL = ("sensor", "sire", "linea123")


def schedule(value=None):
    return {"start_at": None, "interval_minutes": 5, "config_version": 1, **(value or {})}


def update_schedule(saved, payload):
    from fastapi import HTTPException
    saved = schedule(saved)
    if payload.get("expected_revision") != saved["config_version"]:
        raise HTTPException(409, "Capture schedule changed; reload before saving")
    interval = payload.get("interval_minutes")
    if type(interval) is not int or not 1 <= interval <= 1440:
        raise ValueError("Capture interval must be between 1 and 1440 minutes")
    try:
        start = datetime.fromisoformat(payload["start_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError("A valid capture start date and time is required") from exc
    if start.tzinfo is None or start.utcoffset() is None:
        raise ValueError("Capture start date and time must include its UTC offset")
    return {"start_at": start.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "interval_minutes": interval, "config_version": saved["config_version"] + 1}


def schedule_at(value, now, previous=None):
    """Use the latest S+nI slot; missed slots are never replayed."""
    value = schedule(value)
    if not value["start_at"]:
        return None
    start = datetime.fromisoformat(value["start_at"].replace("Z", "+00:00")).timestamp()
    interval = value["interval_minutes"] * 60
    slot = start + max(0, int((now - start) // interval)) * interval
    return slot + interval if previous is not None and previous >= slot else slot


def query_lines(value):
    """Each nonempty line is an independent search; preserve its operators and spelling."""
    if not isinstance(value, str) or len(value.encode("utf-16-le", "surrogatepass")) // 2 > 1000:
        raise ValueError("Searches must contain at most 1000 characters in total")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) > 10 or any(len(line.encode("utf-16-le", "surrogatepass")) // 2 > 512 for line in lines):
        raise ValueError("Use up to 10 searches, with at most 512 characters per line")
    return list(dict.fromkeys(lines)) or [""]


def validate_source(source):
    """Apply the same connector/query boundary to local and cloud administration."""
    if source["mode"] not in (*SYNTHETIC_MODES, "real"):
        raise ValueError("Unsupported source mode")
    maximum = source.get("synthetic_batch_max", 3)
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError("Synthetic batch maximum must be between 1 and 100 records")
    queries = query_lines(source["query"])
    if source["mode"] == "real":
        if source["platform"] != "x":
            raise ValueError("Only X supports real capture; other platforms support Synthetic capture")
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


def batch(source, state, cursor, now, force=False, schedule=None):
    if not state.get("run_id") or state["status"] == "idle" or is_complete(state, cursor):
        return None
    if (schedule_at(schedule, now) or now) > now:
        return None
    if source["platform"] in PLATFORMS:
        return bounded_batch(source, state, cursor, now, force, schedule, state["elapsed_seconds"],
            lambda selected, start, end: window_events(selected, state["run_id"], state.get("anchor_at"), start, end))
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
    return [source for source in configured if source["enabled"] and source["mode"] in SYNTHETIC_MODES] + [
        {"platform": name, "query": "", "interval_minutes": 1} for name in INSTITUTIONAL]


def inputs(configured, controls, legacy):
    active = [item for item in configured if item["enabled"] and item["mode"] in SYNTHETIC_MODES]
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


def is_complete(control, cursor):
    return (bool(control.get("run_id")) and cursor.get("run_id") == control["run_id"]
            and (cursor.get("complete", False) if "emitted_ids" in cursor else cursor.get("elapsed", -1) >= 600))


def institutional_pending(control, institutional, cursors):
    return (bool(control.get("run_id")) and institutional.get("run_id") == control["run_id"]
            and any(not is_complete(institutional, cursors.get(name, {})) for name in INSTITUTIONAL))


def continuous_batch(source, control, cursor, now, force=False, schedule=None):
    """Capture one finite scenario; an exhausted run never republishes later cycles."""
    if is_complete(control, cursor):
        return None
    if (schedule_at(schedule, now) or now) > now:
        return None
    generation = continuous_generation(source, control, cursor)
    if source["platform"] in PLATFORMS:
        return bounded_batch(source, control, cursor, now, force, schedule, min(600, max(0, now - control["anchor_at"])),
            lambda selected, start, end: continuous_window(selected, control, generation, 0, start, end), generation)
    same = cursor.get("run_id") == control["run_id"] and cursor.get("query") == source["query"]
    start = cursor.get("elapsed", -1) if same else -1
    end = min(600, max(0, now - control["anchor_at"]))
    final = end == 600
    if same and not force and (end <= start or (not final and cursor.get("interval_minutes") == source["interval_minutes"] and now < cursor.get("next_due", 0))):
        return None
    generation = continuous_generation(source, control, cursor)
    events = continuous_window(source, control, generation, 0, start, end)
    return events, {"run_id": control["run_id"], "query": source["query"], "elapsed": end, **generation,
        "interval_minutes": source["interval_minutes"],
        "batch_key": {"platform": source["platform"], "run_id": control["run_id"], "query": source["query"], "from_seconds": start, "to_seconds": end, **generation},
        "next_due": None if final else now + source["interval_minutes"] * 60}


def bounded_batch(source, control, cursor, now, force, shared_schedule, end, window, generation=None):
    """Select unseen records; committing this returned cursor belongs to the Landing writer."""
    if is_complete(control, cursor) or (end == 0 and control.get("status") == "paused"):
        return None
    same_run = cursor.get("run_id") == control["run_id"]
    previous = cursor if same_run else {}
    slot = schedule_at(shared_schedule, now, previous.get("capture_slot"))
    if slot is not None and slot > now:
        return None
    if slot is None and same_run and not force and now < (previous.get("next_due") or 0):
        return None
    emitted = set(previous.get("emitted_ids", []))
    if same_run and "emitted_ids" not in previous:
        emitted.update(item["source_id"] for item in window({**source, "query": previous.get("query", source["query"])}, -1, previous.get("elapsed", -1)))
    pending = [item for item in window(source, -1, end) if item["source_id"] not in emitted]
    maximum = source.get("synthetic_batch_max", 3)
    chosen = random.Random(f"{control['run_id']}:{len(emitted)}:{slot}").sample(pending, min(maximum, len(pending)))
    emitted.update(item["source_id"] for item in chosen)
    complete = end >= 600 and len(chosen) == len(pending)
    interval = (shared_schedule or {}).get("interval_minutes", source["interval_minutes"])
    due = schedule_at(shared_schedule, now, slot) if slot is not None else now + interval * 60
    return chosen, {"run_id": control["run_id"], "query": source["query"], "elapsed": end, **(generation or {}),
        "emitted_ids": sorted(emitted), "complete": complete, "capture_slot": slot,
        "interval_minutes": interval, "next_due": None if complete else due,
        "batch_key": {"platform": source["platform"], "run_id": control["run_id"], "query": source["query"],
            "ids": sorted(item["source_id"] for item in chosen), "capture_slot": slot,
            "from_seconds": previous.get("elapsed", -1), "to_seconds": end, **(generation or {})}}


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
        events = window_events(source, f"{control['run_id']}:{cycle}", anchor, start, end)
        for event in events:
            event["raw_metadata"]["capture_run_id"] = control["run_id"]
        return events
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
