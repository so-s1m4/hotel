"""Stream every CSV and JSONL; report schema, counts, blanks and telemetry duplicates."""
import argparse
from collections import Counter
import csv
import hashlib
import json
from pathlib import Path

from ingestion.loader import inspect_dataset


def audit(root: Path) -> dict:
    result = {"metadata": inspect_dataset(root), "files": {}}
    for path in sorted(root.iterdir()):
        if not path.is_file():
            continue
        with path.open("rb") as binary:
            digest = hashlib.file_digest(binary, "sha256").hexdigest()
        info = {"bytes": path.stat().st_size, "sha256": digest}
        if path.suffix == ".csv":
            with path.open(encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                info["columns"] = reader.fieldnames
                blanks = Counter()
                seen = set()
                duplicates = 0
                count = 0
                for row in reader:
                    count += 1
                    if None in row or any(value is None for value in row.values()):
                        raise ValueError(f"Malformed CSV: {path}:{count + 1}")
                    blanks.update(key for key, value in row.items() if value == "")
                    if path.name == "room_telemetry.csv":
                        key = (row["timestamp_utc"], row["room_id"])
                        duplicates += key in seen
                        seen.add(key)
                info.update(rows=count, blank_cells=dict(blanks))
                if path.name == "room_telemetry.csv":
                    info["duplicate_timestamp_room_keys"] = duplicates
        elif path.suffix == ".jsonl":
            types = Counter()
            count = 0
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    record = json.loads(line)
                    count += 1
                    types[str(record.get("type", record.get("action", "other")))] += 1
            info.update(rows=count, types=dict(types))
        elif path.suffix == ".json":
            info["top_level_keys"] = list(json.loads(path.read_text(encoding="utf-8")))
        result["files"][path.name] = info
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.data_dir)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["metadata"], ensure_ascii=False))


if __name__ == "__main__":
    main()
