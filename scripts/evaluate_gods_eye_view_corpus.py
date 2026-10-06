"""Offline fixture evaluation; never import this module from capture or inference.

Usage: python scripts/evaluate_gods_eye_view_corpus.py --predictions processed-claims.json
Predictions: {"posts": [{"fixture_id": "post-0001", "claims": [...]}]}.
Normalized posts may instead carry fixture_id in raw_metadata. Supply one run/cycle.
No predictions means pending, not an invented successful model evaluation.
"""
import argparse
import json
from pathlib import Path


DEFAULT_TRUTH = Path(__file__).resolve().parents[1] / "apps/backend/app/labs/gods_eye_view/source/social_networks/v1/evaluation/ground-truth.json"


def confusion(expected, predicted):
    return {"true_positive": len(expected & predicted), "false_positive": len(predicted - expected),
            "false_negative": len(expected - predicted)}


def prediction_index(posts, identifiers):
    result = {}
    if not isinstance(posts, list):
        raise ValueError("Predictions must contain a posts list")
    for post in posts:
        identifier = post.get("fixture_id") or post.get("raw_metadata", {}).get("fixture_id")
        if identifier not in identifiers or identifier in result:
            raise ValueError("Use known fixture IDs once each from a single run and cycle")
        claims = post.get("claims")
        if claims is not None and (not isinstance(claims, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("category"), str)
                or item.get("relation") not in {"supports", "contradicts"} for item in claims)):
            raise ValueError("Each predicted claim needs category and a textual evidence relation")
        result[identifier] = claims
    return result


def evaluate(truth, predictions=None):
    expected = {post["fixture_id"]: post for post in truth["posts"]}
    if len(expected) != len(truth["posts"]):
        raise ValueError("Evaluation fixtures must have unique IDs")
    indexed = prediction_index(predictions or [], expected)
    ready = {identifier: claims for identifier, claims in indexed.items() if claims is not None}
    categories = {"true_positive": 0, "false_positive": 0, "false_negative": 0}
    relations = dict(categories)
    relation_count = 0
    for identifier, claims in ready.items():
        reference = expected[identifier]
        category = reference["expected_category"]
        wanted = set() if category == "por_clasificar" else {category}
        found = {item["category"] for item in claims if item["category"] != "por_clasificar"}
        for key, value in confusion(wanted, found).items():
            categories[key] += value
        # Copy detection is a separate deterministic stage; the LLM contract has no duplicate label.
        if reference["evidence_role"] not in {"supports", "contradicts"}:
            continue
        relation_count += 1
        wanted_relations = {(category, reference["evidence_role"])} if wanted else set()
        found_relations = {(item["category"], item["relation"]) for item in claims if item["category"] != "por_clasificar"}
        for key, value in confusion(wanted_relations, found_relations).items():
            relations[key] += value
    false_ids = {identifier for identifier, post in expected.items() if post["scenario_assertion"] == "false"}
    return {"dataset_version": truth["dataset_version"],
            "status": "pending" if not ready else "complete" if len(ready) == len(expected) else "partial",
            "fixture_count": len(expected), "evaluated_count": len(ready), "pending_count": len(expected) - len(ready),
            "category_claims": categories if ready else None,
            "textual_relations": {**relations, "evaluated_count": relation_count} if ready else None,
            "scenario_false_assertions": {"fixture_count": len(false_ids), "with_predictions": len(false_ids & ready.keys())},
            "truth_detection": {"status": "not_evaluated",
                "reason": "Claims describe the author's textual stance; supports is not factual verification."},
            "scope": "FP/FN are measured only on supplied predictions; missing predictions remain pending. Textual-relation metrics exclude copy fixtures."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ground-truth", type=Path, default=DEFAULT_TRUTH)
    parser.add_argument("--predictions", type=Path)
    args = parser.parse_args()
    truth = json.loads(args.ground_truth.read_text(encoding="utf-8"))
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))["posts"] if args.predictions else None
    print(json.dumps(evaluate(truth, predictions), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
