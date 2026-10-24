from pathlib import Path

import networkx as nx
import pytest

import acte
from acte.analysis import ImportCycle, _pagerank, import_cycles, rank
from acte.cli import build
from acte.rdf_builder import CODE
from acte.sparql_engine import SPARQLEngine

HUB = {
    "util.py": "def hub():\n    pass\n\n\ndef leaf():\n    pass\n",
    "a.py": "from util import hub\n\n\ndef f1():\n    hub()\n\n\ndef f2():\n    hub()\n\n\nhub()\n",
    "b.py": "from util import hub, leaf\n\n\ndef g():\n    hub()\n    hub()\n    leaf()\n",
}


@pytest.fixture(autouse=True)
def _no_outer_repo(tmp_path, monkeypatch):
    # a git work tree that happens to enclose tmp_path must not be found
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _graph(root, files):
    """The graph the real pipeline builds from *files* written under *root*."""
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)
    return build(root, use_jedi=False)


def test_two_file_import_cycle(tmp_path):
    graph = _graph(tmp_path, {
        "a.py": "import b\n",
        "b.py": "from a import x\n",
        "c.py": "import a\n",  # imports into the cycle, is not part of it
        "d.py": "import d\n",  # a file importing itself is no cycle of 2 or more
    })
    assert import_cycles(graph) == [ImportCycle(["a.py", "b.py"])]


def test_largest_cycle_first(tmp_path):
    graph = _graph(tmp_path, {
        "p.py": "import q\n", "q.py": "import p\n",
        "x.py": "import y\n", "y.py": "import z\n", "z.py": "import x\n",
    })
    assert import_cycles(graph) == [ImportCycle(["x.py", "y.py", "z.py"]), ImportCycle(["p.py", "q.py"])]


def test_no_cycles(tmp_path):
    assert import_cycles(_graph(tmp_path, {"a.py": "import b\n", "b.py": "import os\n"})) == []


def test_hub_function_ranks_first(tmp_path):
    ranking = rank(_graph(tmp_path, HUB))
    assert ranking.converged and ranking.total == 5
    assert [(r.id, r.file_path, r.line, r.callers) for r in ranking.top[:2]] == [
        ("util.py::hub", "util.py", 1, 4),  # f1, f2, g (calls it twice, one caller) and a.py's module-level call
        ("util.py::leaf", "util.py", 5, 1),
    ]
    assert [r.score for r in ranking.top] == sorted((r.score for r in ranking.top), reverse=True)
    assert len(rank(build(tmp_path, use_jedi=False), top=2).top) == 2


def test_pagerank_matches_networkx(tmp_path):
    try:
        from networkx.algorithms.link_analysis.pagerank_alg import _pagerank_python
    except ImportError:
        pytest.skip("networkx no longer has the private _pagerank_python to compare our PageRank against")
    ids = SPARQLEngine()._id
    # ACTE's own src is the larger graph; HUB has a File as a caller, which ACTE's src has not
    for graph, defs in ((build(Path(acte.__file__).parent.parent, use_jedi=False), 100), (_graph(tmp_path, HUB), 4)):
        arrows = nx.DiGraph()
        arrows.add_nodes_from(ids(d) for kind in (CODE.Function, CODE.Class) for d in graph.subjects(None, kind))
        arrows.add_edges_from((ids(s), ids(t)) for p in (CODE.calls, CODE.instantiates) for s, t in graph.subject_objects(p))
        expected = _pagerank_python(arrows)
        ranking = rank(graph, top=10**6)
        assert ranking.converged and len(ranking.top) == ranking.total > defs
        for r in ranking.top:
            assert abs(r.score - expected[r.id]) < 1e-9, r.id
    assert ("a.py", "util.py::hub") in arrows.edges


def test_pagerank_not_converged_returns_last_iterate():
    scores, converged = _pagerank(["a", "b"], [("a", "b")], max_iter=1)
    assert not converged
    assert scores == pytest.approx({"a": 0.2875, "b": 0.7125})  # (0.85 * 0.5 + 0.15) / 2, plus 0.85 * 0.5 into b
