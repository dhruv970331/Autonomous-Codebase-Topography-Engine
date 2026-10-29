
from pathlib import Path

import pytest

from acte.parser import CodeParser
from acte.rdf_builder import KnowledgeGraphBuilder


@pytest.fixture
def parser():
    return CodeParser()

def test_class_function_and_containment(parser, tmp_path):
    code = b"class AuthManager:\n    def verify_token(self):\n        pass\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)
    
    assert any(n.kind == "Class" and n.name == "AuthManager" for n in result.nodes), "Failed to extract class."
    assert any(n.kind == "Function" and n.name == "verify_token" for n in result.nodes), "Failed to extract function."
    assert any(e.kind == "CONTAINS" and "AuthManager" in e.source and "verify_token" in e.target for e in result.edges), "Failed to map containment edge."

def test_call_extraction(parser, tmp_path):
    code = b"def router():\n    db_connect()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)
    
    assert any(cs.caller_id == f"{p}::router" and cs.target_name == "db_connect" for cs in result.call_sites), "Failed to trace call."
    assert not any(e.kind == "CALLS" for e in result.edges), "Calls are CallSites now, not bare-name edges."

def test_import_extraction(parser, tmp_path):
    code = b"import os\nfrom typing import List\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)
    
    assert any(e.kind == "IMPORTS_FROM" and e.target == "os" for e in result.edges), "Failed to extract standard import."
    assert any(e.kind == "IMPORTS_FROM" and e.target == "typing" for e in result.edges), "Failed to extract 'from' import."

def test_typescript_support(parser, tmp_path):
    code = b"class UserService { constructor() {} }\n"
    p = tmp_path / "test.ts"
    p.write_bytes(code)
    result = parser.parse_file(p)
    
    assert any(n.kind == "Class" and n.name == "UserService" for n in result.nodes), "Failed to parse TypeScript."

def test_unsupported_extension(parser, tmp_path):
    p = tmp_path / "test.rust"
    p.write_bytes(b"fn main() {}")
    result = parser.parse_file(p)
    
    assert len(result.nodes) == 0, "Unsupported extension should return empty node list."
    assert len(result.edges) == 0, "Unsupported extension should return empty edge list."

def test_malformed_syntax(parser, tmp_path):
    code = b"def broken_function(:\n    pass" 
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)
    
    # Tree-sitter relies on fault-tolerant error recovery. It must not crash.
    assert len(result.nodes) >= 1, "Parser crashed on malformed syntax instead of recovering."
    assert result.nodes[0].kind == "File", "File node missing after syntax error."

def test_empty_file(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"")
    result = parser.parse_file(p)
    
    assert len(result.nodes) == 1, "Empty file must still generate a base File node."
    assert result.nodes[0].kind == "File"
    assert len(result.edges) == 0, "Empty file must generate zero edges."

def test_call_site_bare_call_has_no_receiver(parser, tmp_path):
    code = b"def router():\n    foo()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert any(cs.target_name == "foo" and cs.receiver is None for cs in result.call_sites), "Bare call should have receiver=None."

def test_call_site_method_call_records_receiver(parser, tmp_path):
    code = b"class AuthManager:\n    def login(self):\n        self.helper()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert any(cs.receiver == "self" and cs.target_name == "helper" for cs in result.call_sites), "self.helper() should record receiver='self' and target_name='helper'."

def test_call_site_module_call_records_receiver(parser, tmp_path):
    code = b"def connect_all():\n    db.connect()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert any(cs.receiver == "db" and cs.target_name == "connect" for cs in result.call_sites), "db.connect() should record receiver='db'."

def test_call_site_caller_id_matches_qualified_method_id(parser, tmp_path):
    code = b"class AuthManager:\n    def login(self):\n        self.helper()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    qualified_login_id = next(e.target for e in result.edges if e.kind == "CONTAINS" and e.target.endswith("AuthManager.login"))
    call_site = next(cs for cs in result.call_sites if cs.target_name == "helper")
    assert call_site.caller_id == qualified_login_id, "CallSite.caller_id must equal the qualified id the parser gives the enclosing method."

def test_call_site_col_bare_call(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def router():\n    foo()\n")
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "foo")
    assert (cs.line, cs.col) == (2, 4)

def test_call_site_col_is_the_method_name(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def f():\n    a.b.m()\n")
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "m")
    assert (cs.line, cs.col) == (2, 8), "col must point at 'm', not at the start of 'a.b.m()'."

def test_call_site_multiline_chain_uses_name_position(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def f():\n    return (x\n        .m())\n")
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "m")
    assert (cs.line, cs.col) == (3, 9), "line and col must come from 'm', not from where the call starts."

def test_call_site_two_calls_on_one_line(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def f():\n    foo(); bar()\n")
    result = parser.parse_file(p)

    cols = {cs.target_name: (cs.line, cs.col) for cs in result.call_sites}
    assert cols["foo"] == (2, 4)
    assert cols["bar"] == (2, 11)

def test_call_site_multiline_receiver_has_no_whitespace(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def f():\n    return (x\n        .m())\n")
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "m")
    assert cs.receiver == "x"

def test_call_site_multiline_chained_receiver_is_compacted(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"def f():\n    return (a\n    .b\n    .m())\n")
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "m")
    assert cs.receiver == "a.b"

def test_call_site_starred_in_bare_tuple(parser, tmp_path):
    # tree-sitter parses an unparenthesised `*g()` as a call of `*g`, and `*o.m()` with the star on `o`
    p = tmp_path / "test.py"
    p.write_bytes(b"def f(o):\n    yield *g(), 1\n    x = *g(), 2\n    for y in *g(), 3:\n        pass\n"
                  b"    print(*g())\n    return *o.a.m(), x, y\n")
    result = parser.parse_file(p)

    assert [(cs.target_name, cs.receiver, cs.line, cs.col) for cs in result.call_sites] == [
        ("g", None, 2, 11), ("g", None, 3, 9), ("g", None, 4, 14),
        ("print", None, 6, 4), ("g", None, 6, 11), ("m", "o.a", 7, 16)]

def test_import_binding_plain(parser, tmp_path):
    code = b"import calc\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert len(result.import_bindings) == 1
    b = result.import_bindings[0]
    assert b.local_name == "calc"
    assert b.imported_name == "calc"
    assert b.module == "calc"
    assert b.is_module_alias is True
    assert b.file_path == str(p)
    assert b.line == 1

def test_import_binding_aliased(parser, tmp_path):
    code = b"import calc as c\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert len(result.import_bindings) == 1
    b = result.import_bindings[0]
    assert b.local_name == "c"
    assert b.imported_name == "calc"
    assert b.module == "calc"
    assert b.is_module_alias is True
    assert b.file_path == str(p)
    assert b.line == 1

def test_import_binding_from_import(parser, tmp_path):
    code = b"from calc import add\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert len(result.import_bindings) == 1
    b = result.import_bindings[0]
    assert b.local_name == "add"
    assert b.imported_name == "add"
    assert b.module == "calc"
    assert b.is_module_alias is False
    assert b.file_path == str(p)
    assert b.line == 1

def test_import_binding_from_import_aliased(parser, tmp_path):
    code = b"from calc import add as plus\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert len(result.import_bindings) == 1
    b = result.import_bindings[0]
    assert b.local_name == "plus"
    assert b.imported_name == "add"
    assert b.module == "calc"
    assert b.is_module_alias is False
    assert b.file_path == str(p)
    assert b.line == 1

def test_import_binding_dotted_module(parser, tmp_path):
    code = b"import pkg.calc\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    assert len(result.import_bindings) == 1
    b = result.import_bindings[0]
    assert b.local_name == "pkg.calc"
    assert b.imported_name == "pkg.calc"
    assert b.module == "pkg.calc"
    assert b.is_module_alias is True
    assert b.file_path == str(p)
    assert b.line == 1
def test_relative_import_edge_targets_the_module(parser, tmp_path):
    code = b"from . import sibling\nfrom .mod import thing\nfrom ..pkg.deep import other\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    result = parser.parse_file(p)

    targets = {e.target for e in result.edges if e.kind == "IMPORTS_FROM"}
    assert targets == {".", ".mod", "..pkg.deep"}, f"Relative import edges point at the wrong node: {targets}"
    assert "sibling" not in targets, "Edge points at the imported name instead of the module."

    # The edge and the binding must agree on what the module is.
    for binding in result.import_bindings:
        assert binding.module in targets, f"Binding module {binding.module!r} has no matching edge."

_NESTED_SHAPES = b"""\
def f():
    class Inner:
        y = inner_setup()
        def m(self):
            pass

class C:
    x = setup()
    def m(self):
        def helper():
            pass

def outer():
    def helper():
        g()

def helper():
    pass

class A:
    class B:
        pass
"""

def test_nested_definitions_get_qualname_ids(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(_NESTED_SHAPES)
    result = parser.parse_file(p)

    expected = {
        "f", "f.<locals>.Inner", "f.<locals>.Inner.m",
        "C", "C.m", "C.m.<locals>.helper",
        "outer", "outer.<locals>.helper",
        "helper",
        "A", "A.B",
    }
    contained = {e.target for e in result.edges if e.kind == "CONTAINS"}
    assert contained == {f"{p}::{q}" for q in expected}
    # The builder derives node ids from NodeInfo; they must be the same strings.
    builder = KnowledgeGraphBuilder()
    assert {builder._node_id(n) for n in result.nodes if n.kind != "File"} == contained
    # A nested definition is contained by its immediate enclosing definition.
    contains = {(e.source, e.target) for e in result.edges if e.kind == "CONTAINS"}
    assert (f"{p}::outer", f"{p}::outer.<locals>.helper") in contains
    assert (f"{p}::f.<locals>.Inner", f"{p}::f.<locals>.Inner.m") in contains

def test_call_in_nested_function_has_nested_caller_id(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(_NESTED_SHAPES)
    result = parser.parse_file(p)

    cs = next(cs for cs in result.call_sites if cs.target_name == "g")
    assert cs.caller_id == f"{p}::outer.<locals>.helper"

def test_call_in_class_body_is_attributed_to_enclosing_function_or_file(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(_NESTED_SHAPES)
    result = parser.parse_file(p)

    callers = {cs.target_name: cs.caller_id for cs in result.call_sites}
    assert callers["inner_setup"] == f"{p}::f"
    assert callers["setup"] == str(p)

def test_import_binding_scope_ids(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(
        b"import os\n"
        b"def load():\n    import json\n"
        b"def outer():\n    def helper():\n        from calc import add\n"
    )
    result = parser.parse_file(p)

    scopes = {b.local_name: b.scope_id for b in result.import_bindings}
    assert scopes == {
        "os": str(p),
        "json": f"{p}::load",
        "add": f"{p}::outer.<locals>.helper",
    }

def test_import_binding_in_class_body_is_scoped_to_the_class(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(
        b"class C:\n    import os\n"
        b"def f():\n    class Inner:\n        from calc import add\n"
    )
    result = parser.parse_file(p)

    scopes = {b.local_name: b.scope_id for b in result.import_bindings}
    assert scopes == {"os": f"{p}::C", "add": f"{p}::f.<locals>.Inner"}

def test_star_imports(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_bytes(b"from .db import *\nfrom pkg.sub import *\nfrom calc import add\nclass C:\n    from m import *\n")
    result = parser.parse_file(p)

    assert [(b.local_name, b.imported_name, b.module, b.is_module_alias, b.line, b.scope_id) for b in result.star_imports] == [
        ("*", "*", ".db", False, 1, str(p)),
        ("*", "*", "pkg.sub", False, 2, str(p)),
        ("*", "*", "m", False, 5, f"{p}::C"),
    ]
    assert [b.local_name for b in result.import_bindings] == ["add"]

def _bindings(parser, tmp_path, code, kind, name="a.py"):
    p = tmp_path / name
    p.write_text(code)
    result = parser.parse_file(p)
    return [(b.scope_id.replace(str(p), "a.py"), b.name, b.type_name, b.line) for b in result.local_bindings if b.kind == kind]

def test_local_binding_param(parser, tmp_path):
    code = (
        "class C:\n"
        "    def m(self, a, b: Database, c=1, d: db.Pool = None, *args: int, e, f: Optional[X] = None, **kw):\n"
        "        pass\n"
    )
    assert _bindings(parser, tmp_path, code, "param") == [
        ("a.py::C.m", "self", None, 2),
        ("a.py::C.m", "a", None, 2),
        ("a.py::C.m", "b", "Database", 2),
        ("a.py::C.m", "c", None, 2),
        ("a.py::C.m", "d", "db.Pool", 2),
        ("a.py::C.m", "args", None, 2),
        ("a.py::C.m", "e", None, 2),
        ("a.py::C.m", "f", "X", 2),
        ("a.py::C.m", "kw", None, 2),
    ]

def test_local_binding_assign(parser, tmp_path):
    code = (
        "x = Database()\n"
        "y = db.Database()\n"
        "z = get_db()\n"
        "a, b = A(), B()\n"
        "c, d = pair()\n"
        "e = f = C()\n"
        "g += 1\n"
        "h, *rest = A(), B()\n"
        "obj.attr = C()\n"
        "items[0] = C()\n"
        "u = make().build()\n"
    )
    assert _bindings(parser, tmp_path, code, "assign") == [
        ("a.py", "x", "Database", 1),
        ("a.py", "y", "db.Database", 2),
        ("a.py", "z", "get_db", 3),
        ("a.py", "a", "A", 4),
        ("a.py", "b", "B", 4),
        ("a.py", "c", None, 5),
        ("a.py", "d", None, 5),
        ("a.py", "e", "C", 6),
        ("a.py", "f", "C", 6),
        ("a.py", "g", None, 7),
        ("a.py", "h", None, 8),
        ("a.py", "rest", None, 8),
        ("a.py", "u", None, 11),
    ]

def test_local_binding_none_values(parser, tmp_path):
    code = "a = None\nb = c = None\nd, e = None, X()\nf = X()\nclass C:\n    def m(self):\n        self.g = None\n        self.h = 'None'\n"
    p = tmp_path / "a.py"
    p.write_text(code)
    assert [(b.name, b.is_none) for b in parser.parse_file(p).local_bindings if b.kind in ("assign", "attr")] == [
        ("a", True), ("b", True), ("c", True), ("d", True), ("e", False), ("f", False), ("self.g", True), ("self.h", False),
    ]

def test_local_binding_literal_types(parser, tmp_path):
    code = "a = {}\nb = []\nc = ()\nd = {1}\ne = 's'\nf = b'x'\ng = 1\nh = 1.5\ni = True\nj = 'a' 'b'\nk = rb'x'\nl = -1\nm = X()\n"
    p = tmp_path / "a.py"
    p.write_text(code)
    assert [(b.name, b.literal) for b in parser.parse_file(p).local_bindings] == [
        ("a", "dict"), ("b", "list"), ("c", "tuple"), ("d", "set"), ("e", "str"), ("f", "bytes"), ("g", "int"),
        ("h", "float"), ("i", "bool"), ("j", "str"), ("k", "bytes"), ("l", None), ("m", None),
    ]

def test_local_binding_scopes(parser, tmp_path):
    code = "x = A()\nclass C:\n    y = B()\n    def m(self):\n        z = D()\n"
    assert _bindings(parser, tmp_path, code, "assign") == [
        ("a.py", "x", "A", 1),
        ("a.py::C", "y", "B", 3),
        ("a.py::C.m", "z", "D", 5),
    ]

def test_local_binding_annotation(parser, tmp_path):
    code = (
        "def load():\n"
        "    a: Database = connect()\n"
        "    b: Optional[Database] = None\n"
        "    c: Database | None = None\n"
        "    d: m.Pool\n"
    )
    assert _bindings(parser, tmp_path, code, "annotation") == [
        ("a.py::load", "a", "Database", 2),
        ("a.py::load", "b", "Database", 3),
        ("a.py::load", "c", "Database", 4),
        ("a.py::load", "d", "m.Pool", 5),
    ]

def test_local_binding_with(parser, tmp_path):
    code = "def run():\n    with Database() as db, open(p) as (x, y):\n        pass\n"
    assert _bindings(parser, tmp_path, code, "with") == [
        ("a.py::run", "db", "Database", 2),
        ("a.py::run", "x", None, 2),
        ("a.py::run", "y", None, 2),
    ]

def test_local_binding_for(parser, tmp_path):
    code = "for i in range(3):\n    pass\nfor k, (v, w) in items:\n    pass\n"
    assert _bindings(parser, tmp_path, code, "for") == [
        ("a.py", "i", None, 1),
        ("a.py", "k", None, 3),
        ("a.py", "v", None, 3),
        ("a.py", "w", None, 3),
    ]

def test_local_binding_self_attr_belongs_to_the_class(parser, tmp_path):
    code = (
        "class C:\n"
        "    def __init__(self):\n"
        "        self.repo = Repo()\n"
        "        self.pool: db.Pool = make()\n"
        "        self.count += 1\n"
        "        other.x = Repo()\n"
        "        def helper():\n"
        "            self.y = Y()\n"
        "def f(self):\n"
        "    self.z = Z()\n"
    )
    assert _bindings(parser, tmp_path, code, "attr") == [
        ("a.py::C", "self.repo", "Repo", 3),
        ("a.py::C", "self.pool", "db.Pool", 4),
        ("a.py::C", "self.count", None, 5),
    ]

def test_local_bindings_python_only(parser, tmp_path):
    p = tmp_path / "a.js"
    p.write_text("function f(a) { let x = new A(); for (const i of xs) {} }\n")
    assert parser.parse_file(p).local_bindings == []

def test_local_binding_except(parser, tmp_path):
    code = (
        "try:\n    pass\n"
        "except E as e:\n    pass\n"
        "except m.E as d:\n    pass\n"
        "except (A, B) as t:\n    pass\n"
        "except ValueError:\n    pass\n"
    )
    assert _bindings(parser, tmp_path, code, "except") == [
        ("a.py", "e", "E", 3),
        ("a.py", "d", "m.E", 5),
        ("a.py", "t", None, 7),
    ]

def test_local_binding_except_group_is_not_typed(parser, tmp_path):
    code = "try:\n    pass\nexcept* E as eg:\n    pass\n"
    assert _bindings(parser, tmp_path, code, "except") == [("a.py", "eg", None, 3)]

def test_local_binding_walrus(parser, tmp_path):
    code = "def f():\n    if (db := Database()):\n        pass\n    if (n := len(xs)) > 1:\n        pass\n    if (y := x):\n        pass\n"
    assert _bindings(parser, tmp_path, code, "walrus") == [
        ("a.py::f", "db", "Database", 2),
        ("a.py::f", "n", "len", 4),
        ("a.py::f", "y", None, 6),
    ]

def test_local_binding_match(parser, tmp_path):
    code = (
        "match v:\n"
        "    case Point(x=a, y=0):\n        pass\n"
        "    case [b, *rest]:\n        pass\n"
        '    case {"k": kv, **others}:\n        pass\n'
        "    case m.C() as c:\n        pass\n"
        "    case [1, 2] as s:\n        pass\n"
        "    case Color.RED:\n        pass\n"
        "    case [_, *_]:\n        pass\n"
        "    case _:\n        pass\n"
        "    case bare:\n        pass\n"
    )
    assert _bindings(parser, tmp_path, code, "match") == [
        ("a.py", "a", None, 2),
        ("a.py", "b", None, 4),
        ("a.py", "rest", None, 4),
        ("a.py", "kv", None, 6),
        ("a.py", "others", None, 6),
        ("a.py", "c", "m.C", 8),
        ("a.py", "s", None, 10),
        ("a.py", "bare", None, 18),
    ]

def test_local_binding_comprehension(parser, tmp_path):
    code = "def f():\n    [x for x in xs]\n    {k: v for k, v in d}\n    (g for g in gs)\n    {s for s in ss}\n"
    assert _bindings(parser, tmp_path, code, "comprehension") == [
        ("a.py::f", "x", None, 2),
        ("a.py::f", "k", None, 3),
        ("a.py::f", "v", None, 3),
        ("a.py::f", "g", None, 4),
        ("a.py::f", "s", None, 5),
    ]

def test_local_binding_lambda(parser, tmp_path):
    code = "def f():\n    return lambda a, b=1, *c, d, **e: a\n"
    assert _bindings(parser, tmp_path, code, "lambda") == [
        ("a.py::f", "a", None, 2),
        ("a.py::f", "b", None, 2),
        ("a.py::f", "c", None, 2),
        ("a.py::f", "d", None, 2),
        ("a.py::f", "e", None, 2),
    ]

def test_local_binding_global_and_nonlocal(parser, tmp_path):
    code = "def outer():\n    global g1, g2\n    def inner():\n        nonlocal n\n"
    assert _bindings(parser, tmp_path, code, "global") == [
        ("a.py::outer", "g1", None, 2),
        ("a.py::outer", "g2", None, 2),
    ]
    assert _bindings(parser, tmp_path, code, "nonlocal") == [("a.py::outer.<locals>.inner", "n", None, 4)]

def test_local_binding_string_annotations(parser, tmp_path):
    code = 'def f(p: "m.Database", q: "Optional[X]"):\n    a: "Database" = connect()\n    b: "None"\n'
    assert _bindings(parser, tmp_path, code, "param") == [
        ("a.py::f", "p", "m.Database", 1),
        ("a.py::f", "q", "X", 1),
    ]
    assert _bindings(parser, tmp_path, code, "annotation") == [
        ("a.py::f", "a", "Database", 2),
        ("a.py::f", "b", None, 3),
    ]

def test_optional_annotation_is_its_one_type(parser, tmp_path):
    forms = [
        "X | None", "None | X", "Optional[X]", "typing.Optional[m.X]",
        '"X | None"', '"None | X"', '"Optional[X]"', '"typing.Optional[m.X]"',
        "X | int", "X | Y | None", "Union[X, None]", "Optional[X | int]", '"X | int"',
    ]
    code = "def f():\n" + "".join(f"    v{i}: {t}\n" for i, t in enumerate(forms))
    assert [b[2] for b in _bindings(parser, tmp_path, code, "annotation")] == [
        "X", "X", "X", "m.X",
        "X", "X", "X", "m.X",
        None, None, None, None, None,
    ]

def test_local_binding_def_and_class(parser, tmp_path):
    code = (
        "def outer():\n"
        "    def helper():\n        pass\n"
        "    class Inner:\n        pass\n"
        "class C:\n"
        "    def m(self):\n        pass\n"
    )
    p = tmp_path / "a.py"
    p.write_text(code)
    result = parser.parse_file(p)
    records = [(b.scope_id.replace(str(p), "a.py"), b.name, b.kind, b.type_name, b.line) for b in result.local_bindings if b.kind in ("def", "class")]
    assert records == [
        ("a.py", "outer", "def", None, 1),
        ("a.py::outer", "helper", "def", None, 2),
        ("a.py::outer", "Inner", "class", None, 4),
        ("a.py", "C", "class", None, 6),
        ("a.py::C", "m", "def", None, 7),
    ]

def test_local_binding_del(parser, tmp_path):
    code = (
        "class C:\n"
        "    def m(self):\n"
        "        del x\n"
        "        del a, (b, [c])\n"
        "        del self.attr, items[0]\n"
    )
    assert _bindings(parser, tmp_path, code, "del") == [
        ("a.py::C.m", "x", None, 3),
        ("a.py::C.m", "a", None, 4),
        ("a.py::C.m", "b", None, 4),
        ("a.py::C.m", "c", None, 4),
    ]
    assert _bindings(parser, tmp_path, code, "attr") == []

def test_function_return_type(parser, tmp_path):
    code = (
        "def a() -> Database:\n    pass\n"
        "def b() -> \"m.Database\":\n    pass\n"
        "def c() -> Optional[X]:\n    pass\n"
        "def d():\n    pass\n"
        "class K:\n    async def e(self) -> m.Repo:\n        pass\n"
    )
    p = tmp_path / "a.py"
    p.write_text(code)
    result = parser.parse_file(p)
    assert [(n.name, n.return_type) for n in result.nodes if n.kind != "File"] == [
        ("a", "Database"), ("b", "m.Database"), ("c", "X"), ("d", None), ("K", None), ("e", "m.Repo"),
    ]

def test_class_bases(parser, tmp_path):
    code = (
        "class A:\n    pass\n"
        "class B():\n    pass\n"
        "class C(A, m.B, Generic[T], typing.List[int], f(), *bs, (A), metaclass=M, **kw):\n    pass\n"
        "class D[T](\n    A,  # first\n    pkg.mod.B,\n):\n    def m(self):\n        class E(D):\n            pass\n"
    )
    p = tmp_path / "a.py"
    p.write_text(code)
    result = parser.parse_file(p)
    assert [(n.name, n.bases) for n in result.nodes if n.kind != "File"] == [
        ("A", []), ("B", []), ("C", ["A", "m.B", "Generic", "typing.List", "?", "?", "?"]),
        ("D", ["A", "pkg.mod.B"]), ("m", None), ("E", ["D"]),
    ]

def test_class_bases_python_only(parser, tmp_path):
    p = tmp_path / "a.ts"
    p.write_text("class A extends B {}\n")
    assert [n.bases for n in parser.parse_file(p).nodes if n.kind == "Class"] == [None]

DECORATED = (
    "import functools\n"                 # 1
    "\n"
    "@functools.cache\n"                 # 3
    "def f():\n"
    "    def inner():\n"                 # 5: not decorated itself
    "        pass\n"
    "\n"
    "@dataclass\n"                       # 8
    "# a comment between the decorators\n"
    "@total_ordering(\n"
    "    key=1,\n"
    ")\n"
    "class C:\n"                         # 13
    "    @property\n"                    # 14
    "    @abstractmethod\n"
    "    def p(self):\n"                 # 16
    "        return 1\n"
    "\n"
    "    def q(self):\n"                 # 19
    "        pass\n"
)


def test_decorator_line(parser, tmp_path):
    p = tmp_path / "a.py"
    p.write_text(DECORATED)
    result = parser.parse_file(p)
    assert [(n.qualified_id.split("::")[1], n.line_start, n.line_end, n.decorator_line) for n in result.nodes if n.kind != "File"] == [
        ("f", 4, 6, 3), ("f.<locals>.inner", 5, 6, None), ("C", 13, 20, 8), ("C.p", 16, 17, 14), ("C.q", 19, 20, None),
    ]


def test_decorator_line_python_only(parser, tmp_path):
    p = tmp_path / "a.ts"
    p.write_text("@Component\nclass A {\n  @Input()\n  m() {}\n}\n")
    assert all(n.decorator_line is None for n in parser.parse_file(p).nodes)


TSX_APP = """\
import React, { useState } from "react";
import type { Props } from './types';
import * as utils from "../utils";
import "./styles.css";

interface ButtonProps {
  label: string;
}

export function Button({ label }: ButtonProps) {
  const [n, setN] = useState(0);
  return <button>{label}</button>;
}

class Counter extends React.Component<Props> {
  render() {
    return <Button label={String(this.count)} />;
  }
}

export default function App() {
  return (
    <main>
      <Counter />
    </main>
  );
}
"""


def _structure(result):
    """(kind, id, first line, last line) of each Class and Function, and every edge."""
    defs = [(n.kind, n.qualified_id, n.line_start, n.line_end) for n in result.nodes if n.kind != "File"]
    edges = [(e.kind, e.source, e.target, e.line) for e in result.edges]
    return defs, edges


def test_tsx_structure_and_imports(parser, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("app.tsx").write_text(TSX_APP)
    result = parser.parse_file(Path("app.tsx"))

    assert (result.nodes[0].kind, result.nodes[0].language) == ("File", "tsx")
    defs, edges = _structure(result)
    assert defs == [
        ("Function", "app.tsx::Button", 10, 13),
        ("Class", "app.tsx::Counter", 15, 19),
        ("Function", "app.tsx::Counter.render", 16, 18),
        ("Function", "app.tsx::App", 21, 27),
    ]
    assert edges == [
        ("IMPORTS_FROM", "app.tsx", "react", 1),
        ("IMPORTS_FROM", "app.tsx", "./types", 2),
        ("IMPORTS_FROM", "app.tsx", "../utils", 3),
        ("IMPORTS_FROM", "app.tsx", "./styles.css", 4),
        ("CONTAINS", "app.tsx", "app.tsx::Button", 10),
        ("CONTAINS", "app.tsx", "app.tsx::Counter", 15),
        ("CONTAINS", "app.tsx::Counter", "app.tsx::Counter.render", 16),
        ("CONTAINS", "app.tsx", "app.tsx::App", 21),
    ]
    assert [(c.caller_id, c.target_name, c.receiver) for c in result.call_sites] == [
        ("app.tsx::Button", "useState", None), ("app.tsx::Counter.render", "String", None),
    ]


FUNCTION_VALUES = """\
const Card = (props) => {
  return format(props);
};
let load = function () {
  items.forEach((x) => save(x));
  const helper = async () => fetch();
};
[1].map(y => track(y));
const { a } = () => pick();
const handlers = { click: () => onClick() };
"""


@pytest.mark.parametrize("suffix", [".js", ".ts", ".tsx"])
def test_function_values_and_their_calls(parser, tmp_path, monkeypatch, suffix):
    monkeypatch.chdir(tmp_path)
    f = f"f{suffix}"
    Path(f).write_text(FUNCTION_VALUES)
    defs, edges = _structure(parser.parse_file(Path(f)))

    # a function value named by a plain variable is a Function; a parameter (`y => ...`) never names one
    assert defs == [
        ("Function", f"{f}::Card", 1, 3),
        ("Function", f"{f}::load", 4, 7),
        ("Function", f"{f}::load.<locals>.helper", 6, 6),
    ]
    assert edges == [
        ("CONTAINS", f, f"{f}::Card", 1),
        ("CONTAINS", f, f"{f}::load", 4),
        ("CONTAINS", f"{f}::load", f"{f}::load.<locals>.helper", 6),
    ]
    # calls inside any other function value count for the enclosing function, or the File
    assert [(c.caller_id, c.target_name) for c in parser.parse_file(Path(f)).call_sites] == [
        (f"{f}::Card", "format"),
        (f"{f}::load", "forEach"), (f"{f}::load", "save"), (f"{f}::load.<locals>.helper", "fetch"),
        (f, "map"), (f, "track"), (f, "pick"), (f, "onClick"),
    ]


GO_SHAPES = """\
package shapes

import "fmt"

import (
\t"math"
\tstr "strings"
)

type Point struct {
\tX, Y float64
}

type Shape interface {
\tArea() float64
}

type Celsius float64

func (p *Point) Scale(f float64) {
\tp.X = p.X * math.Abs(f)
}

func (p Point) String() string {
\treturn fmt.Sprintf("%v", str.TrimSpace("x"))
}

func (c Celsius) String() string {
\treturn format(c)
}

func New(x float64) *Point {
\treturn &Point{X: x}
}

type Alias = Point

type Pair[T any] struct {
\tA, B T
}

func (p *Pair[T]) Swap() {
\tp.A, p.B = swap(p.B, p.A)
}
"""


def test_go_structure_and_imports(parser, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("shapes.go").write_text(GO_SHAPES)
    Path("area.go").write_text("package shapes\n\nfunc (p Point) Area() float64 {\n\treturn 0\n}\n\n"
                               "func (p ((*Point))) Draw() {\n\tdraw(p)\n}\n")
    result = parser.parse_file(Path("shapes.go"))

    assert (result.nodes[0].kind, result.nodes[0].language) == ("File", "go")
    defs, edges = _structure(result)
    # every named type is a Class, `type Celsius float64` too; the alias `type Alias = Point` is not
    assert defs == [
        ("Class", "shapes.go::Point", 10, 12),
        ("Class", "shapes.go::Shape", 14, 16),
        ("Class", "shapes.go::Celsius", 18, 18),
        ("Function", "shapes.go::Point.Scale", 20, 22),
        ("Function", "shapes.go::Point.String", 24, 26),
        ("Function", "shapes.go::Celsius.String", 28, 30),
        ("Function", "shapes.go::New", 32, 34),
        ("Class", "shapes.go::Pair", 38, 40),
        ("Function", "shapes.go::Pair.Swap", 42, 44),
    ]
    assert edges == [
        ("IMPORTS_FROM", "shapes.go", "fmt", 3),
        ("IMPORTS_FROM", "shapes.go", "math", 6),
        ("IMPORTS_FROM", "shapes.go", "strings", 7),
        ("CONTAINS", "shapes.go", "shapes.go::Point", 10),
        ("CONTAINS", "shapes.go", "shapes.go::Shape", 14),
        ("CONTAINS", "shapes.go", "shapes.go::Celsius", 18),
        ("CONTAINS", "shapes.go", "shapes.go::Point.Scale", 20),
        ("CONTAINS", "shapes.go", "shapes.go::Point.String", 24),
        ("CONTAINS", "shapes.go", "shapes.go::Celsius.String", 28),
        ("CONTAINS", "shapes.go", "shapes.go::New", 32),
        ("CONTAINS", "shapes.go", "shapes.go::Pair", 38),
        ("CONTAINS", "shapes.go", "shapes.go::Pair.Swap", 42),
    ]
    assert [(c.caller_id, c.target_name, c.receiver, c.line) for c in result.call_sites] == [
        ("shapes.go::Point.Scale", "Abs", "math", 21),
        ("shapes.go::Point.String", "Sprintf", "fmt", 25),
        ("shapes.go::Point.String", "TrimSpace", "str", 25),
        ("shapes.go::Celsius.String", "format", None, 29),
        ("shapes.go::Pair.Swap", "swap", None, 43),  # a generic receiver loses its type arguments
    ]

    # a method on a type declared in another file of the package is contained by its own File
    # a parenthesized receiver type is unwrapped: `((*Point))` is `Point`
    area = parser.parse_file(Path("area.go"))
    defs, edges = _structure(area)
    assert defs == [("Function", "area.go::Point.Area", 3, 5), ("Function", "area.go::Point.Draw", 7, 9)]
    assert edges == [("CONTAINS", "area.go", "area.go::Point.Area", 3), ("CONTAINS", "area.go", "area.go::Point.Draw", 7)]
    assert [(c.caller_id, c.target_name) for c in area.call_sites] == [("area.go::Point.Draw", "draw")]
