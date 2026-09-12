
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
    
    assert any(e.kind == "CALLS" and "router" in e.source and e.target == "db_connect" for e in result.edges), "Failed to trace call edge."

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
    assert any(e.kind == "CALLS" and e.source == cs.caller_id and e.target == "g" for e in result.edges)

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
        ("a.py::C.m", "f", None, 2),
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
        ("a.py::load", "b", None, 3),
        ("a.py::load", "c", None, 4),
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
