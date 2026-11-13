"""Export the normalized event stream.

    PYTHONPATH=. python -m ingestion.cli --data-dir PLATO40_v2_quickstart --out events.jsonl

Works without an API key or network (guest intents then come from the dataset
label with needs_review=true). Use --llm to force model extraction, --no-llm to
forbid it; default follows the environment.
"""
from __future__ import annotations

import argparse
import collections
import json
import logging
import sys
import time
from pathlib import Path

from .replay import ReplayData


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data-dir", required=True, help="one dataset directory (never merged)")
    ap.add_argument("--out", default="events.jsonl", help="output JSONL ('-' = stdout)")
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--llm", dest="use_llm", action="store_true", default=None,
                       help="extract guest intents with the configured model")
    group.add_argument("--no-llm", "--dry-run", dest="use_llm", action="store_false",
                       help="never call a model (source labels only)")
    ap.add_argument("--no-ticks", action="store_true", help="omit generated clock_tick events")
    ap.add_argument("--limit", type=int, default=None, help="write only the first N events")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(levelname)s %(name)s: %(message)s")

    started = time.time()
    repo = ReplayData(args.data_dir, use_llm=args.use_llm, include_ticks=not args.no_ticks)
    counts: collections.Counter[str] = collections.Counter()
    methods: collections.Counter[str] = collections.Counter()
    handle = sys.stdout if args.out == "-" else Path(args.out).open("w", encoding="utf-8")
    try:
        for n, event in enumerate(repo.iter_events()):
            if args.limit is not None and n >= args.limit:
                break
            handle.write(json.dumps(event.model_dump(), ensure_ascii=False,
                                    separators=(",", ":")) + "\n")
            counts[event.event_type] += 1
            if event.extraction is not None:
                methods[event.extraction.method] += 1
    finally:
        if handle is not sys.stdout:
            handle.close()
    from .loader import SERIES_FILES
    for name in SERIES_FILES:  # load once so the de-duplication audit is complete
        if (Path(args.data_dir) / name).exists() and name != "access_log.csv":
            repo.ds.series(name)
    summary = {"data_dir": str(args.data_dir), "out": args.out,
               "events": sum(counts.values()), "by_type": dict(sorted(counts.items())),
               "guest_extraction_methods": dict(methods), "audit": repo.audit,
               "seconds": round(time.time() - started, 2)}
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
