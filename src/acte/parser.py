"""
Multi-language codebase parser using Tree-sitter.
Extracts structural nodes (classes, functions) and edges (calls,contains, inheritance).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict

import tree_sitter_language_pack as tslp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models for extracted entities
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    kind: str
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
    kind: str
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
    ".go": "go",
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
# Tree-sitter Version-Proof Helpers
#
# tree_sitter_language_pack (tslp) ships its own native Rust-backed Node class
# with different attribute names than the standard tree_sitter Python bindings:
#
#   Standard tree_sitter  │  tslp (Rust-native, all callable)
#   ──────────────────────┼──────────────────────────────────
#   node.type  (property) │  node.kind()
#   node.children (list)  │  node.child_count() + node.child(i)
#   node.start_point      │  node.start_position() → Point(.row, .column)
#   node.end_point        │  node.end_position()   → Point(.row, .column)
#   node.text  (bytes)    │  (absent; use start_byte()/end_byte() instead)
#
# Every helper below tries the standard name first, then the tslp name.
# ---------------------------------------------------------------------------

def _get_children(node):
    """Return all children of *node*, compatible with both tree-sitter APIs."""
    if not node:
        return []

    # Standard tree-sitter: node.children is a list (not callable).
    if hasattr(node, "children"):
        c = getattr(node, "children")
        if callable(c):
            c = c()
        if c is not None:
            return c

    # tslp / legacy tree-sitter: child_count + child(i).
    if hasattr(node, "child_count"):
        count = getattr(node, "child_count")
        if callable(count):
            count = count()
        if isinstance(count, int):
            children = []
            get_child = getattr(node, "child")
            for i in range(count):
                child = get_child(i)
                if callable(child):
                    child = child()
                if child is not None:
                    children.append(child)
            return children

    return []


def _get_type(node) -> str:
    """Return the grammar type string for *node*.

    Standard tree_sitter exposes this as ``node.type`` (a property).
    tslp exposes it as ``node.kind()`` (a callable).
    """
    if not node:
        return ""
    for attr in ("type", "kind"):
        t = getattr(node, attr, None)
        if t is None:
            continue
        if callable(t):
            t = t()
        if t is not None:
            return str(t)
    return ""


def _get_start_line(node) -> int:
    """Return the 1-based start line for *node*.

    Standard tree_sitter: ``node.start_point`` → ``(row, col)`` tuple.
    tslp: ``node.start_position()`` → ``Point`` object with ``.row``.
    """
    if not node:
        return 1
    for attr in ("start_point", "start_position"):
        pt = getattr(node, attr, None)
        if pt is None:
            continue
        if callable(pt):
            pt = pt()
        if pt is None:
            continue
        if isinstance(pt, tuple):
            return pt[0] + 1
        row = getattr(pt, "row", None)
        if row is not None:
            return int(row) + 1
    return 1


def _get_end_line(node) -> int:
    """Return the 1-based end line for *node*.

    Standard tree_sitter: ``node.end_point`` → ``(row, col)`` tuple.
    tslp: ``node.end_position()`` → ``Point`` object with ``.row``.
    """
    if not node:
        return 1
    for attr in ("end_point", "end_position"):
        pt = getattr(node, attr, None)
        if pt is None:
            continue
        if callable(pt):
            pt = pt()
        if pt is None:
            continue
        if isinstance(pt, tuple):
            return pt[0] + 1
        row = getattr(pt, "row", None)
        if row is not None:
            return int(row) + 1
    return 1


def _safe_text(node, source: bytes) -> str:
    """Extract text for *node*, compatible with both tree-sitter APIs.

    Standard tree_sitter exposes ``node.text`` (bytes property).
    tslp omits it; we fall back to slicing *source* with ``start_byte``/``end_byte``.
    """
    if not node:
        return ""
    # Standard tree_sitter: node.text is a bytes property.
    if hasattr(node, "text"):
        t = getattr(node, "text")
        if callable(t):
            t = t()
        if isinstance(t, bytes):
            return t.decode("utf-8", errors="replace")
        if isinstance(t, str):
            return t
    # tslp / fallback: reconstruct from byte offsets.
    if hasattr(node, "start_byte") and hasattr(node, "end_byte"):
        start = getattr(node, "start_byte")
        if callable(start):
            start = start()
        end = getattr(node, "end_byte")
        if callable(end):
            end = end()
        if isinstance(start, int) and isinstance(end, int):
            return source[start:end].decode("utf-8", errors="replace")
    return ""

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
            except Exception as exc:
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

        try:
            tree = parser.parse(source)
        except TypeError:
            tree = parser.parse(source.decode("utf-8", errors="replace"))

        nodes: List[NodeInfo] = []
        edges: List[EdgeInfo] = []
        file_path_str = str(path)

        nodes.append(NodeInfo(
            kind="File",
            name=file_path_str,
            file_path=file_path_str,
            line_start=1,
            line_end=source.count(b"\n") + 1,
            language=language,
        ))

        root = getattr(tree, "root_node", None)
        if callable(root):
            root = root()

        if root:
            self._extract_from_tree(root, source, language, file_path_str, nodes, edges)

        return nodes, edges

    def _extract_from_tree(self, root, source, language, file_path, nodes, edges, enclosing_class=None, enclosing_func=None, _depth=0):
        if _depth > 180 or not root:
            return

        class_types = set(_CLASS_TYPES.get(language, []))
        func_types = set(_FUNCTION_TYPES.get(language, []))
        call_types = set(_CALL_TYPES.get(language, []))
        import_types = set(_IMPORT_TYPES.get(language, []))

        for child in _get_children(root):
            node_type = _get_type(child)

            if node_type in class_types:
                name = self._get_name(child, source)
                if name:
                    qualified = self._qualify(name, file_path, enclosing_class)
                    nodes.append(NodeInfo("Class", name, file_path, _get_start_line(child), _get_end_line(child), language, enclosing_class))
                    edges.append(EdgeInfo("CONTAINS", file_path if not enclosing_class else self._qualify(enclosing_class, file_path, None), qualified, file_path, _get_start_line(child)))
                    self._extract_from_tree(child, source, language, file_path, nodes, edges, name, None, _depth + 1)
                continue

            if node_type in func_types:
                name = self._get_name(child, source)
                if name:
                    qualified = self._qualify(name, file_path, enclosing_class)
                    nodes.append(NodeInfo("Function", name, file_path, _get_start_line(child), _get_end_line(child), language, enclosing_class))
                    container = self._qualify(enclosing_class, file_path, None) if enclosing_class else file_path
                    edges.append(EdgeInfo("CONTAINS", container, qualified, file_path, _get_start_line(child)))
                    self._extract_from_tree(child, source, language, file_path, nodes, edges, enclosing_class, name, _depth + 1)
                continue

            if node_type in call_types:
                call_name = self._get_call_name(child, source)
                if call_name:
                    caller = self._qualify(enclosing_func, file_path, enclosing_class) if enclosing_func else file_path
                    edges.append(EdgeInfo("CALLS", caller, call_name, file_path, _get_start_line(child)))
                self._extract_from_tree(child, source, language, file_path, nodes, edges, enclosing_class, enclosing_func, _depth + 1)
                continue

            if node_type in import_types:
                self._extract_imports(child, language, source, file_path, edges)
                continue

            self._extract_from_tree(child, source, language, file_path, nodes, edges, enclosing_class, enclosing_func, _depth + 1)

    def _qualify(self, name: str, file_path: str, enclosing_class: Optional[str]) -> str:
        if enclosing_class:
            return f"{file_path}::{enclosing_class}.{name}"
        return f"{file_path}::{name}"

    def _get_name(self, node, source: bytes) -> Optional[str]:
        for child in _get_children(node):
            t = _get_type(child)
            if t in ("identifier", "name", "type_identifier", "property_identifier", "field_identifier"):
                return _safe_text(child, source)
        return None

    def _get_call_name(self, node, source: bytes) -> Optional[str]:
        children = _get_children(node)
        if not children:
            return None
        first = children[0]
        t = _get_type(first)
        if t in ("identifier", "simple_identifier"):
            return _safe_text(first, source)
        if t in ("attribute", "member_expression"):
            for child in reversed(_get_children(first)):
                if _get_type(child) in ("identifier", "property_identifier"):
                    return _safe_text(child, source)
        return None

    def _extract_imports(self, node, language: str, source: bytes, file_path: str, edges: List[EdgeInfo]) -> None:
        if language == "python":
            node_type = _get_type(node)
            if node_type == "import_from_statement":
                for child in _get_children(node):
                    if _get_type(child) == "dotted_name":
                        target = _safe_text(child, source)
                        edges.append(EdgeInfo("IMPORTS_FROM", file_path, target, file_path, _get_start_line(node)))
                        break
            else:
                for child in _get_children(node):
                    if _get_type(child) == "dotted_name":
                        target = _safe_text(child, source)
                        edges.append(EdgeInfo("IMPORTS_FROM", file_path, target, file_path, _get_start_line(node)))
        elif language in ("javascript", "typescript", "tsx"):
            for child in _get_children(node):
                if _get_type(child) == "string":
                    val = _safe_text(child, source).strip("'\"")
                    edges.append(EdgeInfo("IMPORTS_FROM", file_path, val, file_path, _get_start_line(node)))