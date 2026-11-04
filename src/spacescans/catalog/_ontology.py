"""The exposome ontology concepts a dataset or variable can be annotated with.

``ontology.json`` is the part of the Spatial and Contextual Exposome Ontology (SPACEO) that
the exposure catalog uses: every concept under the exposome root ``000093_2``
(Spatial_and_Contextual_Exposome), with its label, definition and parents. It is generated
from spacescans-web's ontology files by ``tools/build_ontology_index.py`` (which records the web
commit and the files' sha256). Two kinds of node stay out: the web's ``SPACESCANS_VC_*`` nodes,
which stand for the catalog's own result columns rather than exposure concepts, and the
ontology's statistic branch (``000296``), whose nodes describe particular source variables
(mostly American Community Survey estimates with their own source and years); annotating
another source's variable with one would misstate where it comes from.

Catalog variables carry one of these ids as ``ontology_id``, and the web app places them in
its ontology tree by it. A registered dataset, and each of its variables, may carry one too
(``spacescans.custom.register(metadata={"ontology_id": ...})``); registration checks it with
:func:`concept`. :func:`search_concepts` finds candidates by label or definition offline.
"""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from typing import Any

from spacescans.catalog._registry import CatalogError

_PACKAGE = "spacescans.catalog"
ONTOLOGY_FILE = "ontology.json"
#: The exposome root: Spatial_and_Contextual_Exposome.
ROOT_CONCEPT = "000093_2"
_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")


@dataclass(frozen=True)
class Concept:
    """One ontology concept.

    ``label`` is the ontology's label (words joined by underscores, as in the web's tree);
    ``parents`` the ids of its parents in the index; ``path`` the labels from the root down to
    it along the first parent of each concept.
    """

    id: str
    label: str
    definition: str
    parents: tuple[str, ...]
    path: tuple[str, ...]

    @property
    def name(self) -> str:
        """The label with spaces for underscores ("Particulate Matter PM2.5")."""
        return self.label.replace("_", " ")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "label": self.label, "name": self.name,
                "definition": self.definition, "parents": list(self.parents),
                "path": list(self.path)}


@lru_cache(maxsize=1)
def _index() -> dict[str, Any]:
    text = resources.files(_PACKAGE).joinpath(ONTOLOGY_FILE).read_text(encoding="utf-8")
    return json.loads(text, object_pairs_hook=OrderedDict)


@lru_cache(maxsize=1)
def _concepts() -> OrderedDict[str, Concept]:
    raw: Mapping[str, Mapping[str, Any]] = _index()["concepts"]
    paths: dict[str, tuple[str, ...]] = {}

    def path(node: str, guard: frozenset[str] = frozenset()) -> tuple[str, ...]:
        if node in paths:
            return paths[node]
        parents = [p for p in raw[node].get("parents") or [] if p in raw and p not in guard]
        own = (str(raw[node]["label"]),)
        result = (path(parents[0], guard | {node}) + own) if parents else own
        paths[node] = result
        return result

    return OrderedDict(
        (cid, Concept(id=cid, label=str(entry["label"]),
                      definition=str(entry.get("definition") or ""),
                      parents=tuple(str(p) for p in entry.get("parents") or []),
                      path=path(cid)))
        for cid, entry in raw.items()
    )


def concepts() -> OrderedDict[str, Concept]:
    """Every concept in the index, root first (depth first, siblings by label)."""
    return OrderedDict(_concepts())


def ontology_source() -> dict[str, Any]:
    """Where the index came from: the ontology's name, its root and the web files' sha256."""
    index = _index()
    return {"ontology": index.get("ontology"), "root": index.get("root"),
            "source": json.loads(json.dumps(index.get("source") or {}))}


def _words(text: str) -> list[str]:
    return _WORD.findall(text.replace("_", " ").lower())


def _matches(token: str, words: list[str]) -> bool:
    squashed = token.replace(".", "")
    return any(w.startswith(token) or w.replace(".", "").startswith(squashed) for w in words)


def search_concepts(text: str, *, limit: int = 10) -> list[Concept]:
    """Concepts whose label (first) or definition matches every word of ``text``.

    Matching ignores case and underscores, compares word beginnings ("pollut" finds
    Air_Pollutant) and also ignores dots ("PM25" finds Particulate_Matter_PM2.5). An exact id
    comes first, then an exact label, a label that starts with the words, a label holding them
    all, and last a match in the definition; ties go to the shorter label, then the shallower
    concept. Blank text finds nothing.
    """
    if not isinstance(text, str):
        raise CatalogError("search text must be a string")
    query = _words(text)
    wanted = text.strip().casefold()
    if not query:
        return []
    joined = "".join(query).replace(".", "")
    scored = []
    for order, c in enumerate(_concepts().values()):
        label = _words(c.label)
        if c.id.casefold() == wanted:
            score = 100
        elif "".join(label).replace(".", "") == joined:
            score = 90
        elif len(label) >= len(query) and all(_matches(q, [w]) for q, w in zip(query, label)):
            score = 80
        elif all(_matches(q, label) for q in query):
            score = 70
        elif all(_matches(q, label + _words(c.definition)) for q in query):
            score = 40
        else:
            continue
        scored.append((-score, len(c.label), len(c.path), order, c))
    scored.sort(key=lambda item: item[:4])
    return [item[-1] for item in scored[:max(0, int(limit))]]


def concept(concept_id: str) -> Concept:
    """The concept with this id; :class:`CatalogError` (a ``ValueError``) naming close matches."""
    if not isinstance(concept_id, str) or not concept_id.strip():
        raise CatalogError("an ontology concept is given by its id (a non-empty string), "
                           "for example '000102_2'")
    found = _concepts().get(concept_id.strip())
    if found is not None:
        return found
    close = search_concepts(concept_id, limit=5)
    hint = ("; closest: " + ", ".join(f"{c.id} ({c.label})" for c in close)) if close else ""
    raise CatalogError(f"unknown ontology concept {concept_id.strip()!r}{hint}. Find concepts "
                       "with spacescans.catalog.search_concepts()")
