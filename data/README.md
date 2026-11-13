# Local datasets

Place the organizer's `PLATO40_v2_quickstart.zip` and `PLATO40_v2_dev.zip` in the
repository root, or pass their directory with `--archive-dir`:

```bash
python scripts/prepare_data.py --archive-dir /path/to/downloads
```

This creates `data/PLATO40_v2_quickstart/` and `data/PLATO40_v2_dev/` independently.
Reruns verify existing contents and refuse to overwrite modified files.
Archives and extracted data are ignored by Git. Obtain them from the organizer's
Discord/download; no download URL was supplied with the local material.
Encrypted holdout is intentionally not handled by this script. After official
release, pass the separately unpacked holdout directory to the final pipeline.

Attribution: PLATO-40 synthetic dataset, Proximata Hackathon '26 – Rhomberg track.
Datasets: CC BY 4.0; project code: MIT. The property and all guest data are fictional.
