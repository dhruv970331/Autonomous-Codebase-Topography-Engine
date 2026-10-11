"""
Validation on real code ACTE's authors did not write: pinned releases of rich, httpx, click
and flask, built with jedi and checked against tests/data/real_repos.json.

Skipped unless ACTE_REAL_REPOS=1, since it needs git and the network on first run and takes
a few minutes:

    ACTE_REAL_REPOS=1 poetry run pytest tests/test_real_repos.py -s

Each repo is shallow-cloned at its tag into $ACTE_CORPUS_DIR (default ~/.cache/acte-corpus)
as <name>-<sha>, reused when present, and its HEAD checked against the pinned sha.

The build root is the folder the package is imported from: the repo root for flat layouts
(rich, httpx), src/ for src-layouts (click, flask). So module names match the package's own
absolute imports. The package folder alone makes `from rich.console import Console`
third-party; the repo root of a src-layout adds test fixtures such as flask's
tests/test_apps/.../flask.py, which make every `from flask import X` ambiguous.

The baselines are a first measured run, not ground truth: node, call-site and resolved
counts may move 10% either way, the jedi coverage rate may not fall below its floor, and the
build may take at most 3 times the measured seconds.
"""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from rdflib.namespace import RDF

from acte.cli import build
from acte.health import run_checks
from acte.rdf_builder import CODE
from acte.sparql_engine import SPARQLEngine

pytestmark = pytest.mark.skipif(os.environ.get("ACTE_REAL_REPOS") != "1", reason="set ACTE_REAL_REPOS=1 to run")

BASELINE = json.loads((Path(__file__).parent / "data" / "real_repos.json").read_text())
CORPUS = Path(os.environ.get("ACTE_CORPUS_DIR") or Path.home() / ".cache" / "acte-corpus")


def _checkout(name: str) -> Path:
    """The pinned clone of *name*, cloned on first use."""
    repo = BASELINE["repos"][name]
    path = CORPUS / f"{name}-{repo['sha']}"
    if not path.exists():
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", repo["tag"], "-c", "advice.detachedHead=false",
                        repo["url"], str(path)], check=True)
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    assert head.stdout.strip() == repo["sha"], f"{path} is not at the pinned commit"
    return path / repo["root"]


def _measure(root: Path, use_jedi: bool) -> tuple[dict, dict]:
    """The counts recorded in the baseline for a build of *root*, and the health check results."""
    start = time.perf_counter()
    graph = build(root, use_jedi)
    seconds = time.perf_counter() - start
    cov = SPARQLEngine(graph=graph).coverage()
    files, functions, classes = (len(set(graph.subjects(RDF.type, t))) for t in (CODE.File, CODE.Function, CODE.Class))
    return {
        "files": files, "functions": functions, "nodes": files + functions + classes,
        "call_sites": cov["total"], "resolved": cov["status"].get("resolved", 0),
        "external": cov["status"].get("external", 0), "ambiguous": cov["status"].get("ambiguous", 0),
        "unresolved": {r: n for r, n in cov["reason"].items() if r in ("local", "no-match", "unknown-receiver")},
        "rate": cov["rate"], "seconds": round(seconds, 1),
    }, run_checks(graph)


@pytest.mark.parametrize("name", sorted(BASELINE["repos"]))
def test_real_repo(name):
    root = _checkout(name)
    got, checks = _measure(root, use_jedi=True)
    no_jedi, _ = _measure(root, use_jedi=False)  # reported, not asserted
    print(f"\n{name}: {json.dumps(got)} rate without jedi {no_jedi['rate']:.4f} in {no_jedi['seconds']} s")

    want = BASELINE["repos"][name]
    assert {k: rows[:5] for k, rows in checks.items() if rows} == {}
    assert got["rate"] >= want["rate_floor"]
    for key in ("nodes", "call_sites", "resolved"):
        assert abs(got[key] - want[key]) <= 0.1 * want[key], f"{key}: {got[key]} vs baseline {want[key]}"
    assert got["seconds"] <= 3 * want["seconds"]


def test_build_is_deterministic():
    root = _checkout("flask")
    assert build(root).serialize(format="turtle") == build(root).serialize(format="turtle")
