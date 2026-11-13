"""Evidence-linked run summary. Never claims labelled detection accuracy or savings."""
from collections import Counter


def generate_insights(records, metadata: dict) -> str:
    """Summarise actual decisions; the run's coverage is explicit, not assumed annual."""
    episodes = Counter()
    unique = set()
    for r in records:
        key = (r.issue_type, r.episode_id)
        if key not in unique:
            unique.add(key)
            episodes[r.issue_type] += 1
    refs = list(dict.fromkeys(ref for r in records for ref in r.evidence))
    lines = ["# PLATO-40 replay findings", "",
             f"Coverage: {metadata.get('profile', 'unknown')}, {metadata.get('start_utc', '?')}..{metadata.get('end_utc', '?')}.",
             "Fictional synthetic property; no claim about real PLATO/CLARA buildings.", "",
             "## Observed engine output", "",
             f"{len(records)} proposed decisions across {len(unique)} issue episodes.",
             "These are rule-based candidate incidents, not ground-truth detections or measured improvements.", ""]
    for issue, count in sorted(episodes.items()):
        lines.append(f"- {issue}: {count} episodes")
    if records:
        lines.extend(["", "## Recommendations to investigate", "",
            "CLARA: evaluate state/availability-aware alert suppression and explicit verification workflows. "
            "The episodes below identify where the prototype proposed coordination or investigation; "
            "review false positives and missed cases before changing operating rules.", "",
            "PLATO: investigate whether additional room-release/inspection and common-area metering "
            "signals would resolve the uncertainty in these proposals. This is a design hypothesis, "
            "not an established defect or a quantified retrofit benefit.", "",
            "Supporting source windows used by this run (first 20 unique references):", ""])
        lines.extend(f"- `{ref}`" for ref in refs[:20])
    else:
        lines.extend(["No supported recommendation can be inferred from an empty decision log."])
    lines.extend(["", "## Target architecture", "",
        "Causal source adapters → normalized events / indexed historical gateway → operations, water "
        "and energy detectors → shared resource planner → central safety/approval gate → mock execution "
        "audit → observed verification → human-reviewed learning loop. A real deployment would need "
        "authenticated command adapters, durable approvals/consent and resource reservations; this prototype "
        "only proposes simulated effects.", "",
        "## Limitations", "",
        "No ground truth, calibrated confidence, measured savings or verified repairs. "
        "Thresholds and task durations are documented prototype assumptions. Dataset history is immutable. "
        "This decision summary alone is insufficient for an annual building-design finding; inspect "
        "the underlying full-year measurements and validate alternatives before the final submission.", ""])
    return "\n".join(lines)
