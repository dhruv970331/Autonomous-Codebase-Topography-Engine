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
        if _depth > 180:  # Prevent recursion crashes on generated/minified code
            return

        class_types = set(_CLASS_TYPES.get(language, []))
        func_types = set(_FUNCTION_TYPES.get(language, []))
        call_types = set(_CALL_TYPES.get(language, []))
        import_types = set(_IMPORT_TYPES.get(language, []))

        for child in root.children:
            node_type = child.type

            # Extract Classes
            if node_type in class_types:
                name = self._get_name(child, language)
                if name:
                    qualified = self._qualify(name, file_path, enclosing_class)
                    nodes.append(NodeInfo(
                        kind="Class",
                        name=name,
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=language,
                        parent_name=enclosing_class,
                    ))
                    edges.append(EdgeInfo(
                        kind="CONTAINS",
                        source=file_path if not enclosing_class else self._qualify(enclosing_class, file_path, None),
                        target=qualified,
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    ))
                    # Recurse into class body
                    self._extract_from_tree(
                        child, source, language, file_path, nodes, edges,
                        enclosing_class=name, enclosing_func=None, _depth=_depth + 1
                    )
                continue

            # Extract Functions
            if node_type in func_types:
                name = self._get_name(child, language)
                if name:
                    qualified = self._qualify(name, file_path, enclosing_class)
                    nodes.append(NodeInfo(
                        kind="Function",
                        name=name,
                        file_path=file_path,
                        line_start=child.start_point[0] + 1,
                        line_end=child.end_point[0] + 1,
                        language=language,
                        parent_name=enclosing_class,
                    ))
                    container = self._qualify(enclosing_class, file_path, None) if enclosing_class else file_path
                    edges.append(EdgeInfo(
                        kind="CONTAINS",
                        source=container,
                        target=qualified,
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    ))
                    # Recurse into function body
                    self._extract_from_tree(
                        child, source, language, file_path, nodes, edges,
                        enclosing_class=enclosing_class, enclosing_func=name, _depth=_depth + 1
                    )
                continue

            # Extract Calls
            if node_type in call_types:
                call_name = self._get_call_name(child, language)
                if call_name:
                    caller = self._qualify(enclosing_func, file_path, enclosing_class) if enclosing_func else file_path
                    edges.append(EdgeInfo(
                        kind="CALLS",
                        source=caller,
                        target=call_name, # Bare target; resolution to qualified names happens later
                        file_path=file_path,
                        line=child.start_point[0] + 1,
                    ))

            # Extract Imports
            if node_type in import_types:
                edges.append(EdgeInfo(
                    kind="IMPORTS_FROM",
                    source=file_path,
                    target=child.text.decode("utf-8", errors="replace"), 
                    file_path=file_path,
                    line=child.start_point[0] + 1,
                ))

            # Standard Recursion
            self._extract_from_tree(
                child, source, language, file_path, nodes, edges,
                enclosing_class=enclosing_class, enclosing_func=enclosing_func, _depth=_depth + 1
            )

    def _qualify(self, name: str, file_path: str, enclosing_class: Optional[str]) -> str:
        """Generates a globally unique identifier for a node."""
        if enclosing_class:
            return f"{file_path}::{enclosing_class}.{name}"
        return f"{file_path}::{name}"

    def _get_name(self, node, language: str) -> Optional[str]:
        """Extracts the identifier name from a definition node."""
        for child in node.children:
            if child.type in ("identifier", "name", "type_identifier", "property_identifier", "field_identifier"):
                return child.text.decode("utf-8", errors="replace")
        return None

    def _get_call_name(self, node, language: str) -> Optional[str]:
        """Extracts the target identifier from a call expression."""
        if not node.children:
            return None
        first = node.children[0]
        
        if first.type in ("identifier", "simple_identifier"):
            return first.text.decode("utf-8", errors="replace")
            
        if first.type in ("attribute", "member_expression"):
            for child in reversed(first.children):
                if child.type in ("identifier", "property_identifier"):
                    return child.text.decode("utf-8", errors="replace")
        return None
