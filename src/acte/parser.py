"""
Multi-language codebase parser using Tree-sitter.
Extracts structural nodes (classes, functions) and edges (calls,contains, inheritance).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Set

import tree_sitter_language_pack as tslp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models for extracted entities
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    kind: str  # 'File', 'Class', 'Function', 'Type'
    name: str
    file_path: str
    line_start: int
    line_end: int
    language: str = ""
    parent_name: Optional[str] = None
    params: Optional[str] = None
    return_type: Optional[str] = None
    is_test: bool = False
    extra: dict = field(default_factory=dict)

@dataclass
class EdgeInfo:
    kind: str  # 'CALLS', 'IMPORTS_FROM', 'INHERITS', 'CONTAINS'
    source: str
    target: str
    file_path: str
    line: int = 0
    extra: dict = field(default_factory=dict)

# ---------------------------------------------------------------------------
# Language Syntax Mappings
# ---------------------------------------------------------------------------

EXTENSION_TO_LANGUAGE: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
}

_CLASS_TYPES: Dict[str, List[str]] = {
    "python": ["class_definition"],
    "javascript": ["class_declaration", "class"],
    "typescript": ["class_declaration", "class"],
    "java": ["class_declaration", "interface_declaration", "enum_declaration"],
}

_FUNCTION_TYPES: Dict[str, List[str]] = {
    "python": ["function_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "java": ["method_declaration", "constructor_declaration"],
}

_CALL_TYPES: Dict[str, List[str]] = {
    "python": ["call"],
    "javascript": ["call_expression", "new_expression"],
    "typescript": ["call_expression", "new_expression"],
    "java": ["method_invocation", "object_creation_expression"],
}

_IMPORT_TYPES: Dict[str, List[str]] = {
    "python": ["import_statement", "import_from_statement"],
    "javascript": ["import_statement"],
    "typescript": ["import_statement"],
    "java": ["import_declaration"],
}
