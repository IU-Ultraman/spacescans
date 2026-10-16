"""Bridge commands dataset-register / -list / -get / -remove (phase D, track E).

The commands call the registry of ``spacescans.custom`` (PHASE_D.md 2.2:
``register``, ``list_datasets``, ``get_dataset``, ``remove_dataset``). Most
tests replace it with a fake, in-process through the ``registry_api`` seam
and in a real bridge process through a runner that installs the fake first;
the last test runs the real registry when the installed spacescans has one.
"""
from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

from spacescans.bridge import cmd_datasets
from spacescans.bridge.errors import BridgeError, classify


class _Ctx:
    def __init__(self):
        self.lines: list[str] = []

    def log(self, msg: str) -> None:
        self.lines.append(msg)


@dataclasses.dataclass
class _Record:
    id: str
    name: str
    kind: str
    temporal: str
    key: object = None
    variables: list = dataclasses.field(default_factory=list)
    visibility: str = "private"
    files: list = dataclasses.field(default_factory=list)
    created_utc: str = "2026-10-16T20:00:00Z"


class _NotFound(LookupError):
    pass


class FakeRegistry:
    """The PHASE_D 2.2 signatures, records kept in memory."""

    def __init__(self):
        self.records: dict[str, _Record] = {}
        self.calls: list[tuple] = []

    def register(self, paths, *, name, kind, temporal, key=None, variables=None, metadata=None,
                 root=None, overwrite=False):
        self.calls.append(("register", list(paths), {"name": name, "kind": kind, "temporal": temporal,
                                                     "key": key, "variables": variables, "metadata": metadata,
                                                     "root": root, "overwrite": overwrite}))
        if name in {r.name for r in self.records.values()} and not overwrite:
            from spacescans.custom import CustomExposomeError
            raise CustomExposomeError(f"a dataset named {name} is already registered")
        rec = _Record(id=f"{len(self.records) + 1:032x}", name=name, kind=kind, temporal=temporal, key=key,
                      variables=variables or [{"name": "value"}], visibility=(metadata or {}).get(
                          "visibility", "private"), files=[f"prepared/{i}.parquet" for i in range(len(paths))])
        self.records[rec.id] = rec
        return rec

    def list_datasets(self, root=None):
        self.calls.append(("list", root))
        return list(self.records.values())

    def get_dataset(self, name_or_id, root=None):
        self.calls.append(("get", name_or_id, root))
        for r in self.records.values():
            if name_or_id in (r.id, r.name):
                return r
        raise _NotFound(name_or_id)

    def remove_dataset(self, name_or_id, root=None):
        self.calls.append(("remove", name_or_id, root))
        rec = self.get_dataset(name_or_id, root)
        del self.records[rec.id]        # returns nothing: the command reports the record it read first


@pytest.fixture
def fake(monkeypatch):
    reg = FakeRegistry()
    monkeypatch.setattr(cmd_datasets, "registry_api", lambda: reg)
    monkeypatch.delenv("SPACESCANS_DATASETS_DIR", raising=False)
    return reg


@pytest.fixture
def csv(tmp_path):
    p = tmp_path / "values.csv"
    p.write_text("GEOID,score\n18097000100,0.5\n")
    return str(p)


def test_register_passes_the_contract_arguments_and_returns_the_record(fake, csv, tmp_path):
    ctx = _Ctx()
    root = str(tmp_path / "registry")
    value, warns = cmd_datasets.cmd_dataset_register({
        "paths": csv, "name": "svi", "kind": "polygon_table", "temporal": "static", "key": "Tract",
        "variables": ["score", {"name": "other", "unit": "pct", "description": "Other"}],
        "metadata": {"visibility": "deployment", "source": "CDC"}, "root": root, "overwrite": False,
    }, ctx)
    assert warns == []
    (_, paths, kw), = fake.calls
    assert paths == [csv]
    assert kw == {"name": "svi", "kind": "polygon_table", "temporal": "static", "key": "Tract",
                  "variables": [{"name": "score"}, {"name": "other", "unit": "pct", "description": "Other"}],
                  "metadata": {"visibility": "deployment", "source": "CDC"}, "root": root, "overwrite": False}
    assert value["root"] == root
    rec = value["dataset"]
    assert rec["name"] == "svi" and rec["visibility"] == "deployment" and rec["files"] == ["prepared/0.parquet"]
    json.dumps(value)                                    # plain JSON
    assert ctx.lines and "18097000100" not in " ".join(ctx.lines)


def test_register_refuses_bad_arguments_before_calling_the_registry(fake, csv):
    ctx = _Ctx()
    base = {"paths": [csv], "name": "x", "kind": "raster", "temporal": "static"}
    cases = [
        ({**base, "paths": ["relative.csv"]}, "absolute path"),
        ({**base, "paths": []}, "at least one file"),
        ({k: v for k, v in base.items() if k != "name"}, "'name' is required"),
        ({**base, "variables": [{"unit": "m"}]}, "non-empty 'name'"),
        ({**base, "variables": [{"name": "a", "colour": "red"}]}, "unknown variable field"),
        ({**base, "key": 3}, "'key' must be"),
        ({**base, "options": {"name": "y"}}, "options repeat"),
        ({**base, "options": {"band_names": ["a"]}}, "does not accept these arguments"),
    ]
    for args, msg in cases:
        with pytest.raises(BridgeError, match=msg) as ei:
            cmd_datasets.cmd_dataset_register(args, ctx)
        assert ei.value.kind == "config"
    assert fake.calls == []


def test_registry_errors_keep_their_kind(fake, csv):
    ctx = _Ctx()
    args = {"paths": [csv], "name": "svi", "kind": "polygon_table", "temporal": "static"}
    cmd_datasets.cmd_dataset_register(args, ctx)
    with pytest.raises(Exception) as ei:
        cmd_datasets.cmd_dataset_register(args, ctx)
    err = classify(ei.value)
    assert err.kind == "config" and "already registered" in err.message
    value, _ = cmd_datasets.cmd_dataset_register({**args, "overwrite": True}, ctx)
    assert value["dataset"]["name"] == "svi"


def test_list_get_remove(fake, csv, monkeypatch, tmp_path):
    ctx = _Ctx()
    for name in ("a", "b"):
        cmd_datasets.cmd_dataset_register({"paths": [csv], "name": name, "kind": "polygon_table",
                                           "temporal": "static"}, ctx)
    monkeypatch.setenv("SPACESCANS_DATASETS_DIR", str(tmp_path / "env-root"))
    value, _ = cmd_datasets.cmd_dataset_list({}, ctx)
    assert [r["name"] for r in value["datasets"]] == ["a", "b"]
    assert value["root"] == str(tmp_path / "env-root")
    assert fake.calls[-1] == ("list", None)               # the registry resolves the root itself
    rec = cmd_datasets.cmd_dataset_get({"name_or_id": "b"}, ctx)[0]["dataset"]
    assert cmd_datasets.cmd_dataset_get({"name_or_id": rec["id"]}, ctx)[0]["dataset"]["name"] == "b"
    value, _ = cmd_datasets.cmd_dataset_remove({"name_or_id": "a"}, ctx)
    assert value["dataset"]["name"] == "a"
    assert [r["name"] for r in cmd_datasets.cmd_dataset_list({}, ctx)[0]["datasets"]] == ["b"]
    for cmd in (cmd_datasets.cmd_dataset_get, cmd_datasets.cmd_dataset_remove):
        with pytest.raises(BridgeError) as ei:
            cmd({"name_or_id": "a"}, ctx)
        assert ei.value.kind == "config"
        assert ei.value.details["name_or_id"] == "a"
    with pytest.raises(BridgeError, match="'name_or_id' is required"):
        cmd_datasets.cmd_dataset_get({}, ctx)


def test_get_returning_none_and_lookup_errors_with_a_kind(monkeypatch):
    class KindedNotFound(LookupError):
        kind = "data_missing"

    def get_none(ref, root=None):
        return None

    def get_kinded(ref, root=None):
        raise KindedNotFound("gone")

    api = SimpleNamespace(get_dataset=get_none)
    monkeypatch.setattr(cmd_datasets, "registry_api", lambda: api)
    with pytest.raises(BridgeError) as ei:
        cmd_datasets.cmd_dataset_get({"name_or_id": "x"}, _Ctx())
    assert ei.value.kind == "config"
    api.get_dataset = get_kinded
    with pytest.raises(KindedNotFound):
        cmd_datasets.cmd_dataset_get({"name_or_id": "x"}, _Ctx())


def test_record_dict_accepts_the_usual_record_types():
    rec = _Record(id="1", name="n", kind="raster", temporal="static", files=[Path("a/b.parquet")])
    assert cmd_datasets.record_dict(rec)["files"] == ["a/b.parquet"]
    assert cmd_datasets.record_dict({"id": "1", "x": float("nan")}) == {"id": "1", "x": None}

    class WithToDict:
        def to_dict(self):
            return {"id": "2"}

    class Model:
        def model_dump(self, mode="python"):
            return {"id": "3", "mode": mode}

    class Plain:
        def __init__(self):
            self.id = "4"

    assert cmd_datasets.record_dict(WithToDict()) == {"id": "2"}
    assert cmd_datasets.record_dict(Model()) == {"id": "3", "mode": "json"}
    assert cmd_datasets.record_dict(Plain()) == {"id": "4"}


def test_registry_api_reports_a_spacescans_without_the_registry(monkeypatch):
    from spacescans import custom
    for name in cmd_datasets.REGISTRY_API:
        monkeypatch.delattr(custom, name, raising=False)
    with pytest.raises(BridgeError) as ei:
        cmd_datasets.registry_api()
    assert ei.value.kind == "internal"
    assert ei.value.details["missing"] == list(cmd_datasets.REGISTRY_API)


def test_registered_names_and_link_custom():
    assert cmd_datasets.registered_names({}) == []
    assert cmd_datasets.registered_names({"datasets": "svi"}) == ["svi"]
    assert cmd_datasets.registered_names({"datasets": ["svi", "noise", "svi"]}) == ["svi", "noise"]
    with pytest.raises(BridgeError):
        cmd_datasets.registered_names({"datasets": [""]})
    defs = {"custom_0a1b2c3d": {"variable_key": "custom_0a1b2c3d", "label": "Own"}}
    assert cmd_datasets.link_custom(defs, []) is defs
    assert cmd_datasets.link_custom(None, []) is None
    assert cmd_datasets.link_custom(defs, ["svi"]) == ["svi", defs["custom_0a1b2c3d"]]
    assert cmd_datasets.link_custom(None, ["svi", "noise"]) == ["svi", "noise"]


# --- a real bridge process ----------------------------------------------------------

BRIDGE = [sys.executable, "-I", "-u", "-X", "utf8", "-m", "spacescans.bridge"]

# Installs a file-backed fake registry into spacescans.custom, then runs the bridge.
FAKE_RUNNER = textwrap.dedent('''
    import json, os, runpy, sys
    from pathlib import Path
    import spacescans.custom as _c

    def _file(root):
        return Path(root or os.environ["SPACESCANS_DATASETS_DIR"]) / "fake-registry.json"

    def _load(root):
        f = _file(root)
        return json.loads(f.read_text()) if f.exists() else []

    def _save(root, recs):
        f = _file(root)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(recs))

    def register(paths, *, name, kind, temporal, key=None, variables=None, metadata=None,
                 root=None, overwrite=False):
        recs = _load(root)
        if any(r["name"] == name for r in recs):
            if not overwrite:
                raise _c.CustomExposomeError(f"a dataset named {name} is already registered")
            recs = [r for r in recs if r["name"] != name]
        rec = {"id": f"{len(recs) + 1:032x}", "name": name, "kind": kind, "temporal": temporal,
               "key": key, "variables": variables or [{"name": "value"}],
               "visibility": (metadata or {}).get("visibility", "private"),
               "files": ["prepared/values.parquet"], "n_files": len(paths)}
        _save(root, recs + [rec])
        return rec

    def list_datasets(root=None):
        return _load(root)

    def get_dataset(name_or_id, root=None):
        for r in _load(root):
            if name_or_id in (r["id"], r["name"]):
                return r
        raise KeyError(name_or_id)

    def remove_dataset(name_or_id, root=None):
        rec = get_dataset(name_or_id, root)
        _save(root, [r for r in _load(root) if r["id"] != rec["id"]])
        return rec

    _c.register, _c.list_datasets = register, list_datasets
    _c.get_dataset, _c.remove_dataset = get_dataset, remove_dataset
    sys.argv = ["spacescans.bridge"] + sys.argv[1:]
    runpy.run_module("spacescans.bridge", run_name="__main__", alter_sys=True)
''')


def _call(tmp: Path, command: str, args: dict, prefix: list[str], env: dict | None = None):
    req = tmp / f"{command}.request.json"
    res = tmp / f"{command}.result.json"
    res.unlink(missing_ok=True)
    req.write_text(json.dumps({"protocol": 1, "command": command,
                               "client": {"name": "pytest", "version": "0", "protocol_range": [1, 1]},
                               "args": args}))
    p = subprocess.run(prefix + [command, "--request", str(req), "--result", str(res)],
                       check=False, capture_output=True, text=True, cwd=str(tmp), timeout=120, env=env)
    for line in p.stdout.splitlines():
        json.loads(line)                                  # stdout stays JSON Lines
    return p.returncode, json.loads(res.read_text()) if res.exists() else None


def test_hello_lists_the_dataset_commands(tmp_path):
    rc, res = _call(tmp_path, "hello", {}, BRIDGE)
    assert rc == 0
    assert {"dataset-register", "dataset-list", "dataset-get", "dataset-remove"} <= set(res["value"]["commands"])


def test_round_trip_through_a_bridge_process(tmp_path, csv):
    runner = tmp_path / "fake_registry_bridge.py"
    runner.write_text(FAKE_RUNNER)
    prefix = [sys.executable, "-I", "-u", "-X", "utf8", str(runner)]
    root = str(tmp_path / "registry")
    rc, res = _call(tmp_path, "dataset-register",
                    {"paths": [csv], "name": "svi", "kind": "polygon_table", "temporal": "static",
                     "variables": ["score"], "metadata": {"visibility": "private"}, "root": root}, prefix)
    assert rc == 0, res
    assert res["ok"] and res["value"]["dataset"]["variables"] == [{"name": "score"}]
    assert res["value"]["root"] == root
    rc, res = _call(tmp_path, "dataset-register",
                    {"paths": [csv], "name": "svi", "kind": "polygon_table", "temporal": "static",
                     "root": root}, prefix)
    assert rc == 1 and res["error"]["kind"] == "config" and "already registered" in res["error"]["message"]
    rc, res = _call(tmp_path, "dataset-list", {"root": root}, prefix)
    assert rc == 0 and [r["name"] for r in res["value"]["datasets"]] == ["svi"]
    rc, res = _call(tmp_path, "dataset-get", {"name_or_id": "nope", "root": root}, prefix)
    assert rc == 1 and res["error"]["kind"] == "config"
    assert res["error"]["details"]["name_or_id"] == "nope"
    rc, res = _call(tmp_path, "dataset-remove", {"name_or_id": "svi", "root": root}, prefix)
    assert rc == 0 and res["value"]["dataset"]["name"] == "svi"
    rc, res = _call(tmp_path, "dataset-list", {"root": root}, prefix)
    assert res["value"]["datasets"] == []


def test_real_registry_round_trip(tmp_path):
    """Runs only where spacescans.custom has the registry (track B, integrated)."""
    from spacescans import custom
    if not all(callable(getattr(custom, n, None)) for n in cmd_datasets.REGISTRY_API):
        pytest.skip("this spacescans has no dataset registry yet (track B)")
    values = tmp_path / "county_values.csv"
    values.write_text("GEOID,score\n18097,0.5\n18057,0.25\n")
    root = str(tmp_path / "registry")
    rc, res = _call(tmp_path, "dataset-register",
                    {"paths": [str(values)], "name": "county_scores", "kind": "polygon_table",
                     "temporal": "static", "key": "County", "variables": ["score"], "root": root}, BRIDGE)
    assert rc == 0, res["error"]
    rec = res["value"]["dataset"]
    assert rec["name"] == "county_scores" and rec["kind"] == "polygon_table"
    rc, res = _call(tmp_path, "dataset-list", {"root": root}, BRIDGE)
    assert [r["id"] for r in res["value"]["datasets"]] == [rec["id"]]
    rc, res = _call(tmp_path, "dataset-get", {"name_or_id": rec["id"], "root": root}, BRIDGE)
    assert rc == 0 and res["value"]["dataset"]["name"] == "county_scores"
    rc, res = _call(tmp_path, "dataset-remove", {"name_or_id": "county_scores", "root": root}, BRIDGE)
    assert rc == 0
    rc, res = _call(tmp_path, "dataset-list", {"root": root}, BRIDGE)
    assert res["value"]["datasets"] == []

