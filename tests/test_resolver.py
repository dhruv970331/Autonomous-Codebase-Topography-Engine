from pathlib import Path

import pytest

from acte.parser import CodeParser
from acte.resolver import CANDIDATE_CEILING, SymbolIndex, resolve

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
    assert index.modules_for("db") == []  # exact names only: pkg/db.py is pkg.db
    assert index.modules_for("cte.parser") == []
    assert index.modules_for("os") == []


def test_package_is_the_module_of_its_name(tmp_path):
    index = _index(tmp_path, {rel: "def f():\n    pass\n" for rel in ("nested/__init__.py", "nested/nested.py", "src/acte/parser.py")})
    assert index.modules_for("nested") == [f"{tmp_path}/nested/__init__.py"]
    assert index.modules_for("nested.nested") == [f"{tmp_path}/nested/nested.py"]
    assert index.modules_for("acte.parser") == [f"{tmp_path}/src/acte/parser.py"]  # src/ is a source root


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
        "util.py": "def h():\n    pass\n",
        "src/util.py": "def h():\n    pass\n",
        "x/util.py": "def h():\n    pass\n",
        "a.py": "from util import h\n",  # util is util.py and src/util.py: not exported
        "b.py": "from x.util import h\n\n\ndef h():\n    pass\n",  # import and def disagree
    })
    assert index.module_files("util", f"{tmp_path}/a.py") == [f"{tmp_path}/src/util.py", f"{tmp_path}/util.py"]
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
    assert index.module_files("pkg.sub", b) == [sub]  # src/ is a source root


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


def _calls(root: Path, files: dict[str, str], use_jedi: bool = False) -> dict[tuple[str, str], tuple]:
    """(caller relative to root, called name) -> (status, tier, target relative to root, reason), from the real pipeline.

    The ladder alone unless *use_jedi*: tier TJ changes many unknown-receiver results."""
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code, encoding="utf-8")
    parser = CodeParser()
    resolution = resolve([parser.parse_file(root / rel) for rel in sorted(files)], root, use_jedi)
    rel = lambda s: s.replace(f"{root}/", "") if s else s
    out = {}
    for r in resolution.calls:
        key = (rel(r.call.caller_id), r.call.target_name)
        assert key not in out, f"test project has two calls {key}"
        out[key] = (r.status, r.tier, rel(r.target), r.reason)
    return out


def test_bare_call_never_reaches_a_method(tmp_path):
    calls = _calls(tmp_path, {
        "a.py": "def helper():\n    pass\n\n\nclass C:\n    def helper(self):\n        pass\n\n    def run(self):\n        helper()\n",
        "b.py": "class K:\n    def only(self):\n        pass\n\n    def go(self):\n        only()\n",
    })
    assert calls[("a.py::C.run", "helper")] == ("resolved", "T2", "a.py::helper", None)  # 5a
    assert calls[("b.py::K.go", "only")] == ("unresolved", None, None, "no-match")  # 5c: the only `only` is a method


def test_local_names_shadow_imports(tmp_path):
    calls = _calls(tmp_path, {
        "payment.py": "def charge(card, amount):\n    pass\n",
        "checkout.py": "from payment import charge\n\n\ndef place_order(cart, card, charge):\n    charge(card, cart)\n",
        "n4.py": "from payment import charge as real_charge\n\n\ndef place_order(cart, card, charge):\n    charge(card, cart)\n",
    })
    assert calls[("checkout.py::place_order", "charge")] == ("unresolved", None, None, "local")  # 5b
    assert calls[("n4.py::place_order", "charge")] == ("unresolved", None, None, "local")  # N4


def test_external_imports_stop_the_ladder(tmp_path):
    calls = _calls(tmp_path, {
        "cache.py": "def get(key):\n    pass\n",
        "client.py": "from requests import get\n\n\ndef fetch(url):\n    get(url)\n",  # 6b, N5
        "payment.py": "def charge():\n    pass\n",
        "tests/stripe_fake.py": "def charge():\n    pass\n",
        "pay.py": "from stripe import charge\n\n\ndef pay():\n    charge()\n",  # N9
        "paths.py": "from os import path\n\n\ndef p():\n    path()\n",
        "np.py": "import numpy\n\n\ndef n():\n    numpy()\n",
    })
    assert calls[("client.py::fetch", "get")] == ("external", None, None, "third-party")
    assert calls[("pay.py::pay", "charge")] == ("external", None, None, "third-party")
    assert calls[("paths.py::p", "path")] == ("external", None, None, "stdlib")
    assert calls[("np.py::n", "numpy")] == ("external", None, None, "third-party")


def test_builtins(tmp_path):
    calls = _calls(tmp_path, {
        "report.py": "def format(order):\n    pass\n",
        "money.py": "def show(amount):\n    format(amount, '.2f')\n    len(amount)\n",  # N6
        "own.py": "def print():\n    pass\n\n\ndef f():\n    print()\n",
        "star.py": "from report import *\n\n\ndef g(o):\n    format(o)\n",
    })
    assert calls[("money.py::show", "format")] == ("external", None, None, "builtin")
    assert calls[("money.py::show", "len")] == ("external", None, None, "builtin")
    assert calls[("own.py::f", "print")] == ("resolved", "T2", "own.py::print", None)  # R13: defined, so not a builtin
    assert calls[("star.py::g", "format")] == ("resolved", "T3*", "report.py::format", None)  # star import shadows the builtin


def test_nested_definitions(tmp_path):
    calls = _calls(tmp_path, {
        "a.py": "def helper():\n    pass\n\n\ndef outer():\n    def helper():\n        pass\n    return helper()\n",
        "b.py": "def helper():\n    pass\n",
        "c.py": "def outer():\n    def helper():\n        pass\n\n    class Inner:\n        def m(self):\n            helper()\n",
    })
    assert calls[("a.py::outer", "helper")] == ("resolved", "T2", "a.py::outer.<locals>.helper", None)  # 13, N7
    # through a class scope (skipped) to the enclosing function
    assert calls[("c.py::outer.<locals>.Inner.m", "helper")] == ("resolved", "T2", "c.py::outer.<locals>.helper", None)


def test_scoped_imports(tmp_path):
    calls = _calls(tmp_path, {
        "payment.py": "def charge(x):\n    pass\n",
        "a.py": "def load():\n    from payment import charge\n    charge(1)\n\n\ndef third():\n    charge(2)\n",
    })
    assert calls[("a.py::load", "charge")] == ("resolved", "T3", "payment.py::charge", None)  # 14, N12
    assert calls[("a.py::third", "charge")] == ("unresolved", None, None, "no-match")


def test_global_declaration_skips_enclosing_bindings(tmp_path):
    calls = _calls(tmp_path, {
        "a.py": "def charge():\n    pass\n\n\ndef outer():\n    charge = 1\n\n    def inner():\n        global charge\n        charge()\n",
    })
    assert calls[("a.py::outer.<locals>.inner", "charge")] == ("resolved", "T2", "a.py::charge", None)  # 19


def test_constructor_and_recursion(tmp_path):
    resolution = resolve([CodeParser().parse_file(_write(tmp_path / "a.py", "class Database:\n    pass\n\n\ndef walk(n):\n    if n:\n        walk(n - 1)\n    Database()\n"))], tmp_path)
    got = {r.call.target_name: (r.status, r.edge, r.recursive) for r in resolution.calls}
    assert got == {"walk": ("resolved", "calls", True), "Database": ("resolved", "instantiates", False)}  # R11 (N11), D9


def _write(path: Path, code: str) -> Path:
    path.write_text(code)
    return path


def test_imports_resolve_through_exports(tmp_path):
    calls = _calls(tmp_path, {
        "pkg/__init__.py": "from .db import connect\n",
        "pkg/db.py": "def connect():\n    pass\n",
        "app.py": "from pkg import connect\n\n\ndef f():\n    connect()\n",
        "star.py": "from pkg.db import *\n\n\ndef g():\n    connect()\n",
        "m1.py": "def run():\n    pass\n",
        "m2.py": "def run():\n    pass\n",
        "both.py": "from m1 import *\nfrom m2 import *\n\nrun()\n",
        "fallback.py": "try:\n    from m1 import run\nexcept ImportError:\n    def run():\n        pass\n\nrun()\n",
        "lib.py": "def run():\n    pass\n",
        "other.py": "def go():\n    pass\n",
        "star_after.py": "from lib import run\nfrom m2 import *\n\nrun()\n",  # Python calls m2.run
        "star_other.py": "from lib import run\nfrom other import *\n\nrun()\n",
    })
    assert calls[("app.py::f", "connect")] == ("resolved", "T3", "pkg/db.py::connect", None)
    assert calls[("star.py::g", "connect")] == ("resolved", "T3*", "pkg/db.py::connect", None)
    assert calls[("both.py", "run")] == ("ambiguous", None, None, None)
    assert calls[("fallback.py", "run")] == ("ambiguous", None, None, None)  # def and import disagree
    assert calls[("star_after.py", "run")] == ("ambiguous", None, None, None)  # import and `*` disagree
    assert calls[("star_other.py", "run")] == ("resolved", "T3", "lib.py::run", None)
    resolution = resolve([CodeParser().parse_file(f) for f in sorted(tmp_path.glob("*.py"))], tmp_path, use_jedi=False)
    star_after = next(r for r in resolution.calls if r.call.file_path.endswith("star_after.py"))
    assert star_after.candidates == [f"{tmp_path}/lib.py::run", f"{tmp_path}/m2.py::run"]


def test_ambiguous_candidates_and_ceiling(tmp_path):
    files = {f"d{i}/util.py": "def h():\n    pass\n" for i in range(CANDIDATE_CEILING + 1)}
    files["two/m1.py"] = files["two/m2.py"] = "def run():\n    pass\n"
    stars = "".join(f"from d{i}.util import *\n" for i in range(CANDIDATE_CEILING + 1))
    files["a.py"] = stars + "from two.m1 import *\nfrom two.m2 import *\n\n\ndef f():\n    h()\n    run()\n"
    parser = CodeParser()
    for rel, code in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(code)
    calls = {r.call.target_name: r for r in resolve([parser.parse_file(tmp_path / rel) for rel in sorted(files)], tmp_path).calls}
    h, run = calls["h"], calls["run"]
    assert (h.status, h.candidates, h.candidate_count, h.reason) == ("ambiguous", [], CANDIDATE_CEILING + 1, "ceiling")
    assert (run.status, run.candidates, run.candidate_count) == ("ambiguous", [f"{tmp_path}/two/m1.py::run", f"{tmp_path}/two/m2.py::run"], 2)


def test_codekg_bare_call_cases(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/payment.py": "def charge(card, amount):\n    pass\n",
        "shop/refunds.py": "def charge(card, amount):\n    pass\n",
        "shop/checkout.py": "from shop.payment import charge\n\n\ndef place_order(cart, card):\n    charge(card, cart)\n",  # P1
        "shop/alias.py": "from shop.payment import charge as pay_now\n\n\ndef f(c):\n    pay_now(c, 1)\n",  # P2
        "shop/method.py": "from shop.payment import charge\n\n\nclass Checkout:\n    def run(self, card):\n        charge(card, 1)\n",  # P4
        "billing.py": "def charge():\n    pass\n",
        "billing_legacy.py": "def charge():\n    pass\n",
        "n8.py": "from billing import charge\n\n\ndef pay():\n    charge()\n",  # N8
        "report.py": "import os\n\n\ndef r(parts):\n    \", \".join(parts)\n",  # N2 (receiver calls wait for the next step)
    })
    assert calls[("shop/checkout.py::place_order", "charge")] == ("resolved", "T3", "shop/payment.py::charge", None)
    assert calls[("shop/alias.py::f", "pay_now")] == ("resolved", "T3", "shop/payment.py::charge", None)
    assert calls[("shop/method.py::Checkout.run", "charge")] == ("resolved", "T3", "shop/payment.py::charge", None)
    assert calls[("n8.py::pay", "charge")] == ("resolved", "T3", "billing.py::charge", None)
    assert calls[("report.py::r", "join")] == ("unresolved", None, None, "unknown-receiver")


def test_calls_sorted(tmp_path):
    parser = CodeParser()
    paths = [_write(tmp_path / "b.py", "def g():\n    pass\n\ng()\ng(); g()\n"), _write(tmp_path / "a.py", "len([])\n")]
    calls = resolve([parser.parse_file(p) for p in paths], tmp_path).calls
    keys = [(c.call.file_path, c.call.line, c.call.col, c.call.caller_id) for c in calls]
    assert keys == sorted(keys) and len(keys) == 4


def test_report(tmp_path):
    files = {
        "lib.py": "class Database:\n    pass\n\n\ndef run():\n    pass\n",
        "m2.py": "def other():\n    pass\n",
        "a.py": (
            "import os\nfrom lib import Database, run\nfrom m2 import *\n\n\n"
            "def f(cb):\n    Database()\n    run()\n    other()\n    f(cb)\n    cb()\n    len([])\n    os.getcwd()\n    missing()\n"
        ),
    }
    for rel, code in files.items():
        _write(tmp_path / rel, code)
    parser = CodeParser()
    report = resolve([parser.parse_file(tmp_path / rel) for rel in sorted(files)], tmp_path, use_jedi=False).report()
    assert report == {
        "edge": {"calls": 3, "instantiates": 1},
        "jedi_errors": 0,
        "jedi_unmapped": 0,
        "jedi_version": None,
        "no_resolver": 0,
        "rate": 4 / 6,
        "reason": {"builtin": 1, "local": 1, "no-match": 1, "stdlib": 1},
        "status": {"external": 2, "resolved": 4, "unresolved": 2},
        "tier": {"T2": 1, "T3": 2, "T3*": 1},
        "total": 8,
    }
    assert list(report) == sorted(report)


def test_self_and_cls_calls(tmp_path):
    calls = _calls(tmp_path, {
        "base.py": "class Base:\n    def only_base(self):\n        pass\n",
        "OrderUtils.py": "def price():\n    pass\n",
        "order.py": (
            "from base import Base\n\n\n"
            "class Order(Base):\n"
            "    def total(self):\n        return self.price()\n\n"  # N13: no `price` on Order
            "    def tax(self):\n        return self.rate()\n\n"
            "    def rate(self):\n        self.only_base()\n        self.repo.load()\n\n"
            "    @classmethod\n    def make(cls):\n        return cls.build()\n\n"
            "    @classmethod\n    def build(cls):\n        pass\n\n"
            "    def swap(self, other):\n        self = other\n        self.tax()\n\n"
            "    def later(self):\n        def inner():\n            self.tax()\n        return inner\n\n\n"
            "def free(self):\n    self.tax()\n"
        ),
    })
    assert calls[("order.py::Order.tax", "rate")] == ("resolved", "T1", "order.py::Order.rate", None)
    assert calls[("order.py::Order.make", "build")] == ("resolved", "T1", "order.py::Order.build", None)
    assert calls[("order.py::Order.later.<locals>.inner", "tax")] == ("resolved", "T1", "order.py::Order.tax", None)  # closure over self
    assert calls[("order.py::Order.total", "price")] == ("unresolved", None, None, "no-match")  # N13
    assert calls[("order.py::Order.rate", "only_base")] == ("resolved", "T1", "base.py::Base.only_base", None)  # inherited
    assert calls[("order.py::Order.rate", "load")] == ("unresolved", None, None, "unknown-receiver")  # self.attr: T4b
    assert calls[("order.py::Order.swap", "tax")] == ("unresolved", None, None, "unknown-receiver")  # self rebound
    assert calls[("order.py::free", "tax")] == ("unresolved", None, None, "unknown-receiver")  # not a method


def test_self_calls_in_methods_defined_several_times(tmp_path):
    """@overload stubs and if/else definitions share one scope, so `self` is bound once per definition (as in click's BaseCommand.main)."""
    calls = _calls(tmp_path, {
        "c.py": (
            "import sys\nfrom typing import overload\n\n\n"
            "class C:\n"
            "    @overload\n    def run(self, x: int) -> int: ...\n"
            "    @overload\n    def run(\n        self,\n        x: str,\n    ) -> str: ...\n"
            "    def run(self, x):\n        return self.m()\n\n"
            "    if sys.platform == 'win32':\n        def go(self):\n            self.m()\n"
            "    else:\n        def go(self):\n            self.n()\n\n"
            "    def bad(other, self): ...\n"
            "    def bad(self):\n        self.m()\n\n"
            "    def m(self):\n        pass\n\n"
            "    def n(self):\n        pass\n"
        ),
    })
    assert calls[("c.py::C.run", "m")] == ("resolved", "T1", "c.py::C.m", None)
    assert calls[("c.py::C.go", "m")] == ("resolved", "T1", "c.py::C.m", None)
    assert calls[("c.py::C.go", "n")] == ("resolved", "T1", "c.py::C.n", None)
    assert calls[("c.py::C.bad", "m")] == ("unresolved", None, None, "unknown-receiver")  # self is a second parameter once


def test_class_and_module_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/db.py": "class Database:\n    @classmethod\n    def create(cls):\n        pass\n\n\ndef helper():\n    pass\n",
        "shop/sub.py": "def f():\n    pass\n",
        "shop/rel.py": "from . import sub\n\n\ndef r():\n    sub.f()\n",
        "util.py": "def h():\n    pass\n",
        "src/util.py": "def h():\n    pass\n",
        "t4.py": (
            "from shop.db import Database\n\n\nclass Local:\n    def m(self):\n        pass\n\n\n"
            "def a():\n    Database.create()\n    Local.m(None)\n    Local.missing()\n    Database.create.x()\n"
        ),
        "star.py": "from shop.db import *\n\n\ndef s():\n    Database.create()\n",
        "t5.py": (
            "import shop.db as d\nimport shop.db\nimport util as u\n\n\n"
            "def b():\n    d.helper()\n    d.Database.create()\n    d.missing()\n    d.helper.x()\n"
            "    shop.db.Database()\n    u.h()\n"
        ),
        "deep.py": "import shop.db as d\n\n\ndef c():\n    d.Database.Inner.m()\n",
    })
    assert calls[("t4.py::a", "create")] == ("resolved", "T4", "shop/db.py::Database.create", None)
    assert calls[("t4.py::a", "m")] == ("resolved", "T4", "t4.py::Local.m", None)
    assert calls[("t4.py::a", "missing")] == ("unresolved", None, None, "no-match")
    assert calls[("t4.py::a", "x")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("star.py::s", "create")] == ("resolved", "T4", "shop/db.py::Database.create", None)
    assert calls[("t5.py::b", "helper")] == ("resolved", "T5", "shop/db.py::helper", None)
    assert calls[("t5.py::b", "create")] == ("resolved", "T5", "shop/db.py::Database.create", None)  # mod.C.m()
    assert calls[("t5.py::b", "missing")] == ("unresolved", None, None, "no-match")
    assert calls[("t5.py::b", "x")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("t5.py::b", "Database")] == ("resolved", "T5", "shop/db.py::Database", None)  # `import a.b` binds `a`
    assert calls[("t5.py::b", "h")] == ("ambiguous", None, None, None)  # util is util.py and src/util.py
    assert calls[("shop/rel.py::r", "f")] == ("resolved", "T5", "shop/sub.py::f", None)  # relative `from . import sub`
    assert calls[("deep.py::c", "m")] == ("unresolved", None, None, "unknown-receiver")  # more than one class name left


def test_src_layout_module_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "src/acte/parser.py": "def parse_file(path):\n    pass\n",
        "alias.py": "import acte.parser as p\n\n\ndef f():\n    p.parse_file(1)\n",
        "dotted.py": "import acte.parser\n\n\ndef g():\n    acte.parser.parse_file(2)\n",
    })
    assert calls[("alias.py::f", "parse_file")] == ("resolved", "T5", "src/acte/parser.py::parse_file", None)
    assert calls[("dotted.py::g", "parse_file")] == ("resolved", "T5", "src/acte/parser.py::parse_file", None)  # no acte/__init__.py


# flask's own test fixtures hold a tests/test_apps/.../flask.py, which is tests.apps.inner.flask, not flask
FLASK_LIKE = {
    "flask/__init__.py": "from .app import Flask\n",
    "flask/app.py": "class Flask:\n    pass\n",
    "tests/apps/inner/flask.py": "class Flask:\n    pass\n",
    "tests/test_app.py": "from flask import Flask\n\n\ndef test():\n    Flask()\n",
}


def test_src_is_a_source_root(tmp_path):
    files = {(f"src/{k}" if k.startswith("flask/") else k): v for k, v in FLASK_LIKE.items()}
    assert _calls(tmp_path, files)[("tests/test_app.py::test", "Flask")] == ("resolved", "T3", "src/flask/app.py::Flask", None)
    index = _index(tmp_path, files)
    assert index.modules_for("flask") == [f"{tmp_path}/src/flask/__init__.py"]
    assert index.modules_for("src.flask.app") == index.modules_for("flask.app") == [f"{tmp_path}/src/flask/app.py"]
    assert index.modules_for("inner.flask") == []  # exact names only


def test_flat_layout_has_no_src_names(tmp_path):
    assert _calls(tmp_path, FLASK_LIKE)[("tests/test_app.py::test", "Flask")] == ("resolved", "T3", "flask/app.py::Flask", None)
    assert _index(tmp_path, FLASK_LIKE).src_modules == {}


def test_src_and_root_names_clash(tmp_path):
    index = _index(tmp_path, {"m.py": "", "src/m.py": "", "p/__init__.py": "", "src/p.py": ""})
    assert index.modules_for("m") == [f"{tmp_path}/m.py", f"{tmp_path}/src/m.py"]  # two modules: ambiguous
    assert index.modules_for("p") == [f"{tmp_path}/p/__init__.py"]  # the package wins


def test_src_package_is_not_a_source_root(tmp_path):
    files = {"src/__init__.py": "", "src/utils.py": "def f():\n    pass\n",
             "app.py": "from src.utils import f\n\n\ndef run():\n    f()\n"}
    assert _calls(tmp_path, files)[("app.py::run", "f")] == ("resolved", "T3", "src/utils.py::f", None)
    assert _index(tmp_path, files).src_modules == {}


def test_external_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "utils.py": "def join(items, sep):\n    pass\n\n\ndef get():\n    pass\n",
        "report.py": (
            "import os\nimport requests\nfrom os import path\n\n\n"
            "def a(b, n):\n    os.path.join(b, n)\n\n\n"  # N2
            "def c(parts):\n    \", \".join(parts)\n\n\n"  # N2: a literal receiver
            "def d():\n    requests.get()\n    path.exists()\n    str.join(',', [])\n    dict.fromkeys([])\n"
        ),
        "typed.py": (
            "import copy\nimport requests\nfrom pathlib import Path\n\n\n"
            "def ann(p: Path, s: 'requests.Session'):\n    p.read_text()\n    s.get()\n\n\n"
            "def called(db, n):\n    x = copy.deepcopy(db)\n    x.save()\n    s = requests.Session()\n    s.get()\n"
            "    with open(n) as f:\n        f.read()\n"
        ),
    })
    assert calls[("report.py::a", "join")] == ("external", None, None, "stdlib")
    assert calls[("report.py::c", "join")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("report.py::d", "get")] == ("external", None, None, "third-party")
    assert calls[("report.py::d", "exists")] == ("external", None, None, "stdlib")
    assert calls[("report.py::d", "join")] == ("external", None, None, "builtin")
    assert calls[("report.py::d", "fromkeys")] == ("external", None, None, "builtin")
    # an external type counts only from an annotation; a call to an external name may be a function returning project objects
    assert calls[("typed.py::ann", "read_text")] == ("external", None, None, "stdlib")
    assert calls[("typed.py::ann", "get")] == ("external", None, None, "third-party")  # string annotation
    assert calls[("typed.py::called", "save")] == ("unresolved", None, None, "unknown-receiver")  # copy.deepcopy
    assert calls[("typed.py::called", "get")] == ("unresolved", None, None, "unknown-receiver")  # requests.Session()
    assert calls[("typed.py::called", "read")] == ("unresolved", None, None, "unknown-receiver")  # open is a builtin function


def test_codekg_receiver_cases(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/db.py": "class Database:\n    def connect(self):\n        pass\n\n    def save(self, order):\n        pass\n",
        "shop/net.py": "def connect(host):\n    pass\n",
        "shop/checkout.py": (
            "from shop.db import Database\n\n\n"
            "def place_order(cart, card):\n    db = Database()\n    db.connect()\n"  # N1, P3
        ),
        "store.py": "def save():\n    pass\n",
        "g.py": "def g(x):\n    x.save()\n",  # 6a: one project-level `save`, unknown x
        "files.py": "def read_text(path):\n    pass\n",
        "loader.py": "from pathlib import Path\n\n\ndef load(p):\n    Path(p).read_text()\n",  # N3
        "views.py": "def page(template, ctx):\n    return template.render(ctx)\n",  # N10
        "static/app.js": "function render() {}\n",
    })
    # N1 / P3: Database.connect, never net.connect
    assert calls[("shop/checkout.py::place_order", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)
    assert calls[("g.py::g", "save")] == ("unresolved", None, None, "unknown-receiver")  # D6
    assert calls[("loader.py::load", "read_text")] == ("unresolved", None, None, "unknown-receiver")  # N3: a call can't prove Path is a class
    assert calls[("views.py::page", "render")] == ("unresolved", None, None, "unknown-receiver")


def test_typed_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/db.py": "class Database:\n    def connect(self):\n        pass\n\n\nclass Cache:\n    def connect(self):\n        pass\n",
        "shop/repo.py": "class Repo:\n    def load(self):\n        pass\n",
        "use.py": (
            "import shop.db as m\nfrom shop.db import Database, Cache\nfrom shop.repo import Repo\n\n\n"
            "def p(db: Database):\n    db.connect()\n    db.close()\n\n\n"  # 6c: no `close` on Database
            "def q(db: 'Database'):\n    db.connect()\n\n\n"
            "def r1():\n    Database().connect()\n\n\n"  # R24a
            "def r2():\n    Database().close()\n\n\n"
            "def r3():\n    m.Database(1).connect()\n\n\n"
            "def s():\n    a, b = Repo(), Database()\n    b.connect()\n    a.close()\n\n\n"  # R24c
            "def t():\n    db = Database()\n    db = Cache()\n    db.connect()\n\n\n"
            "def u():\n    db = Database()\n    db = None\n    db.connect()\n\n\n"
            "def v():\n    db = Database()\n    db = make()\n    db.connect()\n\n\n"
            "def w():\n    d = dict()\n    d.get(1)\n    n = max([Database()])\n    n.connect()\n\n\n"
            "class Sub(Database):\n    def connect(self):\n        super().connect()\n        type(self).close()\n\n\n"
            "class Svc:\n    def __init__(self):\n        self.repo = Repo()\n        self.db = Database()\n        self.db = Cache()\n\n"
            "    def run(self):\n        self.repo.load()\n        self.db.connect()\n        self.repo.close()\n"
        ),
        "mod.py": "from shop.db import Database\n\ndb = Database()\n\n\ndef top():\n    db.connect()\n",
    })
    assert calls[("use.py::p", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)
    assert calls[("use.py::p", "close")] == ("unresolved", None, None, "no-match")  # 6c
    assert calls[("use.py::q", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # string annotation
    assert calls[("use.py::r1", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # R24a
    assert calls[("use.py::r2", "close")] == ("unresolved", None, None, "no-match")
    assert calls[("use.py::r3", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # mod.C(...)
    assert calls[("use.py::s", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # R24c
    assert calls[("use.py::s", "close")] == ("unresolved", None, None, "no-match")
    assert calls[("use.py::t", "connect")] == ("unresolved", None, None, "unknown-receiver")  # two classes
    assert calls[("use.py::u", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # None has no connect
    assert calls[("use.py::v", "connect")] == ("unresolved", None, None, "unknown-receiver")  # later untyped make()
    assert calls[("use.py::w", "get")] == ("external", None, None, "builtin")  # dict is a builtin class
    assert calls[("use.py::w", "connect")] == ("unresolved", None, None, "unknown-receiver")  # max is a builtin function
    assert calls[("use.py::Sub.connect", "connect")] == ("resolved", "T1", "shop/db.py::Database.connect", None)  # super()
    assert calls[("use.py::Sub.connect", "close")] == ("unresolved", None, None, "unknown-receiver")  # type(self): a project class
    assert calls[("use.py::Svc.run", "load")] == ("resolved", "T4b", "shop/repo.py::Repo.load", None)
    assert calls[("use.py::Svc.run", "close")] == ("unresolved", None, None, "no-match")
    assert calls[("use.py::Svc.run", "connect")] == ("unresolved", None, None, "unknown-receiver")  # self.db: two classes
    assert calls[("mod.py::top", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # module-level variable


def test_return_annotation_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/db.py": "class Database:\n    def connect(self):\n        pass\n",
        "shop/factory.py": (
            "from shop.db import Database\n\n\n"
            "def get_db() -> Database:\n    pass\n\n\n"
            "def get_names() -> list:\n    pass\n\n\n"
            "def get_any():\n    pass\n\n\n"
            "def get_factory() -> get_db:\n    pass\n"
        ),
        "other.py": "class Database:\n    def connect(self):\n        pass\n",
        "app.py": (
            "from other import Database\nfrom shop.factory import get_db, get_names, get_any, get_factory\n\n\n"
            "def a():\n    db = get_db()\n    db.connect()\n    db.close()\n\n\n"  # Database is shop.db's, not other's
            "def b():\n    get_db().connect()\n\n\n"
            "def c():\n    n = get_names()\n    n.append(1)\n\n\n"
            "def d():\n    x = get_any()\n    x.connect()\n\n\n"
            "def e():\n    f = get_factory()\n    f.connect()\n"
        ),
    })
    assert calls[("app.py::a", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)
    assert calls[("app.py::a", "close")] == ("unresolved", None, None, "no-match")
    assert calls[("app.py::b", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # R24a + R24b
    assert calls[("app.py::c", "append")] == ("unresolved", None, None, "unknown-receiver")  # not a project class
    assert calls[("app.py::d", "connect")] == ("unresolved", None, None, "unknown-receiver")  # no annotation
    assert calls[("app.py::e", "connect")] == ("unresolved", None, None, "unknown-receiver")  # one hop only


JEDI_FILES = {
    "shop/__init__.py": "",
    "shop/models.py": (
        "class A:\n    def m(self):\n        pass\n\n\n"
        "class B:\n    def m(self):\n        pass\n\n\n"
        "def make():\n    return A()\n"
    ),
    "use.py": (
        "import json\nfrom pathlib import Path\n\nfrom shop.models import A, B, make\n\n\n"
        "def union(x: A | B):\n    x.m()\n\n\n"
        "def untyped(x):\n    x.m()\n\n\n"
        "def stdlib(s):\n    x = json.JSONDecoder()\n    x.decode(s)\n\n\n"
        "def made():\n    x = make()\n    x.m()\n\n\n"
        "def wide():\n    x = make()\n    s = \"ééééé✓✓\"; x.m()\n\n\n"  # the called name's byte column is 13 past its character column
        "def last():\n    y = A()\n    y = B()\n    y.m()\n\n\n"
        "def maxed():\n    n = max([A()])\n    n.m()\n\n\n"
        "def literal(parts):\n    \", \".join(parts)\n\n\n"
        "def opened(n):\n    with open(n) as f:\n        f.read()\n\n\n"
        "def pathed(p):\n    Path(p).read_text()\n\n\n"
        "class Sub(A):\n    def m(self):\n        super().m()\n\n\n"
        "class Svc:\n    def __init__(self):\n        self.db = A()\n        self.db = B()\n\n"
        "    def run(self):\n        self.db.m()\n"
    ),
}


def test_jedi_tier(tmp_path):
    jedi = pytest.importorskip("jedi")
    calls = _calls(tmp_path, JEDI_FILES, use_jedi=True)
    assert calls[("use.py::union", "m")] == ("ambiguous", None, None, None)  # 10: A.m and B.m
    assert calls[("use.py::untyped", "m")] == ("unresolved", None, None, "unknown-receiver")  # 10
    assert calls[("use.py::stdlib", "decode")] == ("external", None, None, "jedi-external")  # 10
    assert calls[("use.py::made", "m")] == ("resolved", "TJ", "shop/models.py::A.m", None)  # make() has no annotation
    assert calls[("use.py::wide", "m")] == ("resolved", "TJ", "shop/models.py::A.m", None)
    assert calls[("use.py::last", "m")] == ("resolved", "TJ", "shop/models.py::B.m", None)  # the later assignment wins
    assert calls[("use.py::maxed", "m")] == ("resolved", "TJ", "shop/models.py::A.m", None)
    assert calls[("use.py::Sub.m", "m")] == ("resolved", "T1", "shop/models.py::A.m", None)  # super(): the ladder, before jedi
    assert calls[("use.py::Svc.run", "m")] == ("ambiguous", None, None, None)  # self.db: A or B
    for caller, name in [("literal", "join"), ("opened", "read"), ("pathed", "read_text")]:
        assert calls[(f"use.py::{caller}", name)] == ("external", None, None, "jedi-external")
    resolution = resolve([CodeParser().parse_file(tmp_path / rel) for rel in sorted(JEDI_FILES)], tmp_path)
    two = [f"{tmp_path}/shop/models.py::A.m", f"{tmp_path}/shop/models.py::B.m"]
    assert [r.candidates for r in resolution.calls if r.status == "ambiguous"] == [two, two]  # union, Svc.run
    assert resolution.jedi_version == resolution.report()["jedi_version"] == jedi.__version__


def test_without_jedi_is_the_plain_ladder(tmp_path):
    calls = _calls(tmp_path, JEDI_FILES, use_jedi=False)
    for caller, name in [
        ("union", "m"), ("untyped", "m"), ("stdlib", "decode"), ("made", "m"), ("wide", "m"), ("last", "m"), ("maxed", "m"),
        ("literal", "join"), ("opened", "read"), ("pathed", "read_text"), ("Svc.run", "m"),
    ]:
        assert calls[(f"use.py::{caller}", name)] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("use.py::Sub.m", "m")] == ("resolved", "T1", "shop/models.py::A.m", None)  # super()
    resolution = resolve([CodeParser().parse_file(tmp_path / rel) for rel in sorted(JEDI_FILES)], tmp_path, use_jedi=False)
    assert (resolution.jedi_version, resolution.jedi_unmapped, resolution.jedi_errors) == (None, 0, 0)


def test_jedi_errors_are_counted(tmp_path, monkeypatch):
    jedi = pytest.importorskip("jedi")
    plain = _calls(tmp_path, JEDI_FILES)

    def fail(*args, **kwargs):
        raise RuntimeError("jedi failed")

    monkeypatch.setattr(jedi.Script, "goto", fail)
    resolution = resolve([CodeParser().parse_file(tmp_path / rel) for rel in sorted(JEDI_FILES)], tmp_path)
    assert _calls(tmp_path, JEDI_FILES, use_jedi=True) == plain  # every call keeps its ladder result
    asked = sum(r.reason == "unknown-receiver" for r in resolution.calls)
    assert resolution.report()["jedi_errors"] == asked == 11


def test_jedi_definition_outside_the_parsed_files_is_external(tmp_path):
    """Code under the root that was not parsed (a `.venv`, vendored packages) is not the project."""
    pytest.importorskip("jedi")
    (tmp_path / "fakepkg").mkdir()
    (tmp_path / "fakepkg" / "__init__.py").write_text("class Thing:\n    def go(self):\n        pass\n")  # importable, not parsed
    files = {"app.py": "from fakepkg import Thing\n\n\ndef run():\n    [Thing()][0].go()\n"}
    assert _calls(tmp_path, files, use_jedi=True)[("app.py::run", "go")] == ("external", None, None, "jedi-external")
    resolution = resolve([CodeParser().parse_file(tmp_path / "app.py")], tmp_path)
    assert resolution.jedi_unmapped == 0


def test_receiver_imported_from_a_module_variable(tmp_path):
    calls = _calls(tmp_path, {
        "pkg/__init__.py": "from .impl import smth\n",  # an instance re-exported through a package
        "pkg/impl.py": "class Smth:\n    def func(self):\n        pass\n\n\nsmth = Smth()\n",
        "cfg.py": (
            "class A:\n    def load(self):\n        pass\n\n\nclass B:\n    def load(self):\n        pass\n\n\n"
            "settings = A()\nplain = []\n"
        ),
        "two.py": "from cfg import A, B\n\nmixed = A()\nmixed = B()\n",
        "main.py": (
            "from cfg import plain, settings\nfrom pkg import smth\nfrom two import mixed\n\n\n"
            "def via_package():\n    smth.func()\n\n\n"
            "def direct():\n    settings.load()\n\n\n"
            "def missing():\n    settings.save()\n\n\n"
            "def two_types():\n    mixed.load()\n\n\n"
            "def untyped():\n    plain.load()\n"
        ),
    })
    assert calls[("main.py::via_package", "func")] == ("resolved", "T4b", "pkg/impl.py::Smth.func", None)
    assert calls[("main.py::direct", "load")] == ("resolved", "T4b", "cfg.py::A.load", None)
    assert calls[("main.py::missing", "save")] == ("unresolved", None, None, "no-match")
    assert calls[("main.py::two_types", "load")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("main.py::untyped", "load")] == ("unresolved", None, None, "unknown-receiver")


def test_imported_variable_cycle_terminates(tmp_path):
    calls = _calls(tmp_path, {
        "a.py": "from b import x\n\n\ndef f():\n    x.go()\n",
        "b.py": "from a import x\n",
    })
    assert calls[("a.py::f", "go")] == ("unresolved", None, None, "unknown-receiver")


def test_stdlib_import_is_not_a_same_named_subpackage(tmp_path):
    calls = _calls(tmp_path, {
        "pkg/__init__.py": "",
        "pkg/json/__init__.py": "def loads(s):\n    pass\n",
        "pkg/provider.py": "import json\n\n\ndef f(s):\n    return json.loads(s)\n",  # the stdlib's json, as in flask
    })
    assert calls[("pkg/provider.py::f", "loads")] == ("external", None, None, "stdlib")


def test_inherited_methods(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/base.py": "class Base:\n    def save(self):\n        pass\n\n    @classmethod\n    def make(cls):\n        pass\n",
        "use.py": (
            "import shop.base as sb\nfrom shop.base import Base\n\n\n"
            "class Model(Base):\n"
            "    def run(self):\n        self.save()\n        self.load()\n\n"
            "    @classmethod\n    def build(cls):\n        cls.make()\n\n\n"
            "class Dotted(sb.Base):\n    def save(self):\n        pass\n\n    def run(self):\n        self.save()\n        self.make()\n\n\n"
            "def typed(m: Model):\n    m.save()\n\n\n"
            "def assigned():\n    x = Model()\n    x.make()\n\n\n"
            "def named():\n    Model.make()\n"
        ),
        "other.py": "from use import Model\n\n\ndef named():\n    Model.make()\n",
    })
    assert calls[("use.py::Model.run", "save")] == ("resolved", "T1", "shop/base.py::Base.save", None)
    assert calls[("use.py::Model.run", "load")] == ("unresolved", None, None, "no-match")  # only project classes in the MRO
    assert calls[("use.py::Model.build", "make")] == ("resolved", "T1", "shop/base.py::Base.make", None)  # cls
    assert calls[("use.py::Dotted.run", "save")] == ("resolved", "T1", "use.py::Dotted.save", None)  # own method first
    assert calls[("use.py::Dotted.run", "make")] == ("resolved", "T1", "shop/base.py::Base.make", None)  # `sb.Base`
    assert calls[("use.py::typed", "save")] == ("resolved", "T4b", "shop/base.py::Base.save", None)
    assert calls[("use.py::assigned", "make")] == ("resolved", "T4b", "shop/base.py::Base.make", None)
    assert calls[("use.py::named", "make")] == ("resolved", "T4", "shop/base.py::Base.make", None)
    assert calls[("other.py::named", "make")] == ("resolved", "T4", "shop/base.py::Base.make", None)  # an imported subclass


def test_diamond_follows_c3(tmp_path):
    calls = _calls(tmp_path, {
        "d.py": (
            "class A:\n    def m(self):\n        pass\n\n    def n(self):\n        pass\n\n\n"
            "class B(A):\n    def m(self):\n        pass\n\n\n"
            "class C(A):\n    def m(self):\n        pass\n\n    def n(self):\n        pass\n\n\n"
            "class D(B, C):\n    def go(self):\n        self.m()\n        self.n()\n"
        ),
    })
    assert calls[("d.py::D.go", "m")] == ("resolved", "T1", "d.py::B.m", None)
    assert calls[("d.py::D.go", "n")] == ("resolved", "T1", "d.py::C.n", None)  # C before A, not depth-first


def test_external_and_unknown_bases(tmp_path):
    files = {
        "e.py": (
            "from typing import Generic, TypeVar\n\nT = TypeVar('T')\n\n\n"
            "class Mixin:\n    def mix(self):\n        pass\n\n    def get(self):\n        pass\n\n\n"
            "class D(dict):\n    def own(self):\n        pass\n\n    def go(self):\n        self.own()\n        self.keys()\n\n\n"
            "class Late(dict, Mixin):\n    def go(self):\n        self.mix()\n\n\n"
            "class Early(Mixin, dict):\n    def go(self):\n        self.get()\n        self.keys()\n\n\n"
            "class G(Generic[T]):\n    def go(self):\n        self.mix()\n\n\n"
            "class U(Missing, make()):\n    def go(self):\n        self.mix()\n"
        ),
    }
    calls = _calls(tmp_path, files)
    assert calls[("e.py::D.go", "own")] == ("resolved", "T1", "e.py::D.own", None)  # own methods still resolve
    assert calls[("e.py::D.go", "keys")] == ("unresolved", None, None, "unknown-base")
    assert calls[("e.py::Late.go", "mix")] == ("unresolved", None, None, "unknown-base")  # dict comes first in the MRO
    assert calls[("e.py::Early.go", "get")] == ("resolved", "T1", "e.py::Mixin.get", None)  # Mixin comes before dict
    assert calls[("e.py::Early.go", "keys")] == ("unresolved", None, None, "unknown-base")
    assert calls[("e.py::G.go", "mix")] == ("unresolved", None, None, "unknown-base")
    index = resolve([CodeParser().parse_file(tmp_path / "e.py")], tmp_path, use_jedi=False).index
    bases = {k.split("::")[1]: v for k, v in index.bases.items()}
    assert bases == {
        "Mixin": [], "D": ["external"], "Late": ["external", f"{tmp_path}/e.py::Mixin"],
        "Early": [f"{tmp_path}/e.py::Mixin", "external"], "G": ["external"], "U": ["unknown", "unknown"],
    }


def test_super_calls(tmp_path):
    calls = _calls(tmp_path, {
        "s.py": (
            "class A:\n    def m(self):\n        pass\n\n\n"
            "class B(A):\n    def m(self):\n        super().m()\n\n    def two(self):\n        super(B, self).m()\n\n"
            "    def none(self):\n        super().n()\n\n"
            "    @classmethod\n    def k(cls):\n        super().m()\n\n"
            "    def nested(self):\n        def f():\n            super().m()\n        return f\n\n"
            "    def other(self):\n        super(A, self).m()\n\n\n"
            "class C(A, dict):\n    def m(self):\n        super().m()\n\n    def k(self):\n        super().keys()\n\n\n"
            "def free(self):\n    super().m()\n"
        ),
        "t.py": "from s import A\n\n\ndef super():\n    pass\n\n\nclass B(A):\n    def m(self):\n        super().m()\n",
    })
    for caller in ("B.m", "B.two", "B.k", "C.m"):
        assert calls[(f"s.py::{caller}", "m")] == ("resolved", "T1", "s.py::A.m", None), caller
    assert calls[("s.py::B.none", "n")] == ("unresolved", None, None, "no-match")
    assert calls[("s.py::C.k", "keys")] == ("unresolved", None, None, "unknown-base")
    for caller in ("B.nested.<locals>.f", "free"):  # zero-argument super() in a nested function; no class
        assert calls[(f"s.py::{caller}", "m")] == ("unresolved", None, None, "unknown-receiver"), caller
    assert calls[("s.py::B.other", "m")] == ("unresolved", None, None, "no-match")  # super(A, self) in B: nothing after A
    assert calls[("t.py::B.m", "m")] == ("unresolved", None, None, "unknown-receiver")  # super is not the builtin


def test_super_with_explicit_class_in_a_closure(tmp_path):
    calls = _calls(tmp_path, {
        "s.py": (
            "class A:\n    def m(self):\n        pass\n\n\n"
            "class B(A):\n    def m(self):\n        def f():\n            super(B, self).m()\n        return f\n"
        ),
    })
    assert calls[("s.py::B.m.<locals>.f", "m")] == ("resolved", "T1", "s.py::A.m", None)


def test_none_takes_no_part_in_the_type_unless_none_has_the_method(tmp_path):
    calls = _calls(tmp_path, {
        # django/utils/translation/trans_real.py:265: self._catalog is None at first, a TranslationCatalog later
        "trans.py": (
            "import gettext as gettext_module\n\n\n"
            "class TranslationCatalog:\n    def update(self, trans):\n        pass\n\n\n"
            "class DjangoTranslation(gettext_module.GNUTranslations):\n"
            "    def __init__(self):\n        self._catalog = None\n\n"
            "    def merge(self, other):\n        if self._catalog is None:\n            self._catalog = TranslationCatalog()\n"
            "        else:\n            self._catalog.update(other)\n\n"
            "    def same(self, other):\n        return self._catalog.__eq__(other)\n"
        ),
        "use.py": (
            "from trans import TranslationCatalog\n\n\n"
            "def f(flag):\n    cat = None\n    if flag:\n        cat = TranslationCatalog()\n    cat.update(1)\n    cat.__bool__()\n\n\n"
            "def g():\n    cat = None\n    cat.update(1)\n"
        ),
    })
    assert calls[("trans.py::DjangoTranslation.merge", "update")] == ("resolved", "T4b", "trans.py::TranslationCatalog.update", None)
    assert calls[("use.py::f", "update")] == ("resolved", "T4b", "trans.py::TranslationCatalog.update", None)
    # None has __eq__ and __bool__ too
    assert calls[("trans.py::DjangoTranslation.same", "__eq__")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("use.py::f", "__bool__")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("use.py::g", "update")] == ("unresolved", None, None, "unknown-receiver")  # only ever None


def test_super_with_a_base_class_or_another_object(tmp_path):
    calls = _calls(tmp_path, {
        # django/forms/fields.py:374: FloatField.to_python skips IntegerField.to_python
        "fields.py": (
            "class Field:\n    def to_python(self, value):\n        return value\n\n\n"
            "class IntegerField(Field):\n    def to_python(self, value):\n        return int(value)\n\n\n"
            "class FloatField(IntegerField):\n    def to_python(self, value):\n"
            "        value = super(IntegerField, self).to_python(value)\n        return float(value)\n\n"
            "    def clean(self, value):\n        return super(FloatField, self).to_python(value)\n\n"
            "    def other(self, value):\n        return super(Unrelated, self).to_python(value)\n\n\n"
            "class Unrelated:\n    def to_python(self, value):\n        pass\n"
        ),
        # django/db/models/aggregates.py:136: super(Aggregate, c) on a copy of self
        "agg.py": (
            "class Func:\n    def get_source_expressions(self):\n        return []\n\n\n"
            "class Aggregate(Func):\n    def get_source_expressions(self):\n        return []\n\n"
            "    def resolve_expression(self):\n        c = self.copy()\n        return super(Aggregate, c).get_source_expressions()\n\n"
            "    def by_name(self, klass):\n        return super(klass, self).copy()\n"
        ),
    })
    assert calls[("fields.py::FloatField.to_python", "to_python")] == ("resolved", "T1", "fields.py::Field.to_python", None)
    assert calls[("fields.py::FloatField.clean", "to_python")] == ("resolved", "T1", "fields.py::IntegerField.to_python", None)
    # not in FloatField's MRO: a TypeError at run time
    assert calls[("fields.py::FloatField.other", "to_python")] == ("unresolved", None, None, "unknown-receiver")
    assert calls[("agg.py::Aggregate.resolve_expression", "get_source_expressions")] == (
        "resolved", "T1", "agg.py::Func.get_source_expressions", None)
    assert calls[("agg.py::Aggregate.by_name", "copy")] == ("unresolved", None, None, "unknown-receiver")  # klass is a variable


def test_bad_class_graphs_are_unknown(tmp_path):
    calls = _calls(tmp_path, {
        "bad.py": (
            "class A(B):\n    def go(self):\n        self.x()\n        self.go()\n\n\n"
            "class B(A):\n    def x(self):\n        pass\n\n\n"
            "class S(S):\n    def go(self):\n        self.y()\n\n\n"
            "class P:\n    def p(self):\n        pass\n\n\n"
            "class Q(P):\n    pass\n\n\n"
            "class X(P, Q):\n    def go(self):\n        self.p()\n\n\n"  # no consistent MRO: P must come before and after Q
            "class Twice(P, P):\n    def go(self):\n        self.p()\n"
        ),
    })
    assert calls[("bad.py::A.go", "x")] == ("unresolved", None, None, "unknown-base")  # A and B inherit from each other
    assert calls[("bad.py::A.go", "go")] == ("resolved", "T1", "bad.py::A.go", None)
    assert calls[("bad.py::S.go", "y")] == ("unresolved", None, None, "unknown-base")
    assert calls[("bad.py::X.go", "p")] == ("unresolved", None, None, "unknown-base")
    assert calls[("bad.py::Twice.go", "p")] == ("unresolved", None, None, "unknown-base")


def test_builtin_object_is_no_base(tmp_path):
    files = {
        "o.py": (
            "class P:\n    def p(self):\n        pass\n\n\n"
            "class A(object):\n    def go(self):\n        self.missing()\n\n\n"
            "class B(P, object):\n    def go(self):\n        self.p()\n"
        ),
    }
    calls = _calls(tmp_path, files)
    assert calls[("o.py::A.go", "missing")] == ("unresolved", None, None, "no-match")  # as for `class A:`
    assert calls[("o.py::B.go", "p")] == ("resolved", "T1", "o.py::P.p", None)
    index = resolve([CodeParser().parse_file(tmp_path / "o.py")], tmp_path, use_jedi=False).index
    assert {k.split("::")[1]: v for k, v in index.bases.items()} == {"P": [], "A": [], "B": [f"{tmp_path}/o.py::P"]}
