"""
Multi-language codebase parser using Tree-sitter.
Extracts structural nodes (classes, functions) and edges (calls,contains, inheritance).
"""

import functools
import keyword
import logging
from dataclasses import dataclass, field
from pathlib import Path

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
    parent_name: str | None = None
    params: str | None = None
    return_type: str | None = None
    bases: list[str] | None = None  # a Python class's bases as written (UNKNOWN_BASE for any other expression); None otherwise
    is_test: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def qualified_id(self) -> str:
        """The node's identifier, matching the source / target strings in EdgeInfo:

          File            →  "<file_path>"                   (name IS the file path)
          Class/Function  →  "<file_path>::<parent>.<name>"  (if nested; parent is
                                                              e.g. "C" or "outer.<locals>")
                          →  "<file_path>::<name>"            (module-level)
        """
        if self.kind == "File":
            return self.name  # name == file_path for File nodes
        if self.parent_name:
            return f"{self.file_path}::{self.parent_name}.{self.name}"
        return f"{self.file_path}::{self.name}"

@dataclass
class EdgeInfo:
    kind: str
    source: str
    target: str
    file_path: str
    line: int = 0
    extra: dict = field(default_factory=dict)

@dataclass
class CallSite:
    caller_id: str
    target_name: str
    receiver: str | None
    file_path: str
    line: int
    col: int

@dataclass
class ImportBinding:
    local_name: str
    imported_name: str
    module: str
    is_module_alias: bool
    file_path: str
    line: int
    scope_id: str

@dataclass
class LocalBinding:
    scope_id: str
    name: str
    kind: str
    type_name: str | None
    file_path: str
    line: int

@dataclass
class ParseResult:
    nodes: list[NodeInfo] = field(default_factory=list)
    edges: list[EdgeInfo] = field(default_factory=list)
    call_sites: list[CallSite] = field(default_factory=list)
    import_bindings: list[ImportBinding] = field(default_factory=list)
    local_bindings: list[LocalBinding] = field(default_factory=list)
    star_imports: list[ImportBinding] = field(default_factory=list)  # `from M import *`: local and imported name "*"

# ---------------------------------------------------------------------------
# Language Syntax Mappings
# ---------------------------------------------------------------------------

EXTENSION_TO_LANGUAGE: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
    ".java": "java",
}

_CLASS_TYPES: dict[str, list[str]] = {
    "python": ["class_definition"],
    "javascript": ["class_declaration", "class"],
    "typescript": ["class_declaration", "class"],
    "tsx": ["class_declaration", "class"],
    "java": ["class_declaration", "interface_declaration", "enum_declaration"],
    "go": ["type_spec"],  # every named type: struct, interface, `type UUID [16]byte`; not `type A = B`
}

_FUNCTION_TYPES: dict[str, list[str]] = {
    "python": ["function_definition"],
    "javascript": ["function_declaration", "method_definition", "arrow_function"],
    "typescript": ["function_declaration", "method_definition", "arrow_function"],
    "tsx": ["function_declaration", "method_definition", "arrow_function"],
    "java": ["method_declaration", "constructor_declaration"],
    "go": ["function_declaration", "method_declaration"],
}

_CALL_TYPES: dict[str, list[str]] = {
    "python": ["call"],
    "javascript": ["call_expression", "new_expression"],
    "typescript": ["call_expression", "new_expression"],
    "tsx": ["call_expression", "new_expression"],
    "java": ["method_invocation", "object_creation_expression"],
    "go": ["call_expression"],
}

_IMPORT_TYPES: dict[str, list[str]] = {
    "python": ["import_statement", "import_from_statement"],
    "javascript": ["import_statement"],
    "typescript": ["import_statement"],
    "tsx": ["import_statement"],
    "java": ["import_declaration"],
    "go": ["import_spec"],
}

# Python nodes that bind local names (_extract_bindings).
_BINDING_TYPES = {
    "parameters", "assignment", "augmented_assignment", "with_item", "for_statement",
    "except_clause", "named_expression", "case_clause", "for_in_clause", "lambda_parameters",
    "global_statement", "nonlocal_statement", "delete_statement",
}
# Targets that unpack into several names, and values that can pair with them element-wise.
_TARGET_SEQUENCES = {"pattern_list", "tuple_pattern", "list_pattern", "tuple", "list"}
_VALUE_SEQUENCES = {"expression_list", "tuple", "list"}
_PUNCTUATION = {"(", ")", "[", "]", ","}
# A class base that is not a plain or dotted name (`f()`, `*bases`): never a name, so it never resolves.
UNKNOWN_BASE = "?"

# Kept whole by _compact_text: whitespace inside a literal is part of its value.
_ATOMIC_TYPES = {"string", "template_string"}
# Dropped by _compact_text: they carry no code.
_SKIP_TYPES = {"comment", "line_continuation"}

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


def _get_start_col(node) -> int:
    """Return the 0-based start column for *node*.

    Standard tree_sitter: ``node.start_point`` → ``(row, col)`` tuple.
    tslp: ``node.start_position()`` → ``Point`` object with ``.column``.
    """
    if not node:
        return 0
    for attr in ("start_point", "start_position"):
        pt = getattr(node, attr, None)
        if pt is None:
            continue
        if callable(pt):
            pt = pt()
        if pt is None:
            continue
        if isinstance(pt, tuple):
            return pt[1]
        col = getattr(pt, "column", None)
        if col is not None:
            return int(col)
    return 0


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

def _compact_text(node, source: bytes) -> str:
    """Return the code of *node* with the whitespace between tokens removed.

    Rebuilt from leaf tokens, so string literals stay intact and comments and
    line continuations disappear. A single space is kept between two word tokens
    (``not x``) so they never merge into one name.
    """
    out: list[str] = []
    stack = [node]  # iterative: long builder chains nest deeper than the recursion limit
    while stack:
        n = stack.pop()
        t = _get_type(n)
        if t in _SKIP_TYPES:
            continue
        children = _get_children(n)
        if children and t not in _ATOMIC_TYPES:
            stack.extend(reversed(children))
            continue
        token = _safe_text(n, source)
        if not token:
            continue
        if out and _is_word_char(out[-1][-1]) and _is_word_char(token[0]):
            out.append(" ")
        out.append(token)
    return "".join(out)


def _is_word_char(c: str) -> bool:
    return c.isalnum() or c == "_"

# ---------------------------------------------------------------------------
# Parser Engine
# ---------------------------------------------------------------------------

class CodeParser:
    """Parses source files using Tree-sitter and extracts structural topography."""

    def __init__(self) -> None:
        self._parsers: dict[str, object] = {}

    def _get_parser(self, language: str):
        if language not in self._parsers:
            try:
                self._parsers[language] = tslp.get_parser(language)
            except Exception as exc:  # noqa: BLE001 - unsupported grammar must not abort a run
                logger.debug("Tree-sitter parser unavailable for %s: %s", language, exc)
                return None
        return self._parsers[language]

    def detect_language(self, path: Path) -> str | None:
        return EXTENSION_TO_LANGUAGE.get(path.suffix.lower())

    def parse_file(self, path: Path) -> ParseResult:
        try:
            source = path.read_bytes()
        except (OSError, PermissionError):
            return ParseResult()

        language = self.detect_language(path)
        if not language:
            return ParseResult()

        parser = self._get_parser(language)
        if not parser:
            return ParseResult()

        try:
            tree = parser.parse(source)
        except TypeError:
            tree = parser.parse(source.decode("utf-8", errors="replace"))

        nodes: list[NodeInfo] = []
        edges: list[EdgeInfo] = []
        call_sites: list[CallSite] = []
        import_bindings: list[ImportBinding] = []
        local_bindings: list[LocalBinding] = []
        file_path_str = path.as_posix()

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
            self._extract_from_tree(root, source, language, file_path_str, nodes, edges, call_sites, import_bindings, local_bindings)

        # `*` binds no name of its own, so star imports travel separately from the name bindings
        star_imports = [b for b in import_bindings if b.local_name == "*"]
        import_bindings = [b for b in import_bindings if b.local_name != "*"]
        return ParseResult(nodes=nodes, edges=edges, call_sites=call_sites, import_bindings=import_bindings, local_bindings=local_bindings, star_imports=star_imports)

    def _extract_from_tree(self, root, source, language, file_path, nodes, edges, call_sites, import_bindings, local_bindings, scope=None, in_func=False, func=None, method_of=None, _depth=0):
        """Walk *root*, giving definitions ids shaped like Python's ``__qualname__``.

        *scope* is the qualname of the innermost enclosing class or function (None at
        module level). *in_func* means that scope is a function, so names defined
        inside it get ``<locals>``: ``outer.<locals>.helper``, but ``A.B`` for a class in a class.
        *func* is the qualname of the innermost enclosing function, the caller of any call.
        *method_of* is the class qualname when that scope is a method, the owner of ``self.X``.
        """
        if _depth > 180 or not root:
            return

        class_types = set(_CLASS_TYPES.get(language, []))
        func_types = set(_FUNCTION_TYPES.get(language, []))
        call_types = set(_CALL_TYPES.get(language, []))
        import_types = set(_IMPORT_TYPES.get(language, []))

        for child in _get_children(root):
            node_type = _get_type(child)

            if node_type in class_types or node_type in func_types:
                name_node = self._get_name_node(child)
                name = _safe_text(name_node, source) if name_node else None
                if name:
                    is_func = node_type in func_types
                    parent = f"{scope}.<locals>" if in_func else scope
                    if language == "go" and node_type == "method_declaration":
                        # `Recv.Method`, contained by the File: the type may be declared in another file of the package
                        parent = self._go_receiver_type(child, source)
                        if parent is None:  # a receiver shape not handled, such as `(p (*Point))`: no id for it
                            continue
                    qualname = f"{parent}.{name}" if parent else name
                    container = f"{file_path}::{scope}" if scope else file_path
                    parts = _get_children(child)
                    types = [_get_type(c) for c in parts]
                    # R24b: `-> Database`, as written; the resolver decides what it names
                    returns = self._annotation_type(parts[types.index("->") + 1], source) if language == "python" and is_func and "->" in types else None
                    bases = self._class_bases(parts, source) if language == "python" and not is_func else None
                    nodes.append(NodeInfo("Function" if is_func else "Class", name, file_path, _get_start_line(child), _get_end_line(child), language, parent, return_type=returns, bases=bases))
                    edges.append(EdgeInfo("CONTAINS", container, f"{file_path}::{qualname}", file_path, _get_start_line(child)))
                    if language == "python":  # R17: the name is bound in the enclosing scope
                        local_bindings.append(LocalBinding(container, name, "def" if is_func else "class", None, file_path, _get_start_line(name_node)))
                    self._extract_from_tree(child, source, language, file_path, nodes, edges, call_sites, import_bindings, local_bindings, qualname, is_func, qualname if is_func else func, scope if is_func and scope and not in_func else None, _depth + 1)
                continue

            if node_type in call_types:
                name_node = self._get_call_name_node(child)
                call_name = _safe_text(name_node, source) if name_node else None
                if call_name:
                    # A call in a class body runs in the enclosing function, or the module.
                    caller = f"{file_path}::{func}" if func else file_path
                    receiver = self._get_call_receiver(child, source)
                    call_sites.append(CallSite(caller, call_name, receiver, file_path, _get_start_line(name_node), _get_start_col(name_node)))
                self._extract_from_tree(child, source, language, file_path, nodes, edges, call_sites, import_bindings, local_bindings, scope, in_func, func, method_of, _depth + 1)
                continue

            if node_type in import_types:
                # Innermost scope, class included: a class-body import is visible only in that body.
                scope_id = f"{file_path}::{scope}" if scope else file_path
                self._extract_imports(child, language, source, file_path, edges, import_bindings, scope_id)
                continue

            if language == "python" and node_type in _BINDING_TYPES:
                scope_id = f"{file_path}::{scope}" if scope else file_path
                method_id = f"{file_path}::{method_of}" if method_of else None
                self._extract_bindings(child, source, file_path, scope_id, method_id, local_bindings)
                # no continue: values and default arguments still hold calls

            self._extract_from_tree(child, source, language, file_path, nodes, edges, call_sites, import_bindings, local_bindings, scope, in_func, func, method_of, _depth + 1)

    def _get_name_node(self, node):
        for child in _get_children(node):
            t = _get_type(child)
            if t in ("identifier", "name", "type_identifier", "property_identifier", "field_identifier"):
                return child
        return None

    def _go_receiver_type(self, method, source: bytes) -> str | None:
        """The receiver type name of a Go method, pointer ``*`` and type arguments removed:
        ``Point`` for ``(p *Point)`` or ``(p (*Point))``, ``Pair`` for ``(p *Pair[T])``. None for any other receiver shape."""
        receiver = next((c for c in _get_children(method) if _get_type(c) == "parameter_list"), None)
        decl = next((c for c in _get_children(receiver) if _get_type(c) == "parameter_declaration"), None)
        t = _get_children(decl)[-1] if decl else None  # the type comes last, after the optional name
        while _get_type(t) == "parenthesized_type":  # `(p (*Point))`
            t = _get_children(t)[1]
        if _get_type(t) == "pointer_type":
            t = _get_children(t)[-1]
        if _get_type(t) == "generic_type":
            t = _get_children(t)[0]
        return _safe_text(t, source) if _get_type(t) == "type_identifier" else None

    def _get_call_name(self, node, source: bytes) -> str | None:
        name_node = self._get_call_name_node(node)
        return _safe_text(name_node, source) if name_node else None

    def _get_call_name_node(self, node):
        children = _get_children(node)
        if not children:
            return None
        first = children[0]
        if _get_type(first) == "list_splat":  # `return *g(), x` parses as a call of `*g`; the star belongs to the result
            first = _get_children(first)[-1]
        t = _get_type(first)
        if t in ("identifier", "simple_identifier"):
            return first
        if t in ("attribute", "member_expression", "selector_expression"):
            for child in reversed(_get_children(first)):
                if _get_type(child) in ("identifier", "property_identifier", "field_identifier"):
                    return child
        return None

    def _get_call_receiver(self, node, source: bytes) -> str | None:
        children = _get_children(node)
        if not children:
            return None
        first = children[0]
        if _get_type(first) == "list_splat":  # as in _get_call_name_node
            first = _get_children(first)[-1]
        t = _get_type(first)
        if t in ("identifier", "simple_identifier"):
            return None
        if t in ("attribute", "member_expression", "selector_expression"):
            # `return *o.m(), x` parses with the star on `o`; it belongs to the result, not the receiver
            full_text = _compact_text(first, source).lstrip("*")
            if "." not in full_text:
                return None
            method_name = self._get_call_name(node, source)
            if method_name:
                suffix = "." + method_name
                if full_text.endswith(suffix):
                    return full_text[: -len(suffix)]
        return None

    def _extract_bindings(self, node, source: bytes, file_path: str, scope_id: str, method_id: str | None, local_bindings: list[LocalBinding]) -> None:
        """LocalBinding records for one binding node (Python). *method_id* owns ``self.X`` targets."""
        t = _get_type(node)
        children = _get_children(node)
        # methods and a partial, not nested recursive defs: those form a reference cycle per call that keeps
        # self, and its native tree-sitter parsers, alive until the GC runs, possibly on another thread
        bind = functools.partial(self._bind, source, file_path, scope_id, method_id, local_bindings)

        if t in ("parameters", "lambda_parameters"):
            # A lambda is not a scope in our ids, so its parameters land in the enclosing scope:
            # over-shadowing there can only lose links, never make wrong ones.
            kind = "param" if t == "parameters" else "lambda"
            for p in children:
                pt = _get_type(p)
                if pt in _PUNCTUATION or pt in ("keyword_separator", "positional_separator"):
                    continue
                name_node = p if pt == "identifier" else None
                type_node = None
                splat = pt in ("list_splat_pattern", "dictionary_splat_pattern")
                for c in [p] if splat else _get_children(p):
                    ct = _get_type(c)
                    if name_node is None and ct == "identifier":
                        name_node = c
                    elif name_node is None and ct in ("list_splat_pattern", "dictionary_splat_pattern"):
                        splat = True
                        name_node = next((g for g in _get_children(c) if _get_type(g) == "identifier"), None)
                    elif ct == "type":
                        type_node = c
                if name_node is not None:
                    # *args: X annotates the elements; args itself is a tuple (a dict for **kw)
                    bind(name_node, None, kind, None if splat else self._annotation_type(type_node, source))
        elif t == "assignment" and children:
            types = [_get_type(c) for c in children]
            if ":" in types:
                type_node = children[types.index(":") + 1]
                bind(children[0], None, "annotation", self._annotation_type(type_node, source))
            elif "=" in types:
                value = children[types.index("=") + 1]
                while _get_type(value) == "assignment":  # a = b = C(): every target gets C
                    value = _get_children(value)[-1]
                bind(children[0], value, "assign")
        elif t == "augmented_assignment" and children:
            bind(children[0], None, "assign")
        elif t == "with_item":
            for c in children:
                if _get_type(c) == "as_pattern":
                    parts = _get_children(c)
                    target = next((g for g in parts if _get_type(g) == "as_pattern_target"), None)
                    for g in _get_children(target):
                        bind(g, parts[0], "with")
        elif t == "for_statement":
            types = [_get_type(c) for c in children]
            if "for" in types:
                bind(children[types.index("for") + 1], None, "for")
        elif t == "for_in_clause":
            # Comprehensions are not scopes in our ids, so targets land in the enclosing scope:
            # over-shadowing there can only lose links, never make wrong ones.
            types = [_get_type(c) for c in children]
            if "for" in types:
                bind(children[types.index("for") + 1], None, "comprehension")
        elif t == "named_expression" and children:
            bind(children[0], children[-1], "walrus")
        elif t == "except_clause":
            group = "*" in [_get_type(c) for c in children]  # except* binds an ExceptionGroup, not E
            for c in children:
                if _get_type(c) == "as_pattern":
                    parts = _get_children(c)
                    exc_type = None if group or not self._is_dotted(parts[0]) else _compact_text(parts[0], source)
                    for g in _get_children(parts[-1]):
                        bind(g, None, "except", exc_type)
        elif t in ("global_statement", "nonlocal_statement"):
            for c in children:
                if _get_type(c) == "identifier":
                    bind(c, None, t.split("_")[0])
        elif t == "delete_statement":
            for c in children[1:]:
                self._unbind(c, bind)
        elif t == "case_clause":
            for c in children:
                if _get_type(c) == "case_pattern":
                    self._match_captures(c, source, lambda n, type_name: bind(n, None, "match", type_name))

    def _bind(self, source: bytes, file_path: str, scope_id: str, method_id: str | None, local_bindings: list[LocalBinding],
              target, value, kind, type_name=None) -> None:
        """LocalBinding records for the names *target* binds to *value* (see _extract_bindings)."""
        bind = functools.partial(self._bind, source, file_path, scope_id, method_id, local_bindings)
        tt = _get_type(target)
        if tt == "identifier":
            type_name = type_name or self._callee_type(value, source)
            local_bindings.append(LocalBinding(scope_id, _safe_text(target, source), kind, type_name, file_path, _get_start_line(target)))
        elif tt == "attribute" and method_id:
            parts = _get_children(target)
            if len(parts) == 3 and _get_type(parts[0]) == "identifier" and _safe_text(parts[0], source) == "self":
                type_name = type_name or self._callee_type(value, source)
                local_bindings.append(LocalBinding(method_id, "self." + _safe_text(parts[2], source), "attr", type_name, file_path, _get_start_line(target)))
        elif tt == "list_splat_pattern":  # *rest is a list: never the paired value's type
            for c in _get_children(target):
                bind(c, None, kind)
        elif tt in _TARGET_SEQUENCES:
            targets = [c for c in _get_children(target) if _get_type(c) not in _PUNCTUATION]
            values = [c for c in _get_children(value) if _get_type(c) not in _PUNCTUATION] if _get_type(value) in _VALUE_SEQUENCES else []
            # R24c: pair element-wise only when both sides are plain sequences of equal length
            paired = len(values) == len(targets) and not any("splat" in _get_type(c) for c in targets + values)
            for i, c in enumerate(targets):
                bind(c, values[i] if paired else None, kind)

    def _unbind(self, n, bind) -> None:
        """``del`` targets: only plain names; ``del obj.x`` and ``del a[i]`` bind nothing."""
        if _get_type(n) == "identifier":
            bind(n, None, "del")
        elif _get_type(n) in ("expression_list", "tuple", "list", "parenthesized_expression"):
            for c in _get_children(n):
                self._unbind(c, bind)

    def _match_captures(self, node, source: bytes, emit) -> None:
        """Call *emit(identifier, type_name)* for every name a match pattern captures."""
        t = _get_type(node)
        children = _get_children(node)
        if t == "dotted_name":
            # a bare name captures; a dotted one (Color.RED) is a value pattern
            if len(children) == 1 and _safe_text(children[0], source) != "_":
                emit(children[0], None)
        elif t == "splat_pattern":
            for c in children:
                if _get_type(c) == "identifier" and _safe_text(c, source) != "_":
                    emit(c, None)
        elif t == "as_pattern" and children:
            self._match_captures(children[0], source, emit)
            inner = _get_children(children[0])
            cls = inner[0] if len(inner) == 1 and _get_type(inner[0]) == "class_pattern" else None
            emit(children[-1], _compact_text(_get_children(cls)[0], source) if cls else None)
        else:
            # skip the class name of a class pattern and the keyword of a keyword pattern
            for c in children[1:] if t in ("class_pattern", "keyword_pattern") else children:
                self._match_captures(c, source, emit)

    def _callee_type(self, value, source: bytes) -> str | None:
        """The callee of *value* as written, when *value* is a call on a plain or dotted name."""
        if _get_type(value) != "call":
            return None
        callee = _get_children(value)[0]
        return _compact_text(callee, source) if self._is_dotted(callee) else None

    def _annotation_type(self, type_node, source: bytes) -> str | None:
        """The annotation as written, when it is a plain or dotted name X, or X made optional:
        ``X | None``, ``None | X``, ``Optional[X]``, ``typing.Optional[X]``, each also in quotes.
        A method call on an optional value only works when it is an X. Any other union is None."""
        parts = _get_children(type_node)
        text = _compact_text(type_node, source)
        if len(parts) == 1 and _get_type(parts[0]) == "string":  # x: "Database"
            inner = _get_children(parts[0])
            if len(inner) != 3 or _get_type(inner[1]) != "string_content":
                return None
            text = "".join(_safe_text(inner[1], source).split())
        sides = text.split("|")
        if len(sides) == 2 and "None" in sides:
            text = sides[1] if sides[0] == "None" else sides[0]
        for prefix in ("Optional[", "typing.Optional["):
            if text.startswith(prefix) and text.endswith("]"):
                text = text[len(prefix):-1]
        if all(p.isidentifier() and not keyword.iskeyword(p) for p in text.split(".")):
            return text
        return None

    def _class_bases(self, parts, source: bytes) -> list[str]:
        """The bases of a Python class as written: a plain or dotted name, the name before ``[`` of a
        subscript (``Generic[T]``), else UNKNOWN_BASE. Keyword arguments (``metaclass=M``, ``**kw``) are not bases."""
        bases = []
        for c in _get_children(next((p for p in parts if _get_type(p) == "argument_list"), None)):
            t = _get_type(c)
            if t in _PUNCTUATION or t in _SKIP_TYPES or t in ("keyword_argument", "dictionary_splat"):
                continue
            if t == "subscript":
                c = _get_children(c)[0]
            bases.append(_compact_text(c, source) if self._is_dotted(c) else UNKNOWN_BASE)
        return bases

    def _is_dotted(self, node) -> bool:
        t = _get_type(node)
        if t == "identifier":
            return True
        if t == "attribute":
            parts = _get_children(node)
            return len(parts) == 3 and self._is_dotted(parts[0]) and _get_type(parts[2]) == "identifier"
        return False

    def _extract_imports(self, node, language: str, source: bytes, file_path: str, edges: list[EdgeInfo], import_bindings: list[ImportBinding], scope_id: str) -> None:
        if language == "python":
            node_type = _get_type(node)
            line = _get_start_line(node)
            if node_type == "import_from_statement":
                for child in _get_children(node):
                    # A relative module is a `relative_import`, not a `dotted_name`. Without
                    # it the loop skips past the module and matches an imported name instead.
                    if _get_type(child) in ("dotted_name", "relative_import"):
                        target = _safe_text(child, source)
                        edges.append(EdgeInfo("IMPORTS_FROM", file_path, target, file_path, line))
                        break
                self._extract_import_from_bindings(node, source, file_path, line, import_bindings, scope_id)
            else:
                for child in _get_children(node):
                    if _get_type(child) == "dotted_name":
                        target = _safe_text(child, source)
                        edges.append(EdgeInfo("IMPORTS_FROM", file_path, target, file_path, line))
                self._extract_import_bindings(node, source, file_path, line, import_bindings, scope_id)
        elif language in ("javascript", "typescript", "tsx"):
            for child in _get_children(node):
                if _get_type(child) == "string":
                    val = _safe_text(child, source).strip("'\"")
                    edges.append(EdgeInfo("IMPORTS_FROM", file_path, val, file_path, _get_start_line(node)))
        elif language == "go":  # one import_spec: its path, without the alias
            for child in _get_children(node):
                if _get_type(child) in ("interpreted_string_literal", "raw_string_literal"):
                    val = _safe_text(child, source).strip('"`')
                    edges.append(EdgeInfo("IMPORTS_FROM", file_path, val, file_path, _get_start_line(node)))

    def _extract_import_bindings(self, node, source: bytes, file_path: str, line: int, import_bindings: list[ImportBinding], scope_id: str) -> None:
        """Bindings for a plain ``import_statement`` (``import calc``, ``import calc as c``)."""
        for child in _get_children(node):
            t = _get_type(child)
            if t == "dotted_name":
                name = _safe_text(child, source)
                import_bindings.append(ImportBinding(name, name, name, True, file_path, line, scope_id))
            elif t == "aliased_import":
                module_name = None
                alias = None
                for sub in _get_children(child):
                    st = _get_type(sub)
                    if st == "dotted_name":
                        module_name = _safe_text(sub, source)
                    elif st == "identifier":
                        alias = _safe_text(sub, source)
                if module_name and alias:
                    import_bindings.append(ImportBinding(alias, module_name, module_name, True, file_path, line, scope_id))

    def _extract_import_from_bindings(self, node, source: bytes, file_path: str, line: int, import_bindings: list[ImportBinding], scope_id: str) -> None:
        """Bindings for an ``import_from_statement`` (``from calc import add``, ``... as plus``, ``*``)."""
        module_text: str | None = None
        seen_import_kw = False
        for child in _get_children(node):
            t = _get_type(child)
            if t == "import":
                seen_import_kw = True
                continue
            if not seen_import_kw:
                if t in ("dotted_name", "relative_import"):
                    module_text = _safe_text(child, source)
                continue
            if module_text is None:
                continue
            if t == "dotted_name":
                name = _safe_text(child, source)
                import_bindings.append(ImportBinding(name, name, module_text, False, file_path, line, scope_id))
            elif t == "aliased_import":
                imported_name = None
                alias = None
                for sub in _get_children(child):
                    st = _get_type(sub)
                    if st == "dotted_name":
                        imported_name = _safe_text(sub, source)
                    elif st == "identifier":
                        alias = _safe_text(sub, source)
                if imported_name and alias:
                    import_bindings.append(ImportBinding(alias, imported_name, module_text, False, file_path, line, scope_id))
            elif t == "wildcard_import":  # split out into ParseResult.star_imports by parse_file
                import_bindings.append(ImportBinding("*", "*", module_text, False, file_path, line, scope_id))