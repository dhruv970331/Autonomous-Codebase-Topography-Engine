"""
Validation on real code ACTE's authors did not write: pinned releases of the projects in
tests/data/real_repos.json, built with jedi and checked against the baselines there.

Skipped unless ACTE_REAL_REPOS=1, since it needs git and the network on first run and takes
a few minutes. Projects marked "slow" (a jedi build over 5 minutes) run only with
ACTE_REAL_REPOS=slow, which runs every project:

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

Link accuracy uses an answer key from outside ACTE (E1): jedi's get_references on a fixed
sample of functions. The key's callers are the parsed functions (or files, for module-level
code) whose line ranges hold a call of the function, that is a reference followed by "(".
ACTE's callers are the confirmed callers from callers_of(id). Only the build without jedi
is asserted: the jedi build uses jedi too, so it is not graded independently.
The key is kept independent of tier TJ (jedi's goto) but has known misses, where goto at the
call agrees with ACTE: calls through an import alias (`from m import f as g; g()`), and some
calls on a typed local (`column = DownloadColumn(); column.render(task)` in rich's tests).
So precision below 1.0 is not by itself a wrong link: check each ACTE-only link it prints.
"""
import json
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path

import jedi
import pytest

from acte.cli import build
from acte.health import run_checks
from acte.rdf_builder import CODE, RDF, objects, save, subjects, text, value
from acte.sparql_engine import SPARQLEngine

LEVEL = os.environ.get("ACTE_REAL_REPOS")
pytestmark = pytest.mark.skipif(LEVEL not in ("1", "slow"), reason="set ACTE_REAL_REPOS=1 (or =slow) to run")

BASELINE = json.loads((Path(__file__).parent / "data" / "real_repos.json").read_text())
REPOS = sorted(n for n, r in BASELINE["repos"].items() if LEVEL == "slow" or not r.get("slow"))
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
    files, functions, classes = (len(set(subjects(graph, RDF.type, t))) for t in (CODE.File, CODE.Function, CODE.Class))
    return {
        "files": files, "functions": functions, "nodes": files + functions + classes,
        "call_sites": cov["total"], "resolved": cov["status"].get("resolved", 0),
        "external": cov["status"].get("external", 0), "ambiguous": cov["status"].get("ambiguous", 0),
        "unresolved": {r: n for r, n in cov["reason"].items() if r in ("local", "no-match", "unknown-receiver", "unknown-base")},
        "rate": cov["rate"], "seconds": round(seconds, 1),
    }, run_checks(graph)


@pytest.mark.parametrize("name", REPOS)
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


SAMPLE = 40


def _id(node) -> str:
    return urllib.parse.unquote(text(node)[len(CODE):])  # as SPARQLEngine._id: ids, not percent-encoded URIs


def _oracle(project: jedi.Project, root: Path, graph, ranges: dict, node) -> set[str] | None:
    """The callers of Function *node* according to jedi, as ACTE ids; None if jedi does not find the definition itself."""
    path = text(value(graph, node, CODE.filePath))
    line = max(int(x) for x in objects(graph, node, CODE.lineStart))  # the last definition, the one the resolver keeps
    name = text(value(graph, node, CODE.name))
    lines = (root / path).read_text(encoding="utf-8").splitlines()
    col = re.search(rf"\bdef\s+({re.escape(name)})\b", lines[line - 1]).start(1)
    refs = jedi.Script(path=root / path, project=project).get_references(line, col)
    # jedi gives a @classmethod's own name the path of builtins.pyi, so the position alone is checked
    if not any(r.is_definition() and (r.line, r.column) == (line, col) for r in refs):
        return None  # a spelling or position mismatch: the key would compare nothing (E4)
    callers = set()
    for r in refs:
        rel = r.module_path and r.module_path.is_relative_to(root.resolve()) and r.module_path.relative_to(root.resolve()).as_posix()
        if rel not in ranges:  # typeshed stubs, and files ACTE did not parse, are not in ACTE's id space
            continue
        source = (root / rel).read_text(encoding="utf-8").splitlines()[r.line - 1]
        if r.is_definition() or source[r.column + len(name):][:1] != "(":
            continue
        inner = [(start, f) for start, end, f in ranges[rel] if start <= r.line <= end]
        caller = max(inner)[1] if inner else rel
        if caller != _id(node):  # ACTE draws no arrow for recursion
            callers.add(caller)
    return callers


def _ranges(graph) -> dict[str, list[tuple[int, int, str]]]:
    """Parsed file -> (lineStart, lineEnd, id) of every Function in it. An id defined several times (overloads,
    if/else) has one lineStart and lineEnd per definition; those never overlap, so sorted they pair up."""
    out = {text(value(graph, f, CODE.filePath)): [] for f in subjects(graph, RDF.type, CODE.File)}
    for f in subjects(graph, RDF.type, CODE.Function):
        starts, ends = (sorted(int(x) for x in objects(graph, f, p)) for p in (CODE.lineStart, CODE.lineEnd))
        out[text(value(graph, f, CODE.filePath))].extend((s, e, _id(f)) for s, e in zip(starts, ends, strict=True))
    return out


def _link_accuracy(root: Path) -> dict:
    """Precision and recall of ACTE's confirmed callers against jedi's, over SAMPLE functions, for builds without and with jedi."""
    graphs = {jedi_: build(root, jedi_) for jedi_ in (False, True)}
    engines = {k: SPARQLEngine(graph=g) for k, g in graphs.items()}
    graph, project = graphs[False], jedi.Project(root.resolve())
    ranges = _ranges(graph)
    nodes = sorted((f for f in subjects(graph, RDF.type, CODE.Function)  # jedi reads Python only: no .js in fastapi's docs
                    if text(value(graph, f, CODE.language)) == "python" and not re.fullmatch(r"__\w+__", text(value(graph, f, CODE.name)))),
                   key=_id)
    k = max(1, len(nodes) // SAMPLE)
    rows = []
    for node in (nodes[i] for start in range(k) for i in range(start, len(nodes), k)):  # every k-th, then the next offset
        fid = _id(node)
        oracle = _oracle(project, root, graph, ranges, node)
        assert oracle is not None, f"jedi does not find {fid} at its own position"
        acte = {j: {c.id for a in engines[j].callers_of(fid) for c in a.callers} for j in engines}
        if oracle or acte[False] or acte[True]:  # nothing on either side measures nothing
            rows.append((fid, oracle, acte))
        if len(rows) == SAMPLE:
            break
    out = {"sample": len(rows), "disagreements": {}}
    for j, key in ((False, ""), (True, "_jedi")):
        hit = sum(len(a[j] & o) for _, o, a in rows)
        out["precision" + key] = hit / sum(len(a[j]) for _, _, a in rows)
        out["recall" + key] = hit / sum(len(o) for _, o, _ in rows)
        out["disagreements"][f"acte_only{key}"] = sorted(f"{f} <- {c}" for f, o, a in rows for c in a[j] - o)
        out["disagreements"][f"jedi_only{key}"] = sorted(f"{f} <- {c}" for f, o, a in rows for c in o - a[j])
    return out


@pytest.mark.parametrize("name", REPOS)
def test_link_accuracy(name):
    got = _link_accuracy(_checkout(name))
    print(f"\n{name}: " + json.dumps({k: v for k, v in got.items() if k != "disagreements"}))
    for kind, links in got["disagreements"].items():
        print(f"  {kind}: {len(links)}", *(f"\n    {x}" for x in links))
    want = BASELINE["repos"][name]
    assert got["sample"] == want["sample"]
    assert got["precision"] >= want["precision_floor"], got["disagreements"]["acte_only"]


def test_build_is_deterministic(tmp_path):
    root = _checkout("flask")
    for name in ("a.ttl", "b.ttl"):
        save(build(root), tmp_path / name)
    assert (tmp_path / "a.ttl").read_bytes() == (tmp_path / "b.ttl").read_bytes()
