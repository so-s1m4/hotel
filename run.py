"""Replay S01–S05, energy optimisation and logistics through the team's causal gateway; --check only validates inputs."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
from time import perf_counter

from ingestion.loader import inspect_dataset


def main() -> int:
    started = perf_counter()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("decisions.jsonl"))
    parser.add_argument("--check", action="store_true", help="check inputs only; no decisions produced")
    parser.add_argument("--audit", type=Path, help="mock execution JSONL (default: next to decisions)")
    parser.add_argument("--insights", type=Path, help="write an evidence-linked run summary")
    parser.add_argument("--annual-insights", type=Path, help="measured retrospective report plus companion JSON, after replay")
    parser.add_argument("--llm", action="store_true", help="opt in to configured guest-message model; default source-label offline replay")
    parser.add_argument("--config", type=Path, help="JSON overrides of engine.core.EngineConfig defaults")
    args = parser.parse_args()
    try:
        metadata = inspect_dataset(args.data_dir)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"Input error: {exc}\n")
    if args.check:
        print(json.dumps({"status": "preflight_ok", **metadata}, indent=2))
        return 0
    audit = args.audit or args.out.with_name(args.out.stem + "_audit.jsonl")
    rejection_path = args.out.with_name(args.out.stem + "_rejections.jsonl")
    destinations = [args.out, audit, rejection_path] + ([args.insights] if args.insights else [])
    if args.annual_insights:
        destinations += [args.annual_insights, args.annual_insights.with_suffix(".json")]
    from engine.output_artifacts import preflight_outputs, publish_artifacts
    try:
        preflight_outputs(destinations, args.data_dir, protected_inputs=[args.config] if args.config else [])
    except (ValueError, OSError) as exc:
        parser.exit(2, f"Output preflight failed; prior outputs were not replaced: {exc}\n")
    from ingestion.replay import ReplayData
    from engine.core import EngineConfig
    from engine.planner import DecisionEngine
    from engine.decisions import write_decisions
    from engine.insights import generate_insights
    try:
        building = json.loads((args.data_dir / "building.json").read_text(encoding="utf-8"))
        config = EngineConfig(**json.loads(args.config.read_text(encoding="utf-8"))) if args.config else EngineConfig()
        engine = DecisionEngine(ReplayData(args.data_dir, use_llm=args.llm), building, config)
        records = engine.run()
        annual = None
        if args.annual_insights:
            from engine.annual import analyze_dataset, render_report
            annual = analyze_dataset(args.data_dir)
    except NotImplementedError as exc:
        parser.exit(2, f"Replay gateway is not implemented yet: {exc}. Engine scenario tests run independently; use --check for input preflight.\n")
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Replay failed; decisions were not written: {exc}\n")
    # Serialize every output before publishing any member of the generation.
    try:
        artifacts = {args.out: lambda path: write_decisions(records, path)}
        text_artifacts = {
            audit: "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in engine.executor.audit),
            rejection_path: "".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in engine.rejections),
        }
        if args.insights:
            text_artifacts[args.insights] = generate_insights(records, metadata)
        if args.annual_insights:
            text_artifacts[args.annual_insights] = render_report(annual)
            text_artifacts[args.annual_insights.with_suffix(".json")] = json.dumps(annual, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        artifacts.update({path: (lambda temporary, text=text: temporary.write_text(text, encoding="utf-8"))
                          for path, text in text_artifacts.items()})
        publish_artifacts(artifacts)
    except (ValueError, OSError, TypeError) as exc:
        parser.exit(2, f"Output generation failed; replay_complete was not reached: {exc}\n")
    print(json.dumps({"status": "replay_complete", "events": engine.event_count,
                      "decisions": len(records), "safety_rejections": len(engine.rejections),
                      "out": str(args.out), "audit": str(audit), "config": asdict(config), "notifications_suppressed": engine.notifications.suppressed,
                      "elapsed_seconds": round(perf_counter()-started, 3),
                      "detector_diagnostics": {"water": dict(engine.water.diagnostics)}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
