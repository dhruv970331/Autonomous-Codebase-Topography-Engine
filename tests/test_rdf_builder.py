import pytest
from acte.parser import NodeInfo, EdgeInfo
from acte.rdf_builder import KnowledgeGraphBuilder, CODE
from rdflib.namespace import RDF

@pytest.fixture
def builder():
    return KnowledgeGraphBuilder()

@pytest.fixture
def sample_data():
    nodes = [
        NodeInfo(kind="File", name="main.py", file_path="main.py", line_start=1, line_end=10, language="python"),
        NodeInfo(kind="Class", name="AuthManager", file_path="main.py", line_start=2, line_end=8, language="python"),
        NodeInfo(kind="Function", name="verify", file_path="main.py", line_start=3, line_end=5, language="python", parent_name="AuthManager")
    ]
    edges = [
        EdgeInfo(kind="CONTAINS", source="main.py", target="main.py::AuthManager", file_path="main.py", line=2),
        EdgeInfo(kind="CONTAINS", source="main.py::AuthManager", target="main.py::AuthManager.verify", file_path="main.py", line=3),
        EdgeInfo(kind="CALLS", source="main.py::AuthManager.verify", target="db_connect", file_path="main.py", line=4)
    ]
    return nodes, edges

def test_uri_alignment(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    # Extract the exact URI generated for the function node
    func_node = next(n for n in nodes if n.name == "verify")
    func_uri = builder._make_uri(builder._node_id(func_node))

    # Verify the CALLS edge uses the identical URI for its source
    target_uri = builder._make_uri("db_connect")
    assert (
        func_uri,
        CODE.calls,
        target_uri,
    ) in graph, "Fatal: Node URI and Edge source URI do not match."


def test_accumulation_and_reset(builder, sample_data):
    nodes, edges = sample_data

    builder.build(nodes, edges)
    initial_length = len(builder.graph)

    # RDF graphs are sets of triples; rebuilding identical data
    # should not duplicate triples.
    builder.build(nodes, edges)
    assert (
        len(builder.graph) == initial_length
    ), "Graph duplicated identical triples."

    # Reset should clear triples but retain namespace bindings
    builder.reset()
    assert len(builder.graph) == 0, "Graph failed to reset."
    assert (
        "code" in [prefix for prefix, _ in builder.graph.namespaces()]
    ), "Namespace bindings lost on reset."


def test_line_end_mapping(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    file_uri = builder._make_uri("main.py")
    line_end = graph.value(subject=file_uri, predicate=CODE.lineEnd)

    assert str(line_end) == "10", "lineEnd literal missing or incorrect."


def test_rdf_types(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    class_uri = builder._make_uri("main.py::AuthManager")
    function_uri = builder._make_uri("main.py::AuthManager.verify")
    file_uri = builder._make_uri("main.py")

    # Verify semantic type mappings are emitted correctly
    assert (class_uri, RDF.type, CODE.Class) in graph
    assert (function_uri, RDF.type, CODE.Function) in graph
    assert (file_uri, RDF.type, CODE.File) in graph


def test_edge_predicates(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    # Verify CALLS edges map to code:calls
    caller = builder._make_uri("main.py::AuthManager.verify")
    callee = builder._make_uri("db_connect")

    assert (caller, CODE.calls, callee) in graph

    # Verify CONTAINS edges map to code:contains
    file_uri = builder._make_uri("main.py")
    class_uri = builder._make_uri("main.py::AuthManager")

    assert (file_uri, CODE.contains, class_uri) in graph