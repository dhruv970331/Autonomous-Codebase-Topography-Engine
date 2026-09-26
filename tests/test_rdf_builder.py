import pytest
from rdflib import Literal, URIRef
from rdflib.compare import isomorphic
from rdflib.namespace import RDF, XSD

from acte.parser import CodeParser, EdgeInfo, NodeInfo
from acte.rdf_builder import CODE, KnowledgeGraphBuilder, build_graph
from acte.sparql_engine import SPARQLEngine


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
def test_hash_in_path_is_encoded(tmp_path):
    """A '#' in a real path is percent-encoded, and lookups still match."""
    src = tmp_path / "dir#1" / "mod.py"
    src.parent.mkdir()
    src.write_text("def helper():\n    pass\n\ndef run():\n    helper()\n")
    result = CodeParser().parse_file(src)
    builder = KnowledgeGraphBuilder()
    graph = builder.build(result.nodes, result.edges)

    uris = {str(t) for triple in graph for t in triple if isinstance(t, URIRef)}
    code_uris = {u for u in uris if u.startswith(str(CODE))}
    assert code_uris
    assert all(u.count("#") == 1 for u in code_uris)

    engine = SPARQLEngine(graph=graph)
    assert {f.name for f in engine.functions_in_file(str(src))} == {"helper", "run"}
    assert engine.callees_of("run", str(src)) == ["helper"]

def test_nested_and_top_level_helper_are_distinct_nodes(tmp_path):
    src = tmp_path / "a.py"
    src.write_text("def outer():\n    def helper():\n        pass\n\ndef helper():\n    pass\n")
    result = CodeParser().parse_file(src)
    graph = KnowledgeGraphBuilder().build(result.nodes, result.edges)

    helpers = set(graph.subjects(CODE.name, Literal("helper", datatype=XSD.string)))
    assert len(helpers) == 2
    assert sorted(len(list(graph.objects(h, CODE.lineStart))) for h in helpers) == [1, 1]


CALL_PROJECT = {
    "x/util.py": "def build():\n    pass\n",
    "y/util.py": "def build():\n    pass\n",
    "app.py": (
        "import os\n"
        "from util import build\n"
        "\n"
        "\n"
        "def helper():\n"
        "    pass\n"
        "\n"
        "\n"
        "class Database:\n"
        "    def connect(self):\n"
        "        pass\n"
        "\n"
        "\n"
        "def run(n):\n"
        "    helper()\n"
        "    Database()\n"
        "    build()\n"
        "    os.getcwd()\n"
        "    run(n - 1)\n"
        "    [Database()][0].connect()  # unknown-receiver without jedi, TJ with it\n"
        "\n"
        "\n"
        "helper()\n"
    ),
}


def _call_graph(root, use_jedi=False):
    for rel, code in CALL_PROJECT.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)
    return build_graph(sorted(root / rel for rel in CALL_PROJECT), root, use_jedi)


def test_call_sites_and_arrows(tmp_path):
    graph = _call_graph(tmp_path)
    uri = KnowledgeGraphBuilder()._make_uri
    app = f"{tmp_path}/app.py"
    run, helper, db = uri(f"{app}::run"), uri(f"{app}::helper"), uri(f"{app}::Database")
    s = lambda v: Literal(v, datatype=XSD.string)
    i = lambda v: Literal(v, datatype=XSD.integer)

    def site(caller, line, col, name, status, extra):
        return {
            (RDF.type, CODE.CallSite), (CODE.inFunction, uri(caller)), (CODE.line, i(line)),
            (CODE.column, i(col)), (CODE.targetName, s(name)), (CODE.status, s(status)), *extra,
        }

    expected = {
        f"{app}::run@L15c4": site(f"{app}::run", 15, 4, "helper", "resolved",
                                  {(CODE.tier, s("T2")), (CODE.resolvedTo, helper)}),
        f"{app}::run@L16c4": site(f"{app}::run", 16, 4, "Database", "resolved",
                                  {(CODE.tier, s("T2")), (CODE.resolvedTo, db)}),
        f"{app}::run@L17c4": site(f"{app}::run", 17, 4, "build", "ambiguous", {
            (CODE.candidateCount, i(2)),
            (CODE.candidate, uri(f"{tmp_path}/x/util.py::build")),
            (CODE.candidate, uri(f"{tmp_path}/y/util.py::build")),
        }),
        f"{app}::run@L18c7": site(f"{app}::run", 18, 7, "getcwd", "external",
                                  {(CODE.receiver, s("os")), (CODE.reason, s("stdlib"))}),
        f"{app}::run@L19c4": site(f"{app}::run", 19, 4, "run", "resolved", {
            (CODE.tier, s("T2")), (CODE.resolvedTo, run),
            (CODE.recursive, Literal(True, datatype=XSD.boolean)),
        }),
        f"{app}::run@L20c5": site(f"{app}::run", 20, 5, "Database", "resolved",
                                  {(CODE.tier, s("T2")), (CODE.resolvedTo, db)}),
        f"{app}::run@L20c20": site(f"{app}::run", 20, 20, "connect", "unresolved",
                                   {(CODE.receiver, s("[Database()][0]")), (CODE.reason, s("unknown-receiver"))}),
        f"{app}@L23c0": site(app, 23, 0, "helper", "resolved",
                             {(CODE.tier, s("T2")), (CODE.resolvedTo, helper)}),
    }
    sites = set(graph.subjects(RDF.type, CODE.CallSite))
    assert sites == {uri(k) for k in expected}
    for k, props in expected.items():
        assert set(graph.predicate_objects(uri(k))) == props, k
    assert all(str(u).count("#") == 1 for u in sites)
    assert f"{CODE}{tmp_path}/app.py::run@L19c4" in {str(u) for u in sites}  # "@" stays readable

    assert (run, CODE.calls, helper) in graph
    assert (uri(app), CODE.calls, helper) in graph
    assert (run, CODE.instantiates, db) in graph
    assert (run, CODE.calls, db) not in graph
    assert (run, CODE.calls, run) not in graph  # R11
    candidates = {uri(f"{tmp_path}/x/util.py::build"), uri(f"{tmp_path}/y/util.py::build")}
    assert not candidates & set(graph.objects(run, CODE.calls))  # ambiguous: no arrow
    assert (run, CODE.calls, uri(f"{app}::Database.connect")) not in graph


def test_jedi_resolved_call_site(tmp_path):
    pytest.importorskip("jedi")
    graph = _call_graph(tmp_path, use_jedi=True)
    uri = KnowledgeGraphBuilder()._make_uri
    run, connect = uri(f"{tmp_path}/app.py::run"), uri(f"{tmp_path}/app.py::Database.connect")
    site = uri(f"{tmp_path}/app.py::run@L20c20")
    assert str(graph.value(site, CODE.tier)) == "TJ"
    assert graph.value(site, CODE.resolvedTo) == connect
    assert (run, CODE.calls, connect) in graph


@pytest.mark.parametrize("use_jedi", [False, True])
def test_build_is_deterministic(tmp_path, use_jedi):
    assert isomorphic(_call_graph(tmp_path / "a", use_jedi), _call_graph(tmp_path / "a", use_jedi))
