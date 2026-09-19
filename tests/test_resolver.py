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


def _calls(root: Path, files: dict[str, str]) -> dict[tuple[str, str], tuple]:
    """(caller relative to root, called name) -> (status, tier, target relative to root, reason), from the real pipeline."""
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)
    parser = CodeParser()
    resolution = resolve([parser.parse_file(root / rel) for rel in sorted(files)], root)
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
    resolution = resolve([CodeParser().parse_file(f) for f in sorted(tmp_path.glob("*.py"))], tmp_path)
    star_after = next(r for r in resolution.calls if r.call.file_path.endswith("star_after.py"))
    assert star_after.candidates == [f"{tmp_path}/lib.py::run", f"{tmp_path}/m2.py::run"]


def test_ambiguous_candidates_and_ceiling(tmp_path):
    files = {f"d{i}/util.py": "def h():\n    pass\n" for i in range(CANDIDATE_CEILING + 1)}
    files["two/m1.py"] = files["two/m2.py"] = "def run():\n    pass\n"
    files["a.py"] = "from util import h\nfrom m1 import *\nfrom m2 import *\n\n\ndef f():\n    h()\n    run()\n"
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
    report = resolve([parser.parse_file(tmp_path / rel) for rel in sorted(files)], tmp_path).report()
    assert report == {
        "edge": {"calls": 3, "instantiates": 1},
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
    assert calls[("order.py::Order.rate", "only_base")] == ("unresolved", None, None, "no-match")  # no inheritance yet
    assert calls[("order.py::Order.rate", "load")] == ("unresolved", None, None, "unknown-receiver")  # self.attr: T4b
    assert calls[("order.py::Order.swap", "tax")] == ("unresolved", None, None, "unknown-receiver")  # self rebound
    assert calls[("order.py::free", "tax")] == ("unresolved", None, None, "unknown-receiver")  # not a method


def test_class_and_module_receivers(tmp_path):
    calls = _calls(tmp_path, {
        "shop/__init__.py": "",
        "shop/db.py": "class Database:\n    @classmethod\n    def create(cls):\n        pass\n\n\ndef helper():\n    pass\n",
        "shop/sub.py": "def f():\n    pass\n",
        "shop/rel.py": "from . import sub\n\n\ndef r():\n    sub.f()\n",
        "x/util.py": "def h():\n    pass\n",
        "y/util.py": "def h():\n    pass\n",
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
    assert calls[("t5.py::b", "h")] == ("ambiguous", None, None, None)  # util matches two files
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
    assert calls[("use.py::u", "connect")] == ("unresolved", None, None, "unknown-receiver")  # later None
    assert calls[("use.py::v", "connect")] == ("unresolved", None, None, "unknown-receiver")  # later untyped make()
    assert calls[("use.py::w", "get")] == ("external", None, None, "builtin")  # dict is a builtin class
    assert calls[("use.py::w", "connect")] == ("unresolved", None, None, "unknown-receiver")  # max is a builtin function
    assert calls[("use.py::Sub.connect", "connect")] == ("unresolved", None, None, "unknown-receiver")  # super(): a project base
    assert calls[("use.py::Sub.connect", "close")] == ("unresolved", None, None, "unknown-receiver")  # type(self): a project class
    assert calls[("use.py::Svc.run", "load")] == ("resolved", "T4b", "shop/repo.py::Repo.load", None)
    assert calls[("use.py::Svc.run", "close")] == ("unresolved", None, None, "no-match")
    assert calls[("use.py::Svc.run", "connect")] == ("unresolved", None, None, "unknown-receiver")  # self.db: two classes
    assert calls[("mod.py::top", "connect")] == ("resolved", "T4b", "shop/db.py::Database.connect", None)  # module-level variable
