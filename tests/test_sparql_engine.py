from pathlib import Path

import pytest
from rdflib.namespace import RDF

from acte.parser import CodeParser
from acte.rdf_builder import CODE, build_graph
from acte.resolver import resolve
from acte.sparql_engine import SPARQLEngine

SRC = Path(__file__).resolve().parents[1] / "src"


def _engine(root: Path, files: dict[str, str]) -> SPARQLEngine:
    for rel, code in files.items():
        (root / rel).write_text(code)
    return SPARQLEngine(graph=build_graph(sorted(root / rel for rel in files), root, use_jedi=False))


@pytest.fixture
def mock_engine(tmp_path):
    """A 3-hop project: login -> verify_token -> db_connect, built by the real pipeline."""
    return _engine(tmp_path, {
        "app.py": (
            "from db import db_connect\n"
            "\n"
            "\n"
            "class AuthManager:\n"
            "    def login(self):\n"
            "        self.verify_token()\n"
            "\n"
            "    def verify_token(self):\n"
            "        db_connect()\n"
            "\n"
            "\n"
            "def orphan_func():\n"
            "    pass\n"
        ),
        "db.py": "def db_connect():\n    pass\n",
    })

def test_functions_in_file(mock_engine, tmp_path):
    results = mock_engine.functions_in_file(f"{tmp_path}/db.py")
    assert len(results) == 1
    assert results[0].name == "db_connect"

def test_classes_in_file(mock_engine, tmp_path):
    results = mock_engine.classes_in_file(f"{tmp_path}/app.py")
    assert len(results) == 1
    assert results[0].name == "AuthManager"

def test_callers_of_direct(mock_engine):
    """Test single-hop caller resolution."""
    results = mock_engine.callers_of("db_connect")
    assert len(results) == 1
    assert results[0].name == "verify_token"

def test_blast_radius_transitive(mock_engine):
    """Test multi-hop transitive caller resolution (The core feature)."""
    results = mock_engine.blast_radius("db_connect")
    assert len(results) == 2
    names = {r.name for r in results}
    assert "verify_token" in names
    assert "login" in names

def test_callees_of(mock_engine, tmp_path):
    """Test outbound call extraction using the qualified parent scope."""
    results = mock_engine.callees_of("AuthManager.login", f"{tmp_path}/app.py")
    assert results == ["verify_token"]

def test_dependencies_of(mock_engine, tmp_path):
    results = mock_engine.dependencies_of(f"{tmp_path}/app.py")
    assert len(results) == 1
    assert results[0] == "db"

def test_orphan_functions(mock_engine):
    results = mock_engine.orphan_functions()
    names = {r.name for r in results}
    assert "orphan_func" in names
    assert "login" in names
    assert not names & {"verify_token", "db_connect"}

def test_raw_sparql_execution(mock_engine):
    query = "SELECT ?name WHERE { ?f a code:Function ; code:name ?name . FILTER(?name = 'db_connect') }"
    results = mock_engine.execute_raw_sparql(query)
    assert len(results) == 1
    assert results[0]["name"] == "db_connect"
    
def test_raw_sparql_error_handling(mock_engine):
    results = mock_engine.execute_raw_sparql("SELECT * WHERE { MALFORMED SYNTAX")
    assert len(results) == 1
    assert "error" in results[0]

def test_missing_function_returns_empty(mock_engine):
    results = mock_engine.callers_of("does_not_exist")
    assert results == []


def test_blast_radius_follows_a_chain_across_files(tmp_path):
    """spec §10.1: entry -> level1 -> level2 -> level3, over two files."""
    engine = _engine(tmp_path, {
        "a.py": "from b import level2\n\n\ndef entry():\n    level1()\n\n\ndef level1():\n    level2()\n",
        "b.py": "def level2():\n    level3()\n\n\ndef level3():\n    pass\n",
    })
    assert {(r.name, r.file_path) for r in engine.blast_radius("level3")} == {
        ("level2", f"{tmp_path}/b.py"), ("level1", f"{tmp_path}/a.py"), ("entry", f"{tmp_path}/a.py"),
    }


def test_arrows_reach_typed_nodes_and_every_call_site_has_one_status():
    """spec §10.2 and §10.17, over ACTE's own src/."""
    graph = build_graph(sorted(SRC.rglob("*.py")), SRC)
    for arrow, kind in ((CODE.calls, CODE.Function), (CODE.instantiates, CODE.Class)):
        targets = set(graph.objects(None, arrow))
        assert targets
        assert all((t, RDF.type, kind) in graph for t in targets), arrow
    sites = set(graph.subjects(RDF.type, CODE.CallSite))
    assert sites
    for site in sites:
        statuses = [str(s) for s in graph.objects(site, CODE.status)]
        assert len(statuses) == 1 and statuses[0] in {"resolved", "ambiguous", "unresolved", "external"}, site


def _rate(use_jedi: bool) -> float:
    parser = CodeParser()
    return resolve([parser.parse_file(p) for p in sorted(SRC.rglob("*.py"))], SRC, use_jedi).report()["rate"]


def test_resolution_rate_with_jedi():
    """spec §10.3, over ACTE's own src/."""
    pytest.importorskip("jedi")
    assert _rate(use_jedi=True) >= 0.80


def test_resolution_rate_without_jedi():
    assert _rate(use_jedi=False) >= 0.59  # ratchet: measured 0.594 at C4; raise it when the ladder improves, never lower it
