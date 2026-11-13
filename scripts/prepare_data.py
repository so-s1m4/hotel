"""Safely extract local participant ZIPs; never opens the encrypted holdout."""
import argparse
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def prepare(archive_dir: Path, destination: Path, profile: str) -> Path:
    prefix = f"PLATO40_v2_{profile}"
    archive = archive_dir / f"{prefix}.zip"
    destination = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        # Validate every path before writing any file.
        for entry in source.infolist():
            relative = Path(entry.filename)
            target = (destination / relative).resolve()
            if (not relative.parts or relative.parts[0] != prefix
                    or not target.is_relative_to(destination)
                    or (entry.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError(f"Unsafe archive member: {entry.filename}")
        for entry in source.infolist():
            target = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            content = source.read(entry)  # zipfile verifies CRC
            if target.exists():
                if target.read_bytes() != content:
                    raise ValueError(f"Refusing to overwrite modified input: {target}")
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
    return destination / prefix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive-dir", type=Path, default=ROOT)
    parser.add_argument("--destination", type=Path, default=ROOT / "data")
    parser.add_argument("--profile", choices=["quickstart", "dev", "all"], default="all")
    args = parser.parse_args()
    for profile in (["quickstart", "dev"] if args.profile == "all" else [args.profile]):
        print(prepare(args.archive_dir, args.destination, profile))


if __name__ == "__main__":
    main()
