"""Build src/spacescans/catalog/ontology.json from spacescans-web's generated ontology.

The web app shows the Spatial and Contextual Exposome Ontology (SPACEO) from JSON files that
its build script generated from the OWL source and its ``scripts/extend_ontology.py`` extended
(``frontend/public/ontology/metadata.json`` and ``parents.json``). This script copies the part
the exposure catalog uses into the package, so that ``spacescans.catalog.concept()`` and
``search_concepts()`` work offline and registration can check an ``ontology_id``:

* every concept under the exposome root ``000093_2`` (Spatial_and_Contextual_Exposome), the
  root included: the subtree the web's Select Exposures step and Data Catalog page show;
* without the ``SPACESCANS_VC_*`` nodes, which stand for the catalog's own result columns
  (``extend_ontology.py``'s VALUE_COL_NODES) rather than for exposure concepts (the statistic
  branch ``000296`` is a separate root and is not copied either: its nodes describe particular
  source variables, mostly ACS estimates);
* each concept's ``label``, ``definition`` and ``parents`` (parents inside the subtree only),
  in depth-first order with siblings sorted by label.

Run from the package repository root:

    python tools/build_ontology_index.py ../spacescans-web

and commit the regenerated file. The output records the web commit and the sha256 of both input
files; the test suite checks that every catalog variable's ``ontology_id`` is in it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = "000093_2"
COLUMN_NODE_PREFIX = "SPACESCANS_VC_"
OUT = Path(__file__).resolve().parents[1] / "src" / "spacescans" / "catalog" / "ontology.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _web_commit(web: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(web), "rev-parse", "HEAD"], check=True,
                             capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return out.stdout.strip() or None


def build(web: Path) -> dict:
    base = web / "frontend" / "public" / "ontology"
    meta_path, parents_path = base / "metadata.json", base / "parents.json"
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    parents = json.loads(parents_path.read_text(encoding="utf-8"))
    if ROOT not in metadata:
        raise SystemExit(f"{meta_path} has no node {ROOT}")

    children: dict[str, list[str]] = {}
    for child, ps in parents.items():
        for p in ps:
            children.setdefault(p, []).append(child)

    order: list[str] = []
    seen: set[str] = set()

    def visit(node: str) -> None:
        if node in seen or node.startswith(COLUMN_NODE_PREFIX):
            return
        seen.add(node)
        order.append(node)
        for child in sorted(children.get(node, []), key=lambda c: (metadata[c]["label"].casefold(), c)):
            visit(child)

    visit(ROOT)
    concepts = {}
    for node in order:
        entry = metadata[node]
        concepts[node] = {
            "label": entry["label"],
            "parents": [p for p in parents.get(node, []) if p in seen] if node != ROOT else [],
            "definition": (entry.get("definition") or "").strip(),
        }
    return {
        "schema_version": 1,
        "ontology": "Spatial and Contextual Exposome Ontology (SPACEO)",
        "root": ROOT,
        "source": {
            "repository": "spacescans-web",
            "commit": _web_commit(web),
            "files": {
                "frontend/public/ontology/metadata.json": _sha256(meta_path),
                "frontend/public/ontology/parents.json": _sha256(parents_path),
            },
            "excluded": f"{COLUMN_NODE_PREFIX}* nodes (the catalog's result columns)",
        },
        "concepts": concepts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("web", type=Path, help="a spacescans-web checkout")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)
    index = build(args.web.resolve())
    args.out.write_text(json.dumps(index, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(index['concepts'])} concepts to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
