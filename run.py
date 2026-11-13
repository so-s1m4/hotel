"""Root entry point. Preflight works; Owner B implements the full replay pipeline."""
import argparse
import json
from pathlib import Path
from ingestion.loader import inspect_dataset


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=Path("decisions.jsonl"))
    parser.add_argument("--check", action="store_true", help="check inputs only; no decisions produced")
    args = parser.parse_args()
    try:
        metadata = inspect_dataset(args.data_dir)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(2, f"Input error: {exc}\n")
    if args.check:
        print(json.dumps({"status": "preflight_ok", **metadata}, indent=2))
        return 0
    parser.exit(2, "Scaffold only: ingestion replay and decision pipeline are not implemented. "
                "Use --check for input validation; see docs/DEVELOPMENT.md.\n")


if __name__ == "__main__":
    raise SystemExit(main())
