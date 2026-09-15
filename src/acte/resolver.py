"""
Call resolution (spec-001). Step C2a1: the symbol index only, not yet wired in.
"""

from collections import defaultdict
from pathlib import Path

from acte.parser import ImportBinding, LocalBinding, NodeInfo, ParseResult


class SymbolIndex:
    """Lookup tables over every file's ParseResult (spec §4). Every list value is sorted (D8)."""

    def __init__(self, results: list[ParseResult], root: Path) -> None:
        self.by_id: dict[str, NodeInfo] = {}
        self.by_file: dict[str, dict[str, str]] = {}
        self.by_class: dict[str, dict[str, str]] = {}
        self.modules: dict[str, str] = {}
        self.by_loc: dict[tuple[str, int], str] = {}
        locals_: dict[str, dict[str, list[LocalBinding]]] = defaultdict(lambda: defaultdict(list))
        imports: dict[str, dict[str, list[ImportBinding]]] = defaultdict(lambda: defaultdict(list))

        nodes = [n for r in results for n in r.nodes]
        for n in nodes:
            nid = n.qualified_id
            if n.kind == "File":
                self.by_file.setdefault(n.file_path, {})
                module = _module_name(n.file_path, root) if n.file_path.endswith(".py") else None
                if module and Path(self.modules.get(module, "")).name != "__init__.py":  # pkg/__init__.py wins over pkg.py, as in Python
                    self.modules[module] = n.file_path
                continue
            self.by_id[nid] = n  # Class and Function only; a redefinition replaces the earlier one (last wins)
            self.by_loc[(n.file_path, n.line_start)] = nid
            if n.parent_name is None:  # module level only: a method here would let bare calls reach it
                self.by_file[n.file_path][n.name] = nid
            if n.kind == "Class":
                self.by_class.setdefault(nid, {})
        for n in nodes:
            owner = f"{n.file_path}::{n.parent_name}"
            if n.kind == "Function" and n.parent_name and owner in self.by_class:  # direct methods only
                self.by_class[owner][n.name] = n.qualified_id

        for r in results:
            for b in r.local_bindings:
                locals_[b.scope_id][b.name].append(b)
            for b in r.import_bindings:
                imports[b.scope_id][b.local_name].append(b)
        self.locals = {s: {k: sorted(v, key=lambda b: (b.file_path, b.line, b.kind, b.type_name or "")) for k, v in names.items()} for s, names in locals_.items()}
        self.imports = {s: {k: sorted(v, key=lambda b: (b.file_path, b.line, b.module, b.imported_name)) for k, v in names.items()} for s, names in imports.items()}

    def modules_for(self, name: str) -> list[str]:
        """Files whose module is *name* or ends with ``.name`` (src-layout: ``src.acte.parser`` serves ``acte.parser``)."""
        return sorted(f for m, f in self.modules.items() if m == name or m.endswith("." + name))


def _module_name(file_path: str, root: Path) -> str:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; a package's ``__init__.py`` is the package itself."""
    module = ".".join(Path(file_path).relative_to(root).with_suffix("").parts)
    return module.removesuffix(".__init__")
