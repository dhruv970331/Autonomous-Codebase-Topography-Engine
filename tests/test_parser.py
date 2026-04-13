import pytest
from pathlib import Path
from acte.parser import CodeParser, NodeInfo, EdgeInfo

@pytest.fixture
def parser():
    return CodeParser()

def test_class_function_and_containment(parser, tmp_path):
    code = b"class AuthManager:\n    def verify_token(self):\n        pass\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    nodes, edges = parser.parse_file(p)
    
    assert any(n.kind == "Class" and n.name == "AuthManager" for n in nodes), "Failed to extract class."
    assert any(n.kind == "Function" and n.name == "verify_token" for n in nodes), "Failed to extract function."
    assert any(e.kind == "CONTAINS" and "AuthManager" in e.source and "verify_token" in e.target for e in edges), "Failed to map containment edge."

def test_call_extraction(parser, tmp_path):
    code = b"def router():\n    db_connect()\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    _, edges = parser.parse_file(p)
    
    assert any(e.kind == "CALLS" and "router" in e.source and e.target == "db_connect" for e in edges), "Failed to trace call edge."

def test_import_extraction(parser, tmp_path):
    code = b"import os\nfrom typing import List\n"
    p = tmp_path / "test.py"
    p.write_bytes(code)
    _, edges = parser.parse_file(p)
    
    assert any(e.kind == "IMPORTS_FROM" and e.target == "os" for e in edges), "Failed to extract standard import."
    assert any(e.kind == "IMPORTS_FROM" and e.target == "typing" for e in edges), "Failed to extract 'from' import."

def test_typescript_support(parser, tmp_path):
    code = b"class UserService { constructor() {} }\n"
    p = tmp_path / "test.ts"
    p.write_bytes(code)
    nodes, _ = parser.parse_file(p)
    
    assert any(n.kind == "Class" and n.name == "UserService" for n in nodes), "Failed to parse TypeScript."

def test_unsupported_extension(parser, tmp_path):
    p = tmp_path / "test.rust"
    p.write_bytes(b"fn main() {}")
    nodes, edges = parser.parse_file(p)
    
    assert len(nodes) == 0, "Unsupported extension should return empty node list."
    assert len(edges) == 0, "Unsupported extension should return empty edge list."

def test_malformed_syntax(parser, tmp_path):
    code = b"def broken_function(:\n    pass" 
    p = tmp_path / "test.py"
    p.write_bytes(code)
    nodes, _ = parser.parse_file(p)
    
    # Tree-sitter relies on fault-tolerant error recovery. It must not crash.
    assert len(nodes) >= 1, "Parser crashed on malformed syntax instead of recovering."
    assert nodes[0].kind == "File", "File node missing after syntax error."

def test_empty_file(parser, tmp_path):
    p = tmp_path / "test.py"
    p.write_bytes(b"")
    nodes, edges = parser.parse_file(p)
    
    assert len(nodes) == 1, "Empty file must still generate a base File node."
    assert nodes[0].kind == "File"
    assert len(edges) == 0, "Empty file must generate zero edges."