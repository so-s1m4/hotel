"""Owner A: normalized event export (implementation pending)."""
import argparse
from pathlib import Path
from ingestion.events import iter_normalized_events
from ingestion.replay import ReplayData


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    # Fail before creating an output when the replay is not implemented.
    events = iter_normalized_events(ReplayData(args.data_dir))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        for event in events:
            stream.write(event.model_dump_json() + "\n")


if __name__ == "__main__":
    main()
