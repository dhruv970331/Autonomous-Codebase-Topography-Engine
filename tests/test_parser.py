
import pytest

from acte.parser import CodeParser


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
