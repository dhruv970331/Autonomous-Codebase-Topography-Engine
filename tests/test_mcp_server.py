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
SMALL_TOOLS = ["find_symbol"]


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


def test_text_cuts_whole_lines_within_budget():
    items = [f"pkg/mod.py::f{i} (Function) pkg/mod.py:{i}" for i in range(400)]
    text = mcp_server._text("headline", items, 400, "a file path")
    lines = text.splitlines()
    assert len(text) <= mcp_server._BUDGET
    assert lines[1:-1] == items[:len(lines) - 2]
    assert lines[-1] == f"Showing {len(lines) - 2} of 400. Narrow with a file path."


def test_text_uncut_has_no_showing_line():
    assert mcp_server._text("headline", ["a", "b"], 2, "x") == "headline\na\nb"
