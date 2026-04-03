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

# ---------------------------------------------------------------------------
# Parser Engine
# ---------------------------------------------------------------------------

class CodeParser:
    """Parses source files using Tree-sitter and extracts structural topography."""

    def __init__(self) -> None:
        self._parsers: Dict[str, object] = {}

    def _get_parser(self, language: str):
        if language not in self._parsers:
            try:
                self._parsers[language] = tslp.get_parser(language)
            except (LookupError, ValueError, ImportError) as exc:
                logger.debug("Tree-sitter parser unavailable for %s: %s", language, exc)
                return None
        return self._parsers[language]

    def detect_language(self, path: Path) -> Optional[str]:
        return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())

    def parse_file(self, path: Path) -> tuple[List[NodeInfo], List[EdgeInfo]]:
        try:
            source = path.read_bytes()
        except (OSError, PermissionError):
            return [], []

        language = self.detect_language(path)
        if not language:
            return [], []

        parser = self._get_parser(language)
        if not parser:
            return [], []

        tree = parser.parse(source)
        nodes: List[NodeInfo] = []
        edges: List[EdgeInfo] = []
        file_path_str = str(path)

        # Base File Node
        nodes.append(NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language=language,
        ))

        self._extract_from_tree(
            tree.root_node, source, language, file_path_str, nodes, edges
        )

        return nodes, edges

    def _extract_from_tree(
        self,
        root,
        source: bytes,
        language: str,
        file_path: str,
        nodes: List[NodeInfo],
        edges: List[EdgeInfo],
        enclosing_class: Optional[str] = None,
        enclosing_func: Optional[str] = None,
        _depth: int = 0,
    ) -> None:
        pass