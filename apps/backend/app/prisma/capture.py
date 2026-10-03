"""Synthetic search input; its clock, filtering and cursors are shared by local and OCI VM producers."""
import re

from .core import folded, simulation_events


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
                    raise ValueError("Consulta sintética inválida: OR requiere términos")
                alternatives.append(terms)
                terms = []
            elif token.upper() == "AND":
                if not terms or position >= len(tokens) or tokens[position].upper() in {"OR", "AND", ")"}:
                    raise ValueError("Consulta sintética inválida: AND requiere términos")
            elif token == "(":
                nested = group()
                if nested == [[]] or position >= len(tokens) or tokens[position] != ")":
                    raise ValueError("Consulta sintética inválida: cierre de paréntesis")
                position += 1
                terms.append(nested)
            else:
                if token == "-" or (":" in token and token != "-is:retweet"):
                    raise ValueError("Sintético admite términos, frases, OR, paréntesis, -término y -is:retweet")
                terms.append(token)
        if not terms and alternatives:
            raise ValueError("Consulta sintética inválida: OR requiere términos")
        return alternatives + [terms]
    expression = group()
    if position != len(tokens) or query.count('"') % 2:
        raise ValueError("Consulta sintética inválida")
    return expression


def matches(text, expression):
    text = folded(text)
    def term(value):
        if isinstance(value, list):
            return any(all(term(item) for item in branch) for branch in value)
        if value == "-is:retweet":
            return True  # Synthetic source records are original posts, including explicit copied-content examples.
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
    if same and not force and not final and now < cursor.get("next_due", 0):
        return None
    expression = search_terms(source["query"])
    events = []
    for event in simulation_events(elapsed, state.get("anchor_at")):
        if event["platform"] != source["platform"] or event["raw_metadata"]["offset_seconds"] <= start:
            continue
        if matches(event["text"], expression):
            events.append({**event, "source_id": f"{state['run_id']}:{event['source_id']}",
                "raw_metadata": {**event["raw_metadata"], "scenario_run_id": state["run_id"], "producer": "vm_search"}})
    return events, {"run_id": state["run_id"], "query": source["query"], "elapsed": elapsed,
                    "next_due": now + source["interval_minutes"] * 60}


def sources(configured):
    return [source for source in configured if source["enabled"] and source["mode"] == "simulation"] + [
        {"platform": name, "query": "", "interval_minutes": 1} for name in ("sensor", "sire", "linea123")]
