from pathlib import Path

import pytest
from pyoxigraph import BlankNode

from acte.cli import _counts
from acte.health import CHECKS, run_checks, scan
from acte.rdf_builder import (
    CODE,
    RDF,
    KnowledgeGraphBuilder,
    add,
    build_graph,
    has,
    lit,
    remove,
    subjects,
)

SRC = Path(__file__).resolve().parents[1] / "src"
SNIPPETS = Path(__file__).parent / "data" / "pycg" / "snippets"

PROJECT = {
    "db.py": "class Db:\n    def save(self):\n        pass\n",
    "app.py": (
        "import os\n"
        "from db import Db\n"
        "\n"
        "def run():\n"
        "    Db().save()\n"
        "    os.getcwd()\n"
    ),
}


def _failing(graph):
    report = run_checks(graph)
    assert list(report) == list(CHECKS)
    return {name for name, rows in report.items() if rows}


def test_own_source_is_healthy():
    assert _failing(build_graph(sorted(SRC.rglob("*.py")), SRC, use_jedi=False)) == set()


def test_pycg_snippets_are_healthy():
    for key_file in sorted(SNIPPETS.glob("*/*/callgraph.json")):
        case = key_file.parent
        assert _failing(build_graph(sorted(case.rglob("*.py")), case, use_jedi=False)) == set(), case


def _site(g, status):
    return next(iter(subjects(g, CODE.status, lit(status))))


NOWHERE = CODE["nowhere"]
MUTATIONS = {
    "dangling-call-arrows": lambda g, run, app: add(g, run, CODE.calls, NOWHERE),
    "dangling-import-arrows": lambda g, run, app: add(g, app, CODE.importsFrom, NOWHERE),
    "dangling-inherits-arrows": lambda g, run, app: add(g, run, CODE.inherits, NOWHERE),
    "self-call-arrows": lambda g, run, app: add(g, run, CODE.calls, run),
    "callsite-status": lambda g, run, app: remove(g, _site(g, "external"), CODE.status),
    "resolved-without-target": lambda g, run, app: remove(g, _site(g, "resolved"), CODE.resolvedTo),
    "callsite-without-caller": lambda g, run, app: remove(g, _site(g, "external"), CODE.inFunction),
    "undeclared-terms": lambda g, run, app: add(g, run, CODE.undeclared, app),
}


@pytest.mark.parametrize("check", CHECKS)
def test_one_broken_triple_fails_exactly_its_check(tmp_path, check):
    for rel, code in PROJECT.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in PROJECT), tmp_path, use_jedi=False)
    assert _failing(graph) == set()
    uri = KnowledgeGraphBuilder()._make_uri
    app, run = uri(f"{tmp_path}/app.py"), uri(f"{tmp_path}/app.py::run")
    assert has(graph, run, RDF.type, CODE.Function)
    before = len(graph)
    MUTATIONS[check](graph, run, app)
    assert len(graph) != before
    assert _failing(graph) == {check}


def test_blank_node_suspect_runs_the_whole_check(tmp_path):
    """A blank node cannot be listed in VALUES, so a check with one among its suspects runs over the whole graph."""
    for rel, code in PROJECT.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in PROJECT), tmp_path, use_jedi=False)
    add(graph, BlankNode(), RDF.type, CODE.CallSite)
    assert _failing(graph) == {"callsite-status", "callsite-without-caller"}


def test_one_pass_gives_the_counts_coverage_and_checks_of_the_lookups(tmp_path):
    for rel, code in PROJECT.items():
        (tmp_path / rel).write_text(code)
    uri = KnowledgeGraphBuilder()._make_uri
    app, run = uri(f"{tmp_path}/app.py"), uri(f"{tmp_path}/app.py::run")
    breaks = {"healthy": lambda g, run, app: None, **MUTATIONS,
              "blank site": lambda g, run, app: add(g, BlankNode(), RDF.type, CODE.CallSite),
              # reversed: the external site loses its caller before its status
              "every check": lambda g, run, app: [m(g, run, app) for m in reversed(MUTATIONS.values())]}
    for name, spoil in breaks.items():
        graph = build_graph(sorted(tmp_path / rel for rel in PROJECT), tmp_path, use_jedi=False)
        spoil(graph, run, app)
        counts, cov = _counts(graph)
        assert scan(graph) == (counts, cov, run_checks(graph)), name
        assert name != "every check" or all(run_checks(graph).values())
    graph = build_graph(sorted(SRC.rglob("*.py")), SRC, use_jedi=False)
    assert scan(graph) == (*_counts(graph), run_checks(graph))
