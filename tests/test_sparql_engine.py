from pathlib import Path

import pytest
from rdflib.namespace import RDF

from acte.parser import CodeParser
from acte.rdf_builder import CODE, KnowledgeGraphBuilder, build_graph
from acte.resolver import CANDIDATE_CEILING, resolve
from acte.sparql_engine import Caller, CallerAnswer, SPARQLEngine

SRC = Path(__file__).resolve().parents[1] / "src"


def _engine(root: Path, files: dict[str, str]) -> SPARQLEngine:
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
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

def test_callers_of_direct(mock_engine, tmp_path):
    """Single-hop callers: exact when every call named db_connect is accounted for."""
    assert mock_engine.callers_of("db_connect") == [CallerAnswer(
        f"{tmp_path}/db.py::db_connect",
        [Caller(f"{tmp_path}/app.py::AuthManager.verify_token", "verify_token", f"{tmp_path}/app.py", 9)],
        [], "exact", {},
    )]

def test_blast_radius_transitive(mock_engine):
    """Test multi-hop transitive caller resolution (The core feature)."""
    [answer] = mock_engine.blast_radius("db_connect")
    assert [(i.name, i.distance) for i in answer.affected] == [("verify_token", 1), ("login", 2)]
    assert (answer.total, answer.truncated, answer.depth, answer.completeness) == (2, False, 3, "exact")

def test_callees_of(mock_engine, tmp_path):
    """Test outbound call extraction using the qualified parent scope."""
    results = mock_engine.callees_of("AuthManager.login", f"{tmp_path}/app.py")
    assert results == ["verify_token"]

def test_dependencies_of(mock_engine, tmp_path):
    results = mock_engine.dependencies_of(f"{tmp_path}/app.py")
    assert (results.dependencies, results.kinds) == ([f"{tmp_path}/db.py"], {})

def test_dependencies_of_lists_project_files_and_external_modules(tmp_path):
    engine = _engine(tmp_path, {"app.py": "import os.path\nimport json\nfrom db import x\n", "db.py": "x = 1\n"})
    assert engine.dependencies_of(f"{tmp_path}/app.py").dependencies == [f"{tmp_path}/db.py", "json", "os.path"]

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


SHOP = (
    "class Database:\n    def __init__(self):\n        pass\n\n    def save(self):\n        pass\n\n\n"
    "class Store:\n    def save(self):\n        pass\n\n\n"
    "def place_order():\n    db = Database()\n    db.save()\n    db.save()\n\n\n"  # lines 14-17
    "def fact(n):\n    return fact(n - 1)\n"
)


def _summary(answers: list[CallerAnswer], root: Path) -> list[tuple]:
    return [(a.target.removeprefix(f"{root}/"), [(c.name, c.line) for c in a.callers], [c.name for c in a.possible],
             a.completeness, a.reasons) for a in answers]


def test_callers_of_possible_callers(tmp_path):
    """spec §10.18 (R19): an ambiguous x.save() is a possible caller of both saves, never a confirmed one."""
    model = "class Database:\n    def save(self):\n        pass\n"
    engine = _engine(tmp_path, {"two/m1.py": model, "two/m2.py": model,
                                "a.py": "from two.m1 import *\nfrom two.m2 import *\n\n\ndef f(x: Database):\n    x.save()\n"})
    assert _summary(engine.callers_of("Database.save"), tmp_path) == [
        ("two/m1.py::Database.save", [], ["f"], "lower-bound", {"ambiguous": 1}),
        ("two/m2.py::Database.save", [], ["f"], "lower-bound", {"ambiguous": 1}),
    ]


def test_callers_of_lower_bound(tmp_path):
    """spec §10.22 (R23): an unknown-receiver x.save() makes the answer lower-bound without naming g."""
    engine = _engine(tmp_path, {"db.py": SHOP + "\n\ndef g(x):\n    x.save()\n"})
    assert _summary(engine.callers_of("Database.save"), tmp_path) == [
        ("db.py::Database.save", [("place_order", 16), ("place_order", 17)], [], "lower-bound", {"unknown-receiver": 1}),
    ]


def test_callers_of_init_and_class_include_instantiators(tmp_path):
    """spec §10.15 (D9): Database() is a caller of Database and of Database.__init__."""
    engine = _engine(tmp_path, {"db.py": SHOP})
    for name in ("Database.__init__", "Database"):
        assert [(c.name, c.line) for a in engine.callers_of(name) for c in a.callers] == [("place_order", 15)], name


def test_callers_of_bare_name_gives_one_answer_per_definition(tmp_path):
    """An undotted name matches top-level functions and methods alike; a dotted one is a qualname."""
    engine = _engine(tmp_path, {"db.py": SHOP, "util.py": "def save():\n    pass\n"})
    assert _summary(engine.callers_of("save"), tmp_path) == [
        ("db.py::Database.save", [("place_order", 16), ("place_order", 17)], [], "exact", {}),
        ("db.py::Store.save", [], [], "exact", {}),
        ("util.py::save", [], [], "exact", {}),
    ]
    assert [a.target for a in engine.callers_of(f"{tmp_path}/db.py::Store.save")] == [f"{tmp_path}/db.py::Store.save"]
    assert [a.target for a in engine.callers_of("Store.save")] == [f"{tmp_path}/db.py::Store.save"]
    assert engine.callers_of("Missing.save") == []


def test_callers_of_init_counts_unsure_instantiations(tmp_path):
    """An ambiguous Database() is a possible caller of each Database.__init__; m.Database() counts as unknown-receiver."""
    model = "class Database:\n    def __init__(self):\n        pass\n"
    engine = _engine(tmp_path, {"two/m1.py": model, "two/m2.py": model,
                                "a.py": "from two.m1 import *\nfrom two.m2 import *\n\n\ndef h(m):\n    Database()\n    m.Database()\n"})
    assert _summary(engine.callers_of("Database.__init__"), tmp_path) == [
        (f"two/m{i}.py::Database.__init__", [], ["h"], "lower-bound", {"ambiguous": 1, "unknown-receiver": 1}) for i in (1, 2)
    ]


def test_callers_of_ceiling_is_lower_bound(tmp_path):
    """A call over the candidate ceiling lists no candidates, so it is counted in reasons instead."""
    files = {f"d{i}/util.py": "def h():\n    pass\n" for i in range(CANDIDATE_CEILING + 1)}
    stars = "".join(f"from d{i}.util import *\n" for i in range(CANDIDATE_CEILING + 1))
    engine = _engine(tmp_path, files | {"a.py": stars + "\n\ndef f():\n    h()\n"})
    answers = engine.callers_of("h")
    assert len(answers) == 6
    assert {(len(a.callers), len(a.possible), a.completeness, str(a.reasons)) for a in answers} == {
        (0, 0, "lower-bound", "{'ceiling': 1}"),
    }


def test_callers_of_excludes_recursion(tmp_path):
    engine = _engine(tmp_path, {"db.py": SHOP})
    assert _summary(engine.callers_of("fact"), tmp_path) == [("db.py::fact", [], [], "exact", {})]


def test_callers_of_locals_qualname_and_cap(tmp_path):
    files = {"n.py": "def outer():\n    def helper():\n        pass\n    helper()\n"}
    files |= {f"r{i}.py": "def run():\n    pass\n" for i in range(7)}
    engine = _engine(tmp_path, files)
    for name in ("outer.<locals>.helper", "outer.helper"):
        assert _summary(engine.callers_of(name), tmp_path) == [("n.py::outer.<locals>.helper", [("outer", 4)], [], "exact", {})]
    assert [a.target for a in engine.callers_of("run")] == [f"{tmp_path}/r{i}.py::run" for i in range(6)]  # R23: at most 6


def test_blast_radius_follows_a_chain_across_files(tmp_path):
    """spec §10.1: entry -> level1 -> level2 -> level3, over two files."""
    engine = _engine(tmp_path, {
        "a.py": "from b import level2\n\n\ndef entry():\n    level1()\n\n\ndef level1():\n    level2()\n",
        "b.py": "def level2():\n    level3()\n\n\ndef level3():\n    pass\n",
    })
    [answer] = engine.blast_radius("level3")
    assert [(i.name, i.file_path, i.distance, i.via.rsplit("::")[-1]) for i in answer.affected] == [
        ("level2", f"{tmp_path}/b.py", 1, "level3"), ("level1", f"{tmp_path}/a.py", 2, "level2"),
        ("entry", f"{tmp_path}/a.py", 3, "level1"),
    ]
    assert (answer.completeness, answer.reasons) == ("exact", {})
    [answer] = engine.blast_radius("level3", depth=1)
    assert [i.name for i in answer.affected] == ["level2"]
    assert (answer.depth, answer.completeness, answer.reasons) == (1, "lower-bound", {"depth-limit": 1})


RANKED = (
    "def t():\n    pass\n\n\ndef a():\n    t()\n\n\ndef b():\n    t()\n\n\n"
    "def x():\n    a()\n\n\ndef y():\n    b()\n\n\ndef z():\n    b()\n"
)


def test_blast_radius_ranks_by_in_degree_and_truncates(tmp_path):
    """D5: within a distance, the node with more confirmed callers comes first; limit cuts after ranking."""
    engine = _engine(tmp_path, {"r.py": RANKED})
    [answer] = engine.blast_radius("t", depth=2)
    assert [(i.name, i.distance, i.via.rsplit("::")[-1]) for i in answer.affected] == [
        ("b", 1, "t"), ("a", 1, "t"), ("x", 2, "a"), ("y", 2, "b"), ("z", 2, "b"),
    ]
    [cut] = engine.blast_radius("t", depth=2, limit=1)
    assert ([i.name for i in cut.affected], cut.total, cut.truncated) == (["b"], 5, True)
    assert engine.blast_radius("t", depth=0)[0].depth == 1
    assert engine.blast_radius("t", depth=99)[0].depth == 10


def test_blast_radius_mutual_recursion_terminates(tmp_path):
    engine = _engine(tmp_path, {"m.py": "def p():\n    q()\n\n\ndef q():\n    p()\n"})
    [answer] = engine.blast_radius("p", depth=10)
    assert [(i.name, i.distance) for i in answer.affected] == [("q", 1)]
    assert answer.completeness == "exact"


def test_blast_radius_of_init_reaches_instantiators_and_files(tmp_path):
    """spec §10.15 (D9); a module-level caller is affected but not expanded."""
    engine = _engine(tmp_path, {"db.py": SHOP + "\n\ndef checkout():\n    place_order()\n\n\nplace_order()\n"})
    [answer] = engine.blast_radius("Database.__init__")
    assert [(i.id.removeprefix(f"{tmp_path}/"), i.distance) for i in answer.affected] == [
        ("db.py::place_order", 1), ("db.py", 2), ("db.py::checkout", 2),
    ]


def test_blast_radius_unknown_receiver_on_the_path_is_lower_bound(tmp_path):
    engine = _engine(tmp_path, {"u.py": RANKED + "\n\ndef other(o):\n    o.a()\n"})
    [answer] = engine.blast_radius("t")
    assert (answer.completeness, answer.reasons) == ("lower-bound", {"unknown-receiver": 1})


def test_blast_radius_possible_callers_two_hops_away(tmp_path):
    """R19: an ambiguous x.save() reaching both saves (which call t) is listed once, and not followed."""
    model = "from lib import t\n\n\nclass Database:\n    def save(self):\n        t()\n"
    engine = _engine(tmp_path, {
        "lib.py": "def t():\n    pass\n", "two/m1.py": model, "two/m2.py": model,
        "a.py": "from two.m1 import *\nfrom two.m2 import *\n\n\ndef f(x: Database):\n    x.save()\n\n\ndef g():\n    f(1)\n",
    })
    [answer] = engine.blast_radius("t")
    assert [i.id.removeprefix(f"{tmp_path}/") for i in answer.affected] == ["two/m1.py::Database.save", "two/m2.py::Database.save"]
    assert [(c.name, c.line) for c in answer.possible] == [("f", 6)]
    assert (answer.completeness, answer.reasons) == ("lower-bound", {"ambiguous": 1})


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


@pytest.mark.parametrize("use_jedi", [False, True])
def test_coverage_matches_report_and_survives_turtle(tmp_path, use_jedi):
    if use_jedi:
        pytest.importorskip("jedi")
    results = [CodeParser().parse_file(p) for p in sorted(SRC.rglob("*.py"))]
    resolution = resolve(results, SRC, use_jedi)  # one run feeds both sides: jedi is slow
    report = resolution.report()
    graph = KnowledgeGraphBuilder().build([n for r in results for n in r.nodes], [e for r in results for e in r.edges], resolution)
    coverage = SPARQLEngine(graph=graph).coverage()
    assert coverage == {k: report[k] for k in ("edge", "no_resolver", "rate", "reason", "status", "tier", "total")}
    graph.serialize(tmp_path / "g.ttl", format="turtle")
    assert SPARQLEngine.from_turtle(tmp_path / "g.ttl").coverage() == coverage


MIXED = (
    "class Mixin:\n    def save(self):\n        pass\n\n\n"
    "class Model(dict, Mixin):\n    def run(self):\n        self.save()\n\n\n"  # dict comes first: save may be dict's
    "def outer():\n    Mixin().save()\n"
)


def test_callers_of_unknown_base_is_lower_bound(tmp_path):
    """A self.save() not found before an external base may still reach Mixin.save, which is after it in the MRO."""
    engine = _engine(tmp_path, {"m.py": MIXED})
    assert _summary(engine.callers_of("Mixin.save"), tmp_path) == [
        ("m.py::Mixin.save", [("outer", 12)], [], "lower-bound", {"unknown-base": 1}),
    ]


def test_blast_radius_unknown_base_is_lower_bound(tmp_path):
    engine = _engine(tmp_path, {"m.py": MIXED})
    [answer] = engine.blast_radius("Mixin.save")
    assert (answer.completeness, answer.reasons) == ("lower-bound", {"unknown-base": 1})


OVERRIDE = (
    "class Base:\n    def m(self):\n        pass\n\n    def run(self):\n        self.m()\n\n\n"  # lines 1-6
    "class Mid(Base):\n    pass\n\n\n"
    "class Sub(Mid):\n    def m(self):\n        super().m()\n\n\n"  # lines 13-15
    "def typed(b: Base):\n    b.m()\n\n\n"  # line 19
    "def direct():\n    Base.m(Sub())\n    Sub().m()\n"  # lines 23-24
)


def test_callers_of_override_lists_calls_to_the_base_version_as_possible(tmp_path):
    """A8: self.m() in Base.run and a b.m() on a Base may run Sub.m; Base.m(...) and super().m() never do."""
    engine = _engine(tmp_path, {"o.py": OVERRIDE})
    assert _summary(engine.callers_of("Sub.m"), tmp_path) == [
        ("o.py::Sub.m", [("direct", 24)], ["run", "typed"], "lower-bound", {"override": 2}),
    ]
    assert [c.reason for a in engine.callers_of("Sub.m") for c in a.possible] == ["override", "override"]
    assert _summary(engine.callers_of("Base.m"), tmp_path) == [  # the base version itself stays exact
        ("o.py::Base.m", [("run", 6), ("m", 15), ("typed", 19), ("direct", 23)], [], "exact", {}),
    ]


def test_blast_radius_override_is_lower_bound(tmp_path):
    engine = _engine(tmp_path, {"o.py": OVERRIDE})
    [answer] = engine.blast_radius("Sub.m")
    assert [i.name for i in answer.affected] == ["direct"]
    assert [(c.name, c.line, c.reason) for c in answer.possible] == [("run", 6, "override"), ("typed", 19, "override")]
    assert (answer.completeness, answer.reasons) == ("lower-bound", {"override": 2})


def test_callers_of_override_includes_jedi_calls(tmp_path):
    """A8: an x.m() only jedi resolves to Base.m (make() has no return annotation) may run Sub.m too."""
    pytest.importorskip("jedi")
    (tmp_path / "j.py").write_text(
        "class Base:\n    def m(self):\n        pass\n\n\nclass Sub(Base):\n    def m(self):\n        pass\n\n\n"
        "def make():\n    return Base()\n\n\ndef use():\n    x = make()\n    x.m()\n"  # line 17
    )
    engine = SPARQLEngine(graph=build_graph([tmp_path / "j.py"], tmp_path, use_jedi=True))
    assert [(s.line, s.status, s.resolved_to) for s in engine.call_sites(f"{tmp_path}/j.py::use") if s.target_name == "m"] == [
        (17, "resolved", f"{tmp_path}/j.py::Base.m"),
    ]
    assert engine.execute_raw_sparql("SELECT ?tier WHERE { ?s code:line 17 ; code:tier ?tier }") == [{"tier": "TJ"}]
    assert _summary(engine.callers_of("Sub.m"), tmp_path) == [("j.py::Sub.m", [], ["use"], "lower-bound", {"override": 1})]


INHERITED_INIT = (
    "class B:\n    def __init__(self):\n        pass\n\n\n"
    "class C(B):\n    pass\n\n\n"
    "class D(C):\n    def __init__(self):\n        super().__init__()\n\n\n"  # line 12
    "class E(dict, B):\n    pass\n\n\n"  # dict comes first: its __init__ may run
    "class F(B, dict):\n    pass\n\n\n"  # B.__init__ comes before dict
    "def make():\n    B()\n    C()\n    D()\n    E()\n    F()\n"  # lines 24-28
)


def test_callers_of_init_counts_creators_of_subclasses_that_inherit_it(tmp_path):
    """D9: C() and F() run B.__init__; D() runs D.__init__; E() may run dict's, so it is only possible."""
    engine = _engine(tmp_path, {"i.py": INHERITED_INIT})
    assert _summary(engine.callers_of("B.__init__"), tmp_path) == [
        ("i.py::B.__init__", [("__init__", 12), ("make", 24), ("make", 25), ("make", 28)], ["make"], "lower-bound", {"unknown-base": 1}),
    ]
    assert [(c.line, c.reason) for a in engine.callers_of("B.__init__") for c in a.possible] == [(27, "unknown-base")]
    assert _summary(engine.callers_of("D.__init__"), tmp_path) == [("i.py::D.__init__", [("make", 26)], [], "exact", {})]
    assert [(c.name, c.line) for a in engine.callers_of("C") for c in a.callers] == [("make", 25)]  # the class itself


def test_blast_radius_counts_an_unknown_base_creator_once(tmp_path):
    engine = _engine(tmp_path, {"i.py": INHERITED_INIT})
    [answer] = engine.blast_radius("B.__init__")
    assert [(i.name, i.distance) for i in answer.affected] == [("__init__", 1), ("make", 1)]  # D5: D.__init__ has a caller, make none
    assert [(c.name, c.line, c.reason) for c in answer.possible] == [("make", 27, "unknown-base")]
    assert (answer.completeness, answer.reasons) == ("lower-bound", {"unknown-base": 1})


def _uncached_definitions(engine: SPARQLEngine, name: str) -> list[tuple[str, str]]:
    """_definitions before the index: a scan of every definition per call."""
    g = engine.graph
    defs = sorted((engine._id(d), str(n)) for kind in (CODE.Function, CODE.Class)
                  for d in g.subjects(RDF.type, kind) for n in g.objects(d, CODE.name))
    exact = [d for d in defs if d[0] == name]
    if exact or "." not in name:
        return exact or [d for d in defs if d[1] == name]
    plain = name.replace(".<locals>", "")
    return [d for d in defs if d[0].split("::", 1)[-1].replace(".<locals>", "") == plain]


def test_definitions_index_matches_a_full_scan():
    from acte.mcp_server import _case_insensitive
    engine = SPARQLEngine(graph=build_graph(sorted(SRC.rglob("*.py")), SRC, use_jedi=False))
    symbols = engine._symbols()
    assert engine.symbols() == symbols and any(".<locals>." in s[0] for s in symbols)
    names = {"no_such_name", "no.such.name", "", "."}
    for d, _, bare, _, _ in symbols:
        qual = d.split("::", 1)[-1]
        names |= {d, bare, qual, qual.replace(".<locals>", ""), d.upper(), bare.upper(), qual.lower()}
    for name in sorted(names):
        assert engine._definitions(name) == _uncached_definitions(engine, name), name
        assert _case_insensitive(name, engine.symbols()) == _case_insensitive(name, symbols), name
    # a changed graph is indexed again
    f = next(iter(engine.graph.subjects(RDF.type, CODE.Function)))
    engine.graph.remove((f, None, None))
    assert engine._definitions(engine._id(f)) == _uncached_definitions(engine, engine._id(f)) == []
    assert engine.symbols() == engine._symbols() != symbols
