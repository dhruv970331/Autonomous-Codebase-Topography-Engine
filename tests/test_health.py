from pathlib import Path

import pytest
from rdflib import Literal, URIRef
from rdflib.namespace import RDF, XSD

from acte.health import CHECKS, run_checks
from acte.rdf_builder import CODE, KnowledgeGraphBuilder, build_graph

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
    return next(iter(g.subjects(CODE.status, Literal(status, datatype=XSD.string))))


NOWHERE = URIRef(CODE["nowhere"])
MUTATIONS = {
    "dangling-call-arrows": lambda g, run, app: g.add((run, CODE.calls, NOWHERE)),
    "dangling-import-arrows": lambda g, run, app: g.add((app, CODE.importsFrom, NOWHERE)),
    "dangling-inherits-arrows": lambda g, run, app: g.add((run, CODE.inherits, NOWHERE)),
    "self-call-arrows": lambda g, run, app: g.add((run, CODE.calls, run)),
    "callsite-status": lambda g, run, app: g.remove((_site(g, "external"), CODE.status, None)),
    "resolved-without-target": lambda g, run, app: g.remove((_site(g, "resolved"), CODE.resolvedTo, None)),
    "callsite-without-caller": lambda g, run, app: g.remove((_site(g, "external"), CODE.inFunction, None)),
    "undeclared-terms": lambda g, run, app: g.add((run, CODE.undeclared, app)),
}


@pytest.mark.parametrize("check", CHECKS)
def test_one_broken_triple_fails_exactly_its_check(tmp_path, check):
    for rel, code in PROJECT.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in PROJECT), tmp_path, use_jedi=False)
    assert _failing(graph) == set()
    uri = KnowledgeGraphBuilder()._make_uri
    app, run = uri(f"{tmp_path}/app.py"), uri(f"{tmp_path}/app.py::run")
    assert (run, RDF.type, CODE.Function) in graph
    before = len(graph)
    MUTATIONS[check](graph, run, app)
    assert len(graph) != before
    assert _failing(graph) == {check}
