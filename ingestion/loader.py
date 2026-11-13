"""Dataset metadata preflight. Owner A adds indexed time-causal row access."""
import json
from pathlib import Path

REQUIRED_FILES = (
    "building.json", "manifest.json", "bookings.csv", "access_log.csv",
    "room_telemetry.csv", "room_water.csv", "water_main.csv", "energy.csv",
    "weather.csv", "weather_forecast.csv", "tariff.csv", "equipment.csv",
    "logistics.csv", "staff_roster.csv", "work_orders.csv", "guest_requests.csv",
    "events.jsonl",
)


def inspect_dataset(data_dir: str | Path) -> dict:
    """Check required inputs and return metadata, without claiming replay support."""
    root = Path(data_dir)
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    if missing:
        raise ValueError(f"Missing dataset files in {root}: {', '.join(missing)}")
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    building = json.loads((root / "building.json").read_text(encoding="utf-8"))
    return {
        "model_id": manifest["model_id"], "profile": manifest["profile"],
        "start_utc": manifest["start_utc"], "end_utc": manifest["end_utc"],
        "rooms": len(building["building"]["rooms"]),
        "required_files": len(REQUIRED_FILES),
    }
