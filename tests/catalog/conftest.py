"""Fixtures for the spacescans.catalog tests.

``web_parity`` is the golden file ``tests/fixtures/catalog/web_parity.json``:
what spacescans-web's own code did (plans, rendered YAML hashes, cache keys,
merge tables, registry answers) at the commit it records. ``web_probe.py``
regenerates it. When a spacescans-web checkout is reachable
(``SPACESCANS_WEB_REPO``, else a ``spacescans-web`` directory next to this
repository) the ``live_probe`` fixture reruns the probe against it; set
``SPACESCANS_WEB_REPO=`` (empty) to skip those tests.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "catalog" / "web_parity.json"
PROBE = HERE / "web_probe.py"
RUN_TOKEN = "{RUN}"


@pytest.fixture(scope="session")
def web_parity() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def web_checkout() -> Path | None:
    env = os.environ.get("SPACESCANS_WEB_REPO")
    if env is not None:
        return Path(env) if env.strip() else None
    candidate = REPO.parent / "spacescans-web"
    return candidate if (candidate / "backend" / "app").is_dir() else None


@pytest.fixture(scope="session")
def web_repo() -> Path:
    web = web_checkout()
    if web is None or not (web / "backend" / "app" / "experiments").is_dir():
        pytest.skip("no spacescans-web checkout (set SPACESCANS_WEB_REPO)")
    return web


@pytest.fixture(scope="session")
def live_probe(web_repo: Path, tmp_path_factory) -> dict:
    # The web's variable_registry imports jsonschema and its version probe needs pyreadr.
    for module in ("jsonschema", "pyreadr", "pandas", "pyarrow"):
        if importlib.util.find_spec(module) is None:
            pytest.skip(f"the web probe needs {module}")
    out = tmp_path_factory.mktemp("web-probe") / "probe.json"
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [sys.executable, "-B", str(PROBE), "--web", str(web_repo), "--texts", "--out", str(out)],
        capture_output=True, text=True, env=env, cwd=str(out.parent), timeout=600, check=False,
    )
    assert proc.returncode == 0, f"web probe failed:\n{proc.stderr[-4000:]}"
    return json.loads(out.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# reference renderer: the render contract of spacescans.catalog.recipes
# ---------------------------------------------------------------------------

def _set(cfg: dict, dotted: str, value, *, create: bool) -> None:
    *parents, leaf = dotted.split(".")
    node = cfg
    for part in parents:
        if create:
            node = node.setdefault(part, {})
        else:
            if not isinstance(node.get(part), dict):
                raise RuntimeError(f"template has no mapping {part!r} for {dotted!r}")
            node = node[part]
    node[leaf] = value


def render_reference(step, *, run_dir: Path, buffer_m, raster_res_m, name_suffix: str) -> str:
    """Follow the render contract in spacescans.catalog.recipes, return the YAML text."""
    import yaml

    from spacescans.catalog import template

    cfg = template(step.template)
    cfg["name"] = f"{cfg['name']}{name_suffix}"
    cfg["buffer"]["patient_file"] = str(run_dir / "input.parquet")
    cfg["buffer"]["buffer_m"] = buffer_m
    if step.uses_raster_res:
        cfg["buffer"]["raster_res_m"] = raster_res_m
    for path, producer in step.bind.items():
        _set(cfg, path, str(run_dir / "output" / f"{producer}.parquet"), create=False)
    for path, value in step.fill.items():
        _set(cfg, path, copy.deepcopy(value), create=True)
    if step.time_block == "always":
        cfg.setdefault("time", {})
    if "time" in cfg:
        cfg["time"]["output_grouping"] = "episode"
    cfg["output"]["path"] = str(run_dir / "output" / f"{step.name}.parquet")
    return yaml.safe_dump(cfg, sort_keys=False)


def normalised_render(step, *, task_name: str, combo: str, root: Path) -> str:
    buf, res = (int(x) for x in combo.split("/"))
    run_dir = root / task_name
    text = render_reference(step, run_dir=run_dir, buffer_m=buf, raster_res_m=res,
                            name_suffix=f"_task_{task_name[-8:]}")
    return text.replace(str(run_dir), RUN_TOKEN)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def scenario_overlay(record: dict, parity: dict):
    """The overlay a plan scenario passed to the web runner."""
    spec = record.get("overlay")
    if spec is None:
        return None
    if spec == "all":
        return copy.deepcopy(parity["custom_definitions"])
    return copy.deepcopy(spec)


def scenario_id(record: dict) -> str:
    return f"{record['experiment']}[{','.join(record['keys'])}]"


class Helpers:
    """The helpers above, as a fixture (test modules cannot import conftest)."""

    render_reference = staticmethod(render_reference)
    normalised_render = staticmethod(normalised_render)
    sha256_text = staticmethod(sha256_text)
    scenario_overlay = staticmethod(scenario_overlay)
    scenario_id = staticmethod(scenario_id)
    RUN_TOKEN = RUN_TOKEN
    # Only a path prefix (nothing is written there): absolute and YAML-plain.
    render_root = Path("/ss-catalog-test/runs")


@pytest.fixture(scope="session")
def helpers() -> type[Helpers]:
    return Helpers


def pytest_generate_tests(metafunc) -> None:
    """Parametrize over the golden file's scenarios (read at collection time)."""
    wanted = {"plan_record", "planned_record", "group_record", "feature_record", "runner_name"}
    names = wanted & set(metafunc.fixturenames)
    if not names:
        return
    parity = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if "plan_record" in names:          # every scenario, errors included
        records = parity["plans"]
        metafunc.parametrize("plan_record", records, ids=[scenario_id(r) for r in records])
    if "planned_record" in names:       # the scenarios that planned steps
        records = [r for r in parity["plans"] if "error" not in r]
        metafunc.parametrize("planned_record", records, ids=[scenario_id(r) for r in records])
    if "group_record" in names:
        records = parity["registry"]["groups"]
        metafunc.parametrize(
            "group_record", records,
            ids=[f"{','.join(r['keys']) or 'empty'}{'+overlay' if r['overlay'] else ''}"
                 for r in records])
    if "feature_record" in names:
        records = parity["features"]
        metafunc.parametrize("feature_record", records,
                             ids=[f"cols{i}" for i in range(len(records))])
    if "runner_name" in names:
        metafunc.parametrize("runner_name", list(parity["runners"]))
