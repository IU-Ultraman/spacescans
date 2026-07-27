# src/spacescans/cli/_quickstart.py
"""`spacescans quickstart` — run bundled end-to-end demo."""
from __future__ import annotations
import os
import sys
import time
from importlib import resources
from pathlib import Path


def cmd_quickstart(args) -> int:
    # Pick output dir
    out_dir = Path(args.output_dir).resolve() if args.output_dir else Path.cwd() / "spacescans-quickstart-out"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Locate bundled resources
    data_root = resources.files("spacescans.resources.data")
    qs_yaml = resources.files("spacescans.resources.configs.quickstart") / "end_to_end.yaml"

    # Verify bundle integrity
    required = ["sample_patients.parquet", "sample_counties.shp"]
    for name in required:
        if not (data_root / name).is_file():
            print(f"Error: bundled resource missing: {name}", file=sys.stderr)
            print("  This indicates a broken wheel. Please reinstall.", file=sys.stderr)
            return 1

    # Copy yaml + set env vars so resolver finds the sample data
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        local_yaml = tmp / "end_to_end.yaml"
        local_yaml.write_bytes(qs_yaml.read_bytes())

        # Materialize bundled data into tmp (importlib resources may be in a zip)
        sample_dir = tmp / "sample"
        sample_dir.mkdir()
        for entry in data_root.iterdir():
            if entry.is_file():
                (sample_dir / entry.name).write_bytes(entry.read_bytes())

        # C2: force-set so --output-dir is never silently ignored
        # I1: snapshot env vars for restore in finally
        _env_keys = ("SPACESCANS_QUICKSTART_DATA_DIR", "SPACESCANS_OUTPUT_DIR")
        saved = {k: os.environ.get(k) for k in _env_keys}
        try:
            os.environ["SPACESCANS_QUICKSTART_DATA_DIR"] = str(sample_dir)
            os.environ["SPACESCANS_OUTPUT_DIR"] = str(out_dir)

            from spacescans.pipeline.runner import Pipeline
            t0 = time.time()
            try:
                result = Pipeline.from_config(local_yaml).run()
            except Exception as e:
                import platform, traceback
                print(f"\n✗ Quickstart failed: {e}", file=sys.stderr)
                print("\nPlease report at https://github.com/xuguang-ai/spacescans-project/issues with:", file=sys.stderr)
                print(f"  Python: {sys.version.split()[0]}", file=sys.stderr)
                print(f"  Platform: {platform.system()} {platform.release()} {platform.machine()}", file=sys.stderr)
                try:
                    from spacescans import __version__
                    print(f"  spacescans-pipeline: {__version__}", file=sys.stderr)
                except Exception:
                    pass
                extras_detected = []
                for ext, mod in [("geo", "rasterio"), ("rda", "pyreadr"), ("hdf4", "pyhdf"), ("nc", "netCDF4")]:
                    try:
                        __import__(mod)
                        extras_detected.append(ext)
                    except ImportError:
                        pass
                print(f"  Extras detected: {', '.join(extras_detected) or 'none'}", file=sys.stderr)
                print(f"  Step: quickstart C3 boundary_overlap_fast on bundled sample_counties", file=sys.stderr)
                print("\n--- full traceback ---", file=sys.stderr)
                traceback.print_exc()
                return 1

            elapsed = time.time() - t0
            print(f"\n✓ Quickstart complete (took {elapsed:.1f}s).")
            print(f"  Output: {result}")
            print(f"\nNext step: spacescans init-config --out ./configs")
            return 0
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
