import pytest

from acte.analysis import ImportCycle, import_cycles
from acte.cli import build


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
