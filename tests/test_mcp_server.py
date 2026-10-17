import gc
import json

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
SMALL_TOOLS = ["find_symbol", "callers_of", "callees_of", "blast_radius"]


@pytest.fixture(autouse=True)
def _no_outer_repo(tmp_path, monkeypatch):
    # a git work tree that happens to enclose tmp_path must not be found
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _server(tmp_path, files):
    root = tmp_path / "proj"
    root.mkdir()
    for rel, code in files.items():
        (root / rel).write_text(code)
    graph = tmp_path / "graph.ttl"
    build(root, use_jedi=False).serialize(destination=graph, format="turtle")
    # the build's cyclic garbage holds tree-sitter parsers, which must not be freed on a tool's worker thread
    gc.collect()
    return create_server(graph)


@pytest.fixture
def server(tmp_path):
    return _server(tmp_path, PROJECT)


def _call(server, tool, **arguments):
    async def go():
        async with Client(server) as client:
            return await client.call_tool(tool, arguments)
    result = anyio.run(go)
    assert result.is_error is False
    return result.content[0].text


def test_lists_small_profile_tools(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools
    assert [t.name for t in anyio.run(go)] == SMALL_TOOLS


@pytest.mark.parametrize(("name", "expected"), [
    ("db.py::Database.connect", ['find_symbol "db.py::Database.connect": 1 match',
                                 "db.py::Database.connect (Function) db.py:2"]),
    ("connect", ['find_symbol "connect": 2 matches',
                 "db.py::Database.connect (Function) db.py:2",
                 "net.py::connect (Function) net.py:1"]),
    ("DATABASE", ['find_symbol "DATABASE": 1 match',
                  "db.py::Database (Class) db.py:1"]),
    ("conect", ["No symbol named conect. Did you mean: connect?"]),
    ("zzqqxx", ["No symbol named zzqqxx."]),
])
def test_find_symbol(server, name, expected):
    assert _call(server, "find_symbol", name=name) == "\n".join(expected)


def test_find_symbol_shows_ten_of_many(tmp_path):
    server = _server(tmp_path, {f"m{i:02}.py": "def run():\n    pass\n" for i in range(12)})
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
    ("callers_of", "conect", ["No symbol named conect. Did you mean: connect?"]),
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
def test_calls(tmp_path, tool, name, expected):
    assert _call(_server(tmp_path, CALLS), tool, name=name) == "\n".join(expected)


def test_callers_of_cut_to_budget(tmp_path):
    code = "def helper():\n    pass\n" + "".join(f"\n\ndef f{i:03}():\n    helper()\n" for i in range(300))
    text = _call(_server(tmp_path, {"big.py": code}), "callers_of", name="helper")
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[:2] == ["callers_of big.py::helper: 300 confirmed, 0 possible (exact)", "Confirmed:"]
    shown = len(lines) - 3
    assert lines[2:-1] == [f"big.py::f{i:03}  big.py:{6 + 4 * i}" for i in range(shown)]
    assert lines[-1] == f"Showing {shown} of 300."


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
    ("blast_radius", {"name": "bse"}, ["No symbol named bse. Did you mean: base?"]),
    ("callers_of", {"name": "BASE"}, ["callers_of chain.py::base: 1 confirmed, 0 possible (exact)",
                                      "Confirmed:", "chain.py::one  chain.py:6"]),
    ("callers_of", {"name": "ONE"}, ['callers_of "ONE": 2 definitions match. Ask again with one full name:',
                                     "chain.py::one (Function) chain.py:5", "other.py::one (Function) other.py:1"]),
    ("callees_of", {"name": "loop"}, ["callees_of rec.py::loop: 1 resolved, 0 external, 0 unresolved, 0 ambiguous",
                                      "Calls:", "rec.py::loop  (line 2)  (recursive)"]),
])
def test_chain(tmp_path, tool, arguments, expected):
    assert _call(_server(tmp_path, CHAIN), tool, **arguments) == "\n".join(expected)


def test_blast_radius_depth_clamped_to_three(tmp_path):
    server = _server(tmp_path, CHAIN)
    assert _call(server, "blast_radius", name="base", depth=9) == _call(server, "blast_radius", name="base", depth=3)


@pytest.mark.parametrize(("depth", "advice"), [(1, ""), (2, " Use a smaller depth.")])
def test_blast_radius_cut_to_budget(tmp_path, depth, advice):
    code = "def helper():\n    pass\n" + "".join(f"\n\ndef f{i:03}():\n    helper()\n" for i in range(300))
    text = _call(_server(tmp_path, {"big.py": code}), "blast_radius", name="helper", depth=depth)
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[:2] == [f"blast_radius big.py::helper (depth {depth}): 300 affected, 0 possible (exact)", "Distance 1:"]
    shown = len(lines) - 3
    assert lines[2:-1] == [f"big.py::f{i:03}  big.py:{6 + 4 * i}  (calls big.py::helper)" for i in range(shown)]
    assert lines[-1] == f"Showing {shown} of 300.{advice}"


def test_tool_list_size(server):
    async def go():
        async with Client(server) as client:
            return (await client.list_tools()).tools
    tools = anyio.run(go)
    assert all(len(t.description) <= 300 for t in tools)
    assert len(json.dumps([t.model_dump(include={"name", "description", "input_schema"}, by_alias=True) for t in tools])) <= 2000


def test_text_cuts_whole_lines_within_budget():
    items = [f"pkg/mod.py::f{i} (Function) pkg/mod.py:{i}" for i in range(400)]
    text = mcp_server._text("headline", [(None, items)], "Narrow with a file path.")
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[1:-1] == items[:len(lines) - 2]
    assert lines[-1] == f"Showing {len(lines) - 2} of 400. Narrow with a file path."


def test_text_uncut_has_no_showing_line():
    assert mcp_server._text("headline", [("Title:", ["a", "b"]), ("Empty:", [])], "x") == "headline\nTitle:\na\nb"
