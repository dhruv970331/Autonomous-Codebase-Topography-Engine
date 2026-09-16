from pathlib import Path

import pytest

from acte.parser import CodeParser
from acte.resolver import SymbolIndex

FILES = {
    "pkg/__init__.py": "from pkg.db import Database\n",
    "pkg/db.py": (
        "import os\n"
        "\n"
        "\n"
        "class Database:\n"
        "    def connect(self):\n"
        "        def retry():\n"
        "            pass\n"
        "        return retry()\n"
        "\n"
        "    class Inner:\n"
        "        def go(self):\n"
        "            pass\n"
        "\n"
        "\n"
        "def helper(path: str):\n"
        "    def nested():\n"
        "        pass\n"
        "    db = Database()\n"
        "    db = os.open(path)\n"
        "    return nested\n"
    ),
    "src/acte/parser.py": "def parse_file(path):\n    pass\n",
    "app.py": "def connect():\n    pass\n",
}


@pytest.fixture
def project(tmp_path) -> tuple[SymbolIndex, Path]:
    for rel, code in FILES.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(code)
    parser = CodeParser()
    return SymbolIndex([parser.parse_file(tmp_path / rel) for rel in sorted(FILES)], tmp_path), tmp_path


def test_by_id_holds_classes_and_functions(project):
    index, root = project
    db = f"{root}/pkg/db.py"
    assert sorted(index.by_id) == sorted([
        f"{root}/app.py::connect",
        f"{db}::Database", f"{db}::Database.connect", f"{db}::Database.connect.<locals>.retry",
        f"{db}::Database.Inner", f"{db}::Database.Inner.go", f"{db}::helper", f"{db}::helper.<locals>.nested",
        f"{root}/src/acte/parser.py::parse_file",
    ])
    assert index.by_id[f"{db}::Database.connect"].name == "connect"


def test_by_file_is_module_level_only(project):
    index, root = project
    db = f"{root}/pkg/db.py"
    assert index.by_file == {
        f"{root}/app.py": {"connect": f"{root}/app.py::connect"},
        f"{root}/pkg/__init__.py": {},
        db: {"Database": f"{db}::Database", "helper": f"{db}::helper"},
        f"{root}/src/acte/parser.py": {"parse_file": f"{root}/src/acte/parser.py::parse_file"},
    }
    # methods, nested classes and nested functions never reach by_file
    assert not {"connect", "go", "Inner", "retry", "nested"} & set(index.by_file[db])


def test_by_class_holds_direct_methods_only(project):
    index, root = project
    db = f"{root}/pkg/db.py"
    assert index.by_class == {
        f"{db}::Database": {"connect": f"{db}::Database.connect"},
        f"{db}::Database.Inner": {"go": f"{db}::Database.Inner.go"},
    }


def test_modules_and_lookup(project):
    index, root = project
    assert index.modules == {
        "app": f"{root}/app.py",
        "pkg": f"{root}/pkg/__init__.py",
        "pkg.db": f"{root}/pkg/db.py",
        "src.acte.parser": f"{root}/src/acte/parser.py",
    }
    assert index.modules_for("acte.parser") == [f"{root}/src/acte/parser.py"]
    assert index.modules_for("pkg") == [f"{root}/pkg/__init__.py"]
    assert index.modules_for("db") == [f"{root}/pkg/db.py"]
    assert index.modules_for("cte.parser") == []  # whole segments only
    assert index.modules_for("os") == []


@pytest.mark.parametrize("order", [1, -1])
def test_package_wins_over_same_named_module(tmp_path, order):
    files = [tmp_path / "pkg.py", tmp_path / "pkg" / "__init__.py"]
    files[1].parent.mkdir()
    for f in files:
        f.write_text("def f():\n    pass\n")
    parser = CodeParser()
    index = SymbolIndex([parser.parse_file(f) for f in files[::order]], tmp_path)
    assert index.modules == {"pkg": f"{tmp_path}/pkg/__init__.py"}


def test_by_loc(project):
    index, root = project
    db = f"{root}/pkg/db.py"
    assert index.by_loc == {
        (f"{root}/app.py", 1): f"{root}/app.py::connect",
        (db, 4): f"{db}::Database",
        (db, 5): f"{db}::Database.connect",
        (db, 6): f"{db}::Database.connect.<locals>.retry",
        (db, 10): f"{db}::Database.Inner",
        (db, 11): f"{db}::Database.Inner.go",
        (db, 15): f"{db}::helper",
        (db, 16): f"{db}::helper.<locals>.nested",
        (f"{root}/src/acte/parser.py", 1): f"{root}/src/acte/parser.py::parse_file",
    }


def test_locals_by_scope_sorted(project):
    index, root = project
    db = f"{root}/pkg/db.py"
    summary = {s: {k: [(b.kind, b.type_name, b.line) for b in v] for k, v in names.items()} for s, names in index.locals.items()}
    assert summary[f"{db}::helper"] == {
        "path": [("param", "str", 15)],
        "nested": [("def", None, 16)],
        "db": [("assign", "Database", 18), ("assign", "os.open", 19)],
    }
    assert summary[db] == {"Database": [("class", None, 4)], "helper": [("def", None, 15)]}
    assert summary[f"{db}::Database"] == {"connect": [("def", None, 5)], "Inner": [("class", None, 10)]}
    assert set(summary) == {
        f"{root}/app.py", db, f"{db}::Database", f"{db}::Database.connect", f"{db}::Database.Inner",
        f"{db}::Database.Inner.go", f"{db}::helper", f"{root}/src/acte/parser.py", f"{root}/src/acte/parser.py::parse_file",
    }


def test_imports_by_scope(project):
    index, root = project
    summary = {s: {k: [(b.module, b.imported_name, b.is_module_alias, b.line) for b in v] for k, v in names.items()} for s, names in index.imports.items()}
    assert summary == {
        f"{root}/pkg/__init__.py": {"Database": [("pkg.db", "Database", False, 1)]},
        f"{root}/pkg/db.py": {"os": [("os", "os", True, 1)]},
    }


def _index(root: Path, files: dict[str, str]) -> SymbolIndex:
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)
    parser = CodeParser()
    return SymbolIndex([parser.parse_file(root / rel) for rel in sorted(files)], root)


def test_exports_follow_re_exports(tmp_path):
    index = _index(tmp_path, {
        "pkg/__init__.py": "import os\nfrom os import path\nfrom .db import Database\nfrom .db import connect as open_db\nfrom .sub import Impl\nfrom . import sub\n",
        "pkg/db.py": "class Database:\n    def connect(self):\n        pass\n\n\ndef connect():\n    pass\n",
        "pkg/sub/__init__.py": "from .impl import Impl\n",
        "pkg/sub/impl.py": "class Impl:\n    pass\n",
        "app.py": "from pkg import Database, open_db\nfrom pkg.sub import Impl as I\n",
    })
    r = tmp_path
    assert index.exports[f"{r}/pkg/__init__.py"] == {
        "Database": f"{r}/pkg/db.py::Database",
        "open_db": f"{r}/pkg/db.py::connect",
        "Impl": f"{r}/pkg/sub/impl.py::Impl",  # two levels: pkg -> pkg.sub -> pkg.sub.impl
    }  # `os`, `path` are external; `sub` is a module, not a definition
    assert index.exports[f"{r}/pkg/sub/__init__.py"] == {"Impl": f"{r}/pkg/sub/impl.py::Impl"}
    assert index.exports[f"{r}/pkg/db.py"] == index.by_file[f"{r}/pkg/db.py"]
    assert index.exports[f"{r}/app.py"] == {
        "Database": f"{r}/pkg/db.py::Database",
        "open_db": f"{r}/pkg/db.py::connect",
        "I": f"{r}/pkg/sub/impl.py::Impl",
    }


def test_exports_skip_ambiguous_modules_and_conflicts(tmp_path):
    index = _index(tmp_path, {
        "x/util.py": "def h():\n    pass\n",
        "y/util.py": "def h():\n    pass\n",
        "a.py": "from util import h\n",  # util matches two files: not exported
        "b.py": "from x.util import h\n\n\ndef h():\n    pass\n",  # import and def disagree
    })
    assert index.module_files("util", f"{tmp_path}/a.py") == [f"{tmp_path}/x/util.py", f"{tmp_path}/y/util.py"]
    assert index.exports[f"{tmp_path}/a.py"] == {}
    assert index.exports[f"{tmp_path}/b.py"] == {}


def test_exports_cycle_terminates(tmp_path):
    index = _index(tmp_path, {
        "a.py": "from b import g, x\n\n\ndef f():\n    pass\n",
        "b.py": "from a import f, x\n\n\ndef g():\n    pass\n",
    })
    assert index.exports[f"{tmp_path}/a.py"] == {"f": f"{tmp_path}/a.py::f", "g": f"{tmp_path}/b.py::g"}
    assert index.exports[f"{tmp_path}/b.py"] == {"g": f"{tmp_path}/b.py::g", "f": f"{tmp_path}/a.py::f"}


def test_module_files_relative(tmp_path):
    index = _index(tmp_path, {
        "src/pkg/__init__.py": "",
        "src/pkg/a.py": "",
        "src/pkg/sub/__init__.py": "",
        "src/pkg/sub/b.py": "",
    })
    b, sub = f"{tmp_path}/src/pkg/sub/b.py", f"{tmp_path}/src/pkg/sub/__init__.py"
    assert index.module_files(".", b) == [sub]
    assert index.module_files(".", sub) == [sub]  # an __init__'s package is itself
    assert index.module_files("..a", b) == [f"{tmp_path}/src/pkg/a.py"]
    assert index.module_files("...", b) == []  # `src` is a directory, not a module
    assert index.module_files(".....x", b) == []  # above the root
    assert index.module_files("pkg.sub", b) == [sub]  # src-layout suffix match


def test_exports_follow_star_imports(tmp_path):
    index = _index(tmp_path, {
        "pkg/__init__.py": "from .api import *\n",
        "pkg/api.py": "from .db import *\nfrom .db import _hidden\n\n\ndef run():\n    pass\n",
        "pkg/db.py": "class Database:\n    pass\n\n\ndef _hidden():\n    pass\n",
    })
    r = tmp_path
    assert {s: [(b.module, b.line) for b in v] for s, v in index.star_imports.items()} == {
        f"{r}/pkg/__init__.py": [(".api", 1)],
        f"{r}/pkg/api.py": [(".db", 1)],
    }
    assert index.exports[f"{r}/pkg/api.py"] == {
        "Database": f"{r}/pkg/db.py::Database",
        "_hidden": f"{r}/pkg/db.py::_hidden",  # named import: private names re-export too
        "run": f"{r}/pkg/api.py::run",
    }
    # two levels of `*`; `*` skips names starting with "_"
    assert index.exports[f"{r}/pkg/__init__.py"] == {"Database": f"{r}/pkg/db.py::Database", "run": f"{r}/pkg/api.py::run"}
