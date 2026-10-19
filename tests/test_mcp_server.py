import json
import subprocess
from importlib.metadata import version

import anyio
import pytest
from mcp import Client

from acte import mcp_server
from acte.cli import build
from acte.mcp_server import create_server

PROJECT = {
    "db.py": "class Database:\n    def connect(self):\n        pass\n",
    "net.py": "def connect(host):\n    pass\n",
}
# run() makes one call of each kind; go()'s tick() could be m1's or m2's
CALLS = {
    "db.py": "class Database:\n    def connect(self):\n        pass\n\n    def save(self):\n        self.connect()\n",
    "store.py": "class Store:\n    def save(self):\n        pass\n",
    "app.py": ("import os\n\nfrom db import Database\n\n\n"
               "def run(x, factory):\n    db = Database()\n    db.connect()\n    x.save()\n    factory()\n"
               "    print(len(os.path.join(\"a\", \"b\")))\n    helper()\n    missing()\n\n\n"
               "def helper():\n    pass\n"),
    "m1.py": "def tick():\n    pass\n",
    "m2.py": "def tick():\n    pass\n",
    "hub.py": "from m1 import *\nfrom m2 import *\n\n\ndef go():\n    tick()\n",
}
# a chain base <- one <- two <- three <- four; hub.go's one() could be chain's or other's
CHAIN = {
    "chain.py": "".join(f"def {f}():\n    {c}\n\n\n" for f, c in
                        [("base", "pass"), ("one", "base()"), ("two", "one()"), ("three", "two()"), ("four", "three()")]),
    "other.py": "def one():\n    pass\n",
    "hub.py": "from chain import *\nfrom other import *\n\n\ndef go():\n    one()\n",
    "rec.py": "def loop(n):\n    loop(n - 1)\n",
}
# helper has 60 callers at distance 1; base has mid at distance 1 and 60 callers of mid at distance 2.
# The long names make 60 lines overflow the budget.
LONG = "handle_request_with_a_long_descriptive_name_number_"
BIG = {
    "big.py": ("def helper():\n    pass\n\n\ndef base():\n    pass\n\n\ndef mid():\n    base()\n"
               + "".join(f"\n\ndef {LONG}{i:02}():\n    helper()\n" for i in range(60))),
    "far.py": "from big import mid\n" + "".join(f"\n\ndef {LONG}{i:02}():\n    mid()\n" for i in range(60)),
}
SMALL_TOOLS = ["find_symbol", "callers_of", "callees_of", "blast_radius"]
FULL_TOOLS = [*SMALL_TOOLS, "functions_in_file", "dependencies_of", "orphan_functions", "graph_info"]


def _write(root, files):
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)


def _build(root, graph):
    build(root, use_jedi=False).serialize(destination=graph, format="turtle")


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def graph_of(tmp_path_factory):
    """graph_of(files): the Turtle graph of a project made of *files*, built once per module."""
    graphs = {}

    def get(files):
        key = tuple(sorted(files.items()))
        if key not in graphs:
            root = tmp_path_factory.mktemp("proj")
            _write(root, files)
            graphs[key] = root.parent / f"{root.name}.ttl"
            with pytest.MonkeyPatch.context() as mp:
                mp.setenv("GIT_CEILING_DIRECTORIES", str(root.parent))  # a work tree enclosing it must not be found
                _build(root, graphs[key])
        return graphs[key]
    return get


@pytest.fixture
def serve(graph_of):
    """serve(files, **create_server options): a server over the project made of *files*."""
    return lambda files, **options: create_server(graph_of(files), **options)


@pytest.fixture
def server(serve):
    return serve(PROJECT)


def _call(server, tool, **arguments):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)
    result = anyio.run(go)
    assert result.is_error is False
    return result.content[0].text


def _tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools
    return anyio.run(go)


def test_lists_small_profile_tools(server):
    assert [t.name for t in _tools(server)] == SMALL_TOOLS


@pytest.mark.parametrize(("name", "expected"), [
    ("db.py::Database.connect", ['find_symbol "db.py::Database.connect": 1 match',
                                 "db.py::Database.connect (Function) db.py:2"]),
    ("connect", ['find_symbol "connect": 2 matches',
                 "db.py::Database.connect (Function) db.py:2",
                 "net.py::connect (Function) net.py:1"]),
    ("DATABASE", ['find_symbol "DATABASE": 1 match',
                  "db.py::Database (Class) db.py:1"]),
    ("conect", ["No symbol named conect. Did you mean: connect? Call the tool again with one of these names."]),
    ("zzqqxx", ["No symbol named zzqqxx."]),
])
def test_find_symbol(server, name, expected):
    assert _call(server, "find_symbol", name=name) == "\n".join(expected)


@pytest.mark.parametrize(("files", "name", "expected"), [
    ({"m.py": "def variable():\n    pass\n"}, "variabel", "Did you mean: variable? Call the tool again with one of these names."),
    ({"m.py": "def variable():\n    pass\n"}, "m.py::varable", "Did you mean: variable? Call the tool again with one of these names."),
    # the bare name names two symbols, each id one: three different suggestions
    ({"m.py": "def variable():\n    pass\n", "n.py": "def variable():\n    pass\n"}, "variabel",
     "Did you mean: variable, n.py::variable, m.py::variable? Call the tool again with one of these names."),  # equal scores: difflib orders them
])
def test_did_you_mean_names_each_symbol_once(serve, files, name, expected):
    assert _call(serve(files), "find_symbol", name=name) == f"No symbol named {name}. {expected}"


def test_find_symbol_shows_ten_of_many(serve):
    server = serve({f"m{i:02}.py": "def run():\n    pass\n" for i in range(12)})
    lines = _call(server, "find_symbol", name="run").splitlines()
    assert lines[0] == 'find_symbol "run": 12 matches'
    assert lines[1:11] == [f"m{i:02}.py::run (Function) m{i:02}.py:1" for i in range(10)]
    assert lines[11:] == ["Showing 10 of 12. Narrow with a fuller name, such as Class.method."]


@pytest.mark.parametrize(("tool", "name", "expected"), [
    ("callers_of", "Database.connect", ["callers_of db.py::Database.connect: 2 confirmed, 0 possible (exact)",
                                        "Confirmed:",
                                        "app.py::run  app.py:8",
                                        "db.py::Database.save  db.py:6"]),
    ("callers_of", "Database.save", [("callers_of db.py::Database.save: 0 confirmed, 0 possible "
                                     "(lower-bound: 1 call on objects of unknown type)")]),
    ("callers_of", "m1.py::tick", [("callers_of m1.py::tick: 0 confirmed, 1 possible "
                                   "(lower-bound: 1 call that could go to several functions)"),
                                   "Possible (could not tell which function is called):",
                                   "hub.py::go  hub.py:6"]),
    ("callers_of", "save", ['callers_of "save": 2 definitions match. Ask again with one full name:',
                            "db.py::Database.save (Function) db.py:5",
                            "store.py::Store.save (Function) store.py:2"]),
    ("callers_of", "conect", ["No symbol named conect. Did you mean: connect? Call the tool again with one of these names."]),
    ("callees_of", "run", ["callees_of app.py::run: 3 resolved, 3 external, 3 unresolved, 0 ambiguous",
                           "Calls:",
                           "db.py::Database  (line 7)",
                           "db.py::Database.connect  (line 8)",
                           "app.py::helper  (line 12)",
                           "External: print, len, os.path.join (3 calls)",
                           ("Unresolved: 1 call on objects of unknown type, 1 call through local variables, "
                           "1 call to names not found")]),
    ("callees_of", "go", ["callees_of hub.py::go: 0 resolved, 0 external, 0 unresolved, 1 ambiguous",
                          "Ambiguous: 1 call that could go to several functions"]),
    ("callees_of", "tick", ['callees_of "tick": 2 definitions match. Ask again with one full name:',
                            "m1.py::tick (Function) m1.py:1",
                            "m2.py::tick (Function) m2.py:1"]),
])
def test_calls(serve, tool, name, expected):
    assert _call(serve(CALLS), tool, name=name) == "\n".join(expected)


def test_callers_of_cut_to_budget(serve):
    text = _call(serve(BIG), "callers_of", name="helper")
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[:2] == ["callers_of big.py::helper: 60 confirmed, 0 possible (exact)", "Confirmed:"]
    shown = len(lines) - 3
    assert lines[2:-1] == [f"big.py::{LONG}{i:02}  big.py:{14 + 4 * i}" for i in range(shown)]
    assert lines[-1] == f"Showing {shown} of 60."


@pytest.mark.parametrize(("tool", "arguments", "expected"), [
    ("blast_radius", {"name": "base"}, [
        ("blast_radius chain.py::base (depth 2): 2 affected, 1 possible (lower-bound: 1 call that could go to "
        "several functions, stopped at depth 2 while callers continue)"),
        "Distance 1:", "chain.py::one  chain.py:6  (calls chain.py::base)",
        "Distance 2:", "chain.py::two  chain.py:10  (calls chain.py::one)",
        "Possible:", "hub.py::go  hub.py:6"]),
    ("blast_radius", {"name": "base", "depth": 3}, [
        ("blast_radius chain.py::base (depth 3): 3 affected, 1 possible (lower-bound: 1 call that could go to "
        "several functions, stopped at depth 3 while callers continue)"),
        "Distance 1:", "chain.py::one  chain.py:6  (calls chain.py::base)",
        "Distance 2:", "chain.py::two  chain.py:10  (calls chain.py::one)",
        "Distance 3:", "chain.py::three  chain.py:14  (calls chain.py::two)",
        "Possible:", "hub.py::go  hub.py:6"]),
    ("blast_radius", {"name": "base", "depth": 0}, [
        "blast_radius chain.py::base (depth 1): 1 affected, 0 possible (lower-bound: stopped at depth 1 while callers continue)",
        "Distance 1:", "chain.py::one  chain.py:6  (calls chain.py::base)"]),
    ("blast_radius", {"name": "four"}, ["blast_radius chain.py::four (depth 2): 0 affected, 0 possible (exact)"]),
    ("blast_radius", {"name": "bse"}, ["No symbol named bse. Did you mean: base? Call the tool again with one of these names."]),
    ("callers_of", {"name": "BASE"}, ["callers_of chain.py::base: 1 confirmed, 0 possible (exact)",
                                      "Confirmed:", "chain.py::one  chain.py:6"]),
    ("callers_of", {"name": "ONE"}, ['callers_of "ONE": 2 definitions match. Ask again with one full name:',
                                     "chain.py::one (Function) chain.py:5", "other.py::one (Function) other.py:1"]),
    ("callees_of", {"name": "loop"}, ["callees_of rec.py::loop: 1 resolved, 0 external, 0 unresolved, 0 ambiguous",
                                      "Calls:", "rec.py::loop  (line 2)  (recursive)"]),
])
def test_chain(serve, tool, arguments, expected):
    assert _call(serve(CHAIN), tool, **arguments) == "\n".join(expected)


def test_blast_radius_depth_clamped_to_three(serve):
    server = serve(CHAIN)
    assert _call(server, "blast_radius", name="base", depth=9) == _call(server, "blast_radius", name="base", depth=3)


@pytest.mark.parametrize("depth", [1, 2])
def test_blast_radius_cut_to_budget(serve, depth):
    text = _call(serve(BIG), "blast_radius", name="helper", depth=depth)
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[:2] == [f"blast_radius big.py::helper (depth {depth}): 60 affected, 0 possible (exact)", "Distance 1:"]
    shown = len(lines) - 3
    assert lines[2:-1] == [f"big.py::{LONG}{i:02}  big.py:{14 + 4 * i}  (calls big.py::helper)" for i in range(shown)]
    assert lines[-1] == f"Showing {shown} of 60."  # all at distance 1: a smaller depth would not help


def test_blast_radius_advises_smaller_depth_when_it_helps(serve):
    text = _call(serve(BIG), "blast_radius", name="base", depth=2)
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[:4] == ["blast_radius big.py::base (depth 2): 61 affected, 0 possible (exact)",
                         "Distance 1:", "big.py::mid  big.py:10  (calls big.py::base)", "Distance 2:"]
    shown = len(lines) - 5
    assert lines[4:-1] == [f"far.py::{LONG}{i:02}  far.py:{5 + 4 * i}  (calls big.py::mid)" for i in range(shown)]
    assert lines[-1] == f"Showing {shown + 1} of 61. Use a smaller depth."


def test_tool_list_size(server):
    tools = _tools(server)
    assert all(len(t.description) <= 300 for t in tools)
    assert len(json.dumps([t.model_dump(include={"name", "description", "input_schema"}, by_alias=True) for t in tools])) <= 2000


def test_full_profile_lists_eight_tools(serve):
    assert [t.name for t in _tools(serve(CALLS, profile="full"))] == FULL_TOOLS


@pytest.mark.parametrize(("tool", "arguments", "expected"), [
    ("functions_in_file", {"path": "db.py"}, ["functions_in_file db.py: 2 functions, 1 class",
                                              "db.py::Database (Class) db.py:1",
                                              "db.py::Database.connect (Function) db.py:2",
                                              "db.py::Database.save (Function) db.py:5"]),
    ("functions_in_file", {"path": "apps.py"}, ["functions_in_file: no file apps.py in the map. Did you mean: app.py? Call the tool again with one of these names."]),
    ("dependencies_of", {"path": "app.py"}, ["dependencies_of app.py: 1 project file, 1 outside module",
                                             "Project files:", "db.py", "Outside modules:", "os"]),
    ("dependencies_of", {"path": "hub.py"}, ["dependencies_of hub.py: 2 project files, 0 outside modules",
                                             "Project files:", "m1.py", "m2.py"]),
    ("dependencies_of", {"path": "nope/zz.txt"}, ["dependencies_of: no file nope/zz.txt in the map."]),
    ("orphan_functions", {}, [("orphan_functions: 6 functions no resolved call reaches; entry points, __init__, "
                               "callbacks and dynamically called code also appear here"),
                              "app.py::run  app.py:6", "db.py::Database.save  db.py:5", "hub.py::go  hub.py:5",
                              "m1.py::tick  m1.py:1", "m2.py::tick  m2.py:1", "store.py::Store.save  store.py:2"]),
])
def test_full_profile_tools(serve, tool, arguments, expected):
    assert _call(serve(CALLS, profile="full"), tool, **arguments) == "\n".join(expected)


def test_graph_info(serve):
    lines = _call(serve(CALLS, profile="full"), "graph_info").splitlines()
    assert lines[0].startswith("graph_info: proj")
    assert lines[0].endswith(f", commit unknown, built by acte {version('acte')} without jedi")
    assert lines[1:] == [
        "Size: 6 files, 8 functions, 2 classes, 11 calls",
        "Coverage: 50.0% of calls resolved (calls to outside code not counted)",
        "Status: 1 ambiguous, 3 external, 4 resolved, 3 unresolved",
        ("Resolved: 1 call on self or cls, 1 call to names in the same file, 1 call through imports, "
         "1 call on variables of known type"),
        ("Not resolved to project code: 2 calls to builtins, 1 call through local variables, 1 call to names not found, "
         "1 call to the standard library, 1 call on objects of unknown type"),
        "Health: ok",
    ]


CALLS_EACH_TOOL = [("find_symbol", {"name": "connect"}), ("callers_of", {"name": "zz"}), ("callees_of", {"name": "run"}),
                   ("blast_radius", {"name": "helper"}), ("functions_in_file", {"path": "db.py"}),
                   ("dependencies_of", {"path": "app.py"}), ("orphan_functions", {}), ("graph_info", {})]


def test_stale_warning_after_a_new_commit(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))  # a work tree enclosing tmp_path must not be found
    root = tmp_path / "proj"
    _write(root, CALLS)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "one")
    old = _git(root, "rev-parse", "HEAD")
    graph = tmp_path / "graph.ttl"
    _build(root, graph)
    server = create_server(graph, project=root, profile="full")
    for tool, arguments in CALLS_EACH_TOOL:
        assert not _call(server, tool, **arguments).startswith("Warning")
    _write(root, {"new.py": "x = 1\n"})
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "two")
    new = _git(root, "rev-parse", "HEAD")
    warning = f"Warning: this map was built at commit {old[:7]} but the project is now at {new[:7]}; answers may be out of date."
    plain = create_server(graph, profile="full")  # no --project: never a warning
    for tool, arguments in CALLS_EACH_TOOL:
        answer = _call(plain, tool, **arguments)
        assert not answer.startswith("Warning")
        assert _call(server, tool, **arguments) == f"{warning}\n{answer}"
    # a project git cannot read: no warning, and no error
    missing = create_server(graph, project=tmp_path / "missing", profile="full")
    for tool, arguments in CALLS_EACH_TOOL:
        assert _call(missing, tool, **arguments) == _call(plain, tool, **arguments)


def test_text_cuts_whole_lines_within_budget():
    items = [f"pkg/mod.py::f{i} (Function) pkg/mod.py:{i}" for i in range(400)]
    text = mcp_server._text("headline", [(None, items)], "Narrow with a file path.")
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[1:-1] == items[:len(lines) - 2]
    assert lines[-1] == f"Showing {len(lines) - 2} of 400. Narrow with a file path."


def test_text_uncut_has_no_showing_line():
    assert mcp_server._text("headline", [("Title:", ["a", "b"]), ("Empty:", [])], "x") == "headline\nTitle:\na\nb"


def test_callers_of_names_unknown_bases_in_plain_words(serve):
    server = serve({"m.py": "class Mixin:\n    def save(self):\n        pass\n\n\nclass Model(dict, Mixin):\n    def run(self):\n        self.save()\n"})
    assert _call(server, "callers_of", name="Mixin.save") == (
        "callers_of m.py::Mixin.save: 0 confirmed, 0 possible "
        "(lower-bound: 1 call to methods that may be in base classes outside the project or unknown)"
    )


def test_callers_of_override_in_plain_words(serve):
    server = serve({"o.py": ("class Base:\n    def m(self):\n        pass\n\n    def run(self):\n        self.m()\n\n\n"
                             "class Sub(Base):\n    def m(self):\n        pass\n")})
    assert _call(server, "callers_of", name="Sub.m") == (
        "callers_of o.py::Sub.m: 0 confirmed, 1 possible "
        "(lower-bound: 1 call to the base-class version that may run this override)\n"
        "Possible (call the base-class version, which may run this override):\n"
        "o.py::Base.run  o.py:6"
    )
    assert _call(server, "blast_radius", name="Sub.m") == (
        "blast_radius o.py::Sub.m (depth 2): 0 affected, 1 possible "
        "(lower-bound: 1 call to the base-class version that may run this override)\n"
        "Possible:\n"
        "o.py::Base.run  o.py:6"
    )


def test_callers_of_init_behind_an_outside_base_in_plain_words(serve):
    server = serve({"i.py": ("class B:\n    def __init__(self):\n        pass\n\n\nclass C(B):\n    pass\n\n\n"
                             "class E(dict, B):\n    pass\n\n\ndef make():\n    C()\n    E()\n")})
    assert _call(server, "callers_of", name="B.__init__") == (
        "callers_of i.py::B.__init__: 1 confirmed, 1 possible "
        "(lower-bound: 1 call to methods that may be in base classes outside the project or unknown)\n"
        "Confirmed:\n"
        "i.py::make  i.py:15\n"
        "Possible (create a subclass that may get its __init__ from a base class outside the project):\n"
        "i.py::make  i.py:16"
    )
