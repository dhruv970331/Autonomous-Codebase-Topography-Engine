from pathlib import Path

import pytest

from acte.health import run_checks
from acte.parser import CodeParser, EdgeInfo, NodeInfo
from acte.rdf_builder import (
    CODE,
    RDF,
    KnowledgeGraphBuilder,
    build_graph,
    has,
    iri,
    lit,
    nt,
    objects,
    subjects,
    text,
    triples,
    value,
)
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
    ]
    return nodes, edges

def test_uri_alignment(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    # Extract the exact URI generated for the function node
    func_node = next(n for n in nodes if n.name == "verify")
    func_uri = builder._make_uri(builder._node_id(func_node))

    # Verify the CONTAINS edge uses the identical URI for its target
    class_uri = builder._make_uri("main.py::AuthManager")
    assert has(graph, class_uri, CODE.contains, func_uri), "Fatal: Node URI and Edge target URI do not match."


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

    builder.reset()
    assert len(builder.graph) == 0, "Graph failed to reset."


def test_line_end_mapping(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    file_uri = builder._make_uri("main.py")
    line_end = value(graph, file_uri, CODE.lineEnd)

    assert text(line_end) == "10", "lineEnd literal missing or incorrect."


def test_rdf_types(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    class_uri = builder._make_uri("main.py::AuthManager")
    function_uri = builder._make_uri("main.py::AuthManager.verify")
    file_uri = builder._make_uri("main.py")

    # Verify semantic type mappings are emitted correctly
    assert has(graph, class_uri, RDF.type, CODE.Class)
    assert has(graph, function_uri, RDF.type, CODE.Function)
    assert has(graph, file_uri, RDF.type, CODE.File)


def test_edge_predicates(builder, sample_data):
    nodes, edges = sample_data
    graph = builder.build(nodes, edges)

    # Verify CONTAINS edges map to code:contains
    file_uri = builder._make_uri("main.py")
    class_uri = builder._make_uri("main.py::AuthManager")

    assert has(graph, file_uri, CODE.contains, class_uri)
def test_hash_in_path_is_encoded(tmp_path):
    """A '#' in a real path is percent-encoded, and lookups still match."""
    src = tmp_path / "dir#1" / "mod.py"
    src.parent.mkdir()
    src.write_text("def helper():\n    pass\n\ndef run():\n    helper()\n")
    graph = build_graph([src], tmp_path)

    uris = {text(t) for triple in triples(graph) for t in triple if nt(t).startswith("<")}
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

    helpers = set(subjects(graph, CODE.name, lit("helper")))
    assert len(helpers) == 2
    assert sorted(len(list(objects(graph, h, CODE.lineStart))) for h in helpers) == [1, 1]


CALL_PROJECT = {
    "x/util.py": "def build():\n    pass\n",
    "y/util.py": "def build():\n    pass\n",
    "app.py": (
        "import os\n"
        "from x.util import *; from y.util import *\n"  # one line, so the line numbers below stay
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
    s = i = lit

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
            (CODE.recursive, lit(True)),
        }),
        f"{app}::run@L20c5": site(f"{app}::run", 20, 5, "Database", "resolved",
                                  {(CODE.tier, s("T2")), (CODE.resolvedTo, db)}),
        f"{app}::run@L20c20": site(f"{app}::run", 20, 20, "connect", "unresolved",
                                   {(CODE.receiver, s("[Database()][0]")), (CODE.reason, s("unknown-receiver"))}),
        f"{app}@L23c0": site(app, 23, 0, "helper", "resolved",
                             {(CODE.tier, s("T2")), (CODE.resolvedTo, helper)}),
    }
    sites = set(subjects(graph, RDF.type, CODE.CallSite))
    assert sites == {uri(k) for k in expected}
    for k, props in expected.items():
        assert {(p, o) for _, p, o in triples(graph, uri(k))} == props, k
    assert all(text(u).count("#") == 1 for u in sites)
    assert f"{CODE}{tmp_path}/app.py::run@L19c4" in {text(u) for u in sites}  # "@" stays readable

    assert has(graph, run, CODE.calls, helper)
    assert has(graph, uri(app), CODE.calls, helper)
    assert has(graph, run, CODE.instantiates, db)
    assert not has(graph, run, CODE.calls, db)
    assert not has(graph, run, CODE.calls, run)  # R11
    candidates = {uri(f"{tmp_path}/x/util.py::build"), uri(f"{tmp_path}/y/util.py::build")}
    assert not candidates & set(objects(graph, run, CODE.calls))  # ambiguous: no arrow
    assert not has(graph, run, CODE.calls, uri(f"{app}::Database.connect"))


def test_jedi_resolved_call_site(tmp_path):
    pytest.importorskip("jedi")
    graph = _call_graph(tmp_path, use_jedi=True)
    uri = KnowledgeGraphBuilder()._make_uri
    run, connect = uri(f"{tmp_path}/app.py::run"), uri(f"{tmp_path}/app.py::Database.connect")
    site = uri(f"{tmp_path}/app.py::run@L20c20")
    assert text(value(graph, site, CODE.tier)) == "TJ"
    assert value(graph, site, CODE.resolvedTo) == connect
    assert has(graph, run, CODE.calls, connect)


def _lines(graph) -> list[str]:
    """The map as sorted N-Triples lines: two builds of the same code give the same lines."""
    return sorted(" ".join(nt(t) for t in triple) for triple in triples(graph))


@pytest.mark.parametrize("use_jedi", [False, True])
def test_build_is_deterministic(tmp_path, use_jedi):
    assert _lines(_call_graph(tmp_path / "a", use_jedi)) == _lines(_call_graph(tmp_path / "a", use_jedi))


def test_imports_point_at_file_nodes_or_name_external_modules(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/b.py": "def f():\n    pass\n",
        "pkg/a.py": "from . import b\nfrom .b import f\nimport pkg.b\nimport os.path\nfrom util import build\nfrom .gone import g\n",
        "x/util.py": "def build():\n    pass\n",
        "y/util.py": "def build():\n    pass\n",
    }
    for rel, code in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in files), tmp_path, use_jedi=False)
    uri = KnowledgeGraphBuilder()._make_uri
    a = uri(f"{tmp_path}/pkg/a.py")

    imported = set(objects(graph, a, CODE.importsFrom))
    assert imported == {uri(f"{tmp_path}/pkg/__init__.py"), uri(f"{tmp_path}/pkg/b.py")}
    assert all(has(graph, f, RDF.type, CODE.File) for f in imported)
    # `util` is no project module (x/util.py is x.util), so it is external; `.gone` is relative: no arrow, no name
    assert set(objects(graph, a, CODE.importsExternal)) == {lit("os.path"), lit("util")}


def test_import_kinds(tmp_path):
    files = {
        "lib.py": "def f():\n    pass\n",
        "other.py": "",
        "app.py": (
            "import typing\n"
            "from typing import TYPE_CHECKING\n"
            "\n"
            "try:\n"
            "    import lib\n"
            "except ImportError:\n"
            "    lib = None\n"
            "\n"
            "if TYPE_CHECKING:\n"
            "    import json\n"
            "    if lib:\n"
            "        import other\n"
            "elif lib:\n"
            "    import csv\n"
            "else:\n"
            "    import re\n"
            "\n"
            "if typing.TYPE_CHECKING:\n"
            "    from collections import abc\n"
            "\n"
            "\n"
            "def run():\n"
            "    import lib\n"
            "    import os\n"
            "    import os\n"
            "    if TYPE_CHECKING:\n"
            "        import decimal\n"
            "\n"
            "    def inner():\n"
            "        import string\n"
            "\n"
            "\n"
            "class C:\n"
            "    import enum\n"
            "\n"
            "    def m(self):\n"
            "        import fractions\n"
        ),
    }
    for rel, code in files.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in files), tmp_path, use_jedi=False)
    uri = KnowledgeGraphBuilder()._make_uri
    app, lib, other = (uri(f"{tmp_path}/{f}") for f in ("app.py", "lib.py", "other.py"))

    def imported(p):
        return {text(o).rsplit("/", 1)[-1] if nt(o).startswith("<") else text(o) for o in objects(graph, app, p)}

    # elif, else and class bodies run at import time; a TYPE_CHECKING block in a function is type-only
    assert imported(CODE.importsFrom) == {"lib.py"}  # inside a module-level try
    assert imported(CODE.importsExternal) == {"typing", "csv", "re", "enum"}
    assert imported(CODE.importsTypeOnly) == {"other.py"}
    assert imported(CODE.importsTypeOnlyExternal) == {"json", "collections", "decimal"}
    assert imported(CODE.importsInFunction) == {"lib.py"}  # lib.py in two kinds: once in each
    assert imported(CODE.importsInFunctionExternal) == {"os", "string", "fractions"}
    assert {(app, CODE.importsFrom, lib), (app, CODE.importsInFunction, lib), (app, CODE.importsTypeOnly, other)} <= set(graph)
    assert not any(run_checks(graph).values())
    deps = SPARQLEngine(graph=graph).dependencies_of(f"{tmp_path}/app.py")
    assert deps.dependencies == sorted([
        f"{tmp_path}/lib.py", f"{tmp_path}/other.py", "typing", "csv", "re", "enum", "json", "collections", "decimal",
        "os", "string", "fractions"])  # every kind
    # lib.py is imported at module level too, so it has no kinds
    assert deps.kinds == {f"{tmp_path}/other.py": ["type-only"], "json": ["type-only"], "collections": ["type-only"],
                          "decimal": ["type-only"], "os": ["in-function"], "string": ["in-function"], "fractions": ["in-function"]}


def test_main_block_imports(tmp_path):
    files = {
        "lib.py": "def f():\n    pass\n",
        "app.py": (
            'if __name__ == "__main__":\n'
            "    import lib\n"
            "    import argparse\n"
            "\n"
            "    def cli():\n"
            "        import pdb\n"
            "\n"
            "    if TYPE_CHECKING:\n"
            "        import decimal\n"
            "\n"
            "if '__main__' == __name__:\n"
            "    import glob\n"
            "else:\n"
            "    import shutil\n"
            "\n"
            "\n"
            "def run():\n"
            '    if __name__ == "__main__":\n'
            "        import timeit\n"
        ),
    }
    for rel, code in files.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in files), tmp_path, use_jedi=False)
    app = KnowledgeGraphBuilder()._make_uri(f"{tmp_path}/app.py")

    def imported(p):
        return {text(o).rsplit("/", 1)[-1] if nt(o).startswith("<") else text(o) for o in objects(graph, app, p)}

    # both operand orders and quote styles; a function in the block is in it too; else runs as usual;
    # only a module-level guard counts, so one in a function is an in-function import; the innermost block decides
    assert imported(CODE.importsInMainBlock) == {"lib.py"}
    assert imported(CODE.importsInMainBlockExternal) == {"argparse", "pdb", "glob"}
    assert imported(CODE.importsExternal) == {"shutil"}
    assert imported(CODE.importsInFunctionExternal) == {"timeit"}
    assert imported(CODE.importsTypeOnlyExternal) == {"decimal"}
    assert imported(CODE.importsFrom) == set()
    assert SPARQLEngine(graph=graph).dependencies_of(f"{tmp_path}/app.py").kinds[f"{tmp_path}/lib.py"] == ["main-block"]
    assert not any(run_checks(graph).values())


def test_imports_in_own_source_reach_typed_files():
    src = Path(__file__).resolve().parents[1] / "src"
    graph = build_graph(sorted(src.rglob("*.py")), src, use_jedi=False)
    imported = set(objects(graph, None, CODE.importsFrom))
    assert imported
    assert all(has(graph, f, RDF.type, CODE.File) for f in imported)


def test_inherits_points_at_project_classes_only(tmp_path):
    files = {
        "base.py": "class Base:\n    pass\n",
        "app.py": "from base import Base\n\n\nclass A(Base, dict):\n    pass\n\n\nclass B(A, make()):\n    pass\n",
    }
    for rel, code in files.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in files), tmp_path, use_jedi=False)
    uri = KnowledgeGraphBuilder()._make_uri
    a, b, base = (uri(f"{tmp_path}/{i}") for i in ("app.py::A", "app.py::B", "base.py::Base"))
    assert {(s, o) for s, _, o in triples(graph, None, CODE.inherits)} == {(a, base), (b, a)}


def test_decorator_line_only_when_decorated(tmp_path):
    (tmp_path / "a.py").write_text("@deco\ndef f():\n    pass\n\n\ndef g():\n    pass\n")
    g = build_graph([tmp_path / "a.py"], tmp_path, use_jedi=False)
    f, plain = (iri(CODE[f"{tmp_path}/a.py::{n}"]) for n in ("f", "g"))
    assert (value(g, f, CODE.lineStart), value(g, f, CODE.decoratorLine)) == (lit(2), lit(1))
    assert value(g, plain, CODE.decoratorLine) is None

def test_go_and_tsx_give_structure_and_unresolved_calls(tmp_path):
    files = {
        "shapes.go": "package shapes\n\nimport \"fmt\"\n\ntype Point struct{}\n\n"
                     "func (p *Point) String() string {\n\treturn fmt.Sprint(New())\n}\n\nfunc New() *Point {\n\treturn nil\n}\n",
        "app.tsx": "import { x } from './lib';\n\nexport function App() {\n  return <div>{helper()}</div>;\n}\n\n"
                   "function helper() {\n  return 1;\n}\n",
        "main.py": "def New():\n    pass\n\nNew()\n",
    }
    for rel, code in files.items():
        (tmp_path / rel).write_text(code)
    graph = build_graph(sorted(tmp_path / rel for rel in files), tmp_path, use_jedi=False)
    uri = KnowledgeGraphBuilder()._make_uri
    go, tsx, py = (uri(f"{tmp_path}/{f}") for f in ("shapes.go", "app.tsx", "main.py"))

    assert set(objects(graph, go, CODE.contains)) == {uri(f"{tmp_path}/shapes.go::{n}") for n in ("Point", "Point.String", "New")}
    assert set(objects(graph, tsx, CODE.contains)) == {uri(f"{tmp_path}/app.tsx::{n}") for n in ("App", "helper")}
    assert has(graph, uri(f"{tmp_path}/shapes.go::Point"), RDF.type, CODE.Class)
    assert has(graph, uri(f"{tmp_path}/shapes.go::Point.String"), RDF.type, CODE.Function)
    # only Python imports are kept in the graph, as for TypeScript
    assert not {o for f in (go, tsx) for p in (CODE.importsFrom, CODE.importsExternal) for o in objects(graph, f, p)}

    status = {
        (text(value(graph, s, CODE.targetName)), text(value(graph, s, CODE.status)), text(value(graph, s, CODE.reason)))
        for s in subjects(graph, RDF.type, CODE.CallSite) if "main.py" not in text(value(graph, s, CODE.inFunction))
    }
    assert status == {("Sprint", "unresolved", "unknown-receiver"), ("New", "unresolved", "no-match"), ("helper", "unresolved", "no-match")}
    # the Python call resolves to the Python New, never to the Go one
    assert {(s, o) for s, _, o in triples(graph, None, CODE.calls)} == {(py, uri(f"{tmp_path}/main.py::New"))}
    assert all(rows == [] for rows in run_checks(graph).values())
