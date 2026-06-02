import pytest
from acte.parser import NodeInfo, EdgeInfo
from acte.rdf_builder import KnowledgeGraphBuilder
from acte.sparql_engine import SPARQLEngine

@pytest.fixture
def mock_engine():
    """Builds a deterministic 3-hop graph for SPARQL validation."""
    nodes = [
        NodeInfo("File", "app.py", "app.py", 1, 50, "python"),
        NodeInfo("File", "db.py", "db.py", 1, 20, "python"),
        NodeInfo("Class", "AuthManager", "app.py", 5, 20, "python"),
        NodeInfo("Function", "login", "app.py", 6, 10, "python", parent_name="AuthManager"),
        NodeInfo("Function", "verify_token", "app.py", 12, 18, "python", parent_name="AuthManager"),
        NodeInfo("Function", "db_connect", "db.py", 5, 10, "python"),
        NodeInfo("Function", "orphan_func", "app.py", 40, 45, "python")
    ]
    edges = [
        # Test graph uses qualified CALL targets for easier blast-radius testing.
        # The real parser currently emits short-name call targets.
        # login calls verify_token (Fully qualified to bridge the graph transitively)
        EdgeInfo("CALLS", "app.py::AuthManager.login", "app.py::AuthManager.verify_token", "app.py", 8),
        # verify_token calls db_connect (Short name, representing an external call)
        EdgeInfo("CALLS", "app.py::AuthManager.verify_token", "db_connect", "app.py", 15),
        # app.py imports db
        EdgeInfo("IMPORTS_FROM", "app.py", "db", "app.py", 2)
    ]
    
    builder = KnowledgeGraphBuilder()
    builder.build(nodes, edges)
    return SPARQLEngine(graph=builder.graph)

def test_functions_in_file(mock_engine):
    results = mock_engine.functions_in_file("db.py")
    assert len(results) == 1
    assert results[0].name == "db_connect"

def test_classes_in_file(mock_engine):
    results = mock_engine.classes_in_file("app.py")
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

def test_callees_of(mock_engine):
    """Test outbound call extraction using the qualified parent scope."""
    results = mock_engine.callees_of("AuthManager.login", "app.py")
    assert len(results) == 1
    assert results[0] == "app.py::AuthManager.verify_token"

def test_dependencies_of(mock_engine):
    results = mock_engine.dependencies_of("app.py")
    assert len(results) == 1
    assert results[0] == "db"

def test_orphan_functions(mock_engine):
    results = mock_engine.orphan_functions()
    names = {r.name for r in results}
    assert "orphan_func" in names
    assert "login" in names

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