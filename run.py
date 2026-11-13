"""Replay S01–S03 through the team's causal gateway; --check only validates inputs."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from ingestion.loader import inspect_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("decisions.jsonl"))
    parser.add_argument("--check", action="store_true", help="check inputs only; no decisions produced")
    parser.add_argument("--audit", type=Path, help="mock execution JSONL (default: next to decisions)")
    parser.add_argument("--insights", type=Path, help="write an evidence-linked run summary")
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
    if len({p.resolve() for p in destinations}) != len(destinations):
        parser.exit(2, "Output paths must be distinct.\n")
    if any(p.resolve().is_relative_to(args.data_dir.resolve()) for p in destinations):
        parser.exit(2, "Write outputs outside the source dataset directory.\n")
    from ingestion.replay import ReplayData
    from engine.core import EngineConfig
    from engine.planner import DecisionEngine
    from engine.decisions import write_decisions
    from engine.insights import generate_insights
    try:
        building = json.loads((args.data_dir / "building.json").read_text(encoding="utf-8"))
        config = EngineConfig(**json.loads(args.config.read_text(encoding="utf-8"))) if args.config else EngineConfig()
        engine = DecisionEngine(ReplayData(args.data_dir), building, config)
        records = engine.run()
    except NotImplementedError as exc:
        parser.exit(2, f"Replay gateway is not implemented yet: {exc}. Engine scenario tests run independently; use --check for input preflight.\n")
    except (ValueError, KeyError, OSError, TypeError) as exc:
        parser.exit(2, f"Replay failed; decisions were not written: {exc}\n")
    # Do not touch prior outputs unless the entire gateway replay succeeded.
    write_decisions(records, args.out)
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in engine.executor.audit), encoding="utf-8")
    if args.insights:
        args.insights.parent.mkdir(parents=True, exist_ok=True)
        args.insights.write_text(generate_insights(records, metadata), encoding="utf-8")
    print(json.dumps({"status": "replay_complete", "events": engine.event_count,
                      "decisions": len(records), "safety_rejections": len(engine.rejections),
                      "out": str(args.out), "audit": str(audit), "config": asdict(config)}, indent=2))
    rejection_path.write_text("".join(json.dumps(row) + "\n" for row in engine.rejections), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
