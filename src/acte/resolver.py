"""
Call resolution (spec-001). Step C2a1: the symbol index only, not yet wired in.
"""

from collections import defaultdict
from pathlib import Path

from acte.parser import ImportBinding, LocalBinding, NodeInfo, ParseResult


class SymbolIndex:
    """Lookup tables over every file's ParseResult (spec §4). Every list value is sorted (D8)."""

    def __init__(self, results: list[ParseResult], root: Path) -> None:
        self.root = root
        self.by_id: dict[str, NodeInfo] = {}
        self.by_file: dict[str, dict[str, str]] = {}
        self.by_class: dict[str, dict[str, str]] = {}
        self.modules: dict[str, str] = {}
        self.by_loc: dict[tuple[str, int], str] = {}
        locals_: dict[str, dict[str, list[LocalBinding]]] = defaultdict(lambda: defaultdict(list))
        imports: dict[str, dict[str, list[ImportBinding]]] = defaultdict(lambda: defaultdict(list))
        stars: dict[str, list[ImportBinding]] = defaultdict(list)

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
            for b in r.star_imports:
                stars[b.scope_id].append(b)
        self.locals = {s: {k: sorted(v, key=lambda b: (b.file_path, b.line, b.kind, b.type_name or "")) for k, v in names.items()} for s, names in locals_.items()}
        self.imports = {s: {k: sorted(v, key=lambda b: (b.file_path, b.line, b.module, b.imported_name)) for k, v in names.items()} for s, names in imports.items()}
        self.star_imports = {s: sorted(v, key=lambda b: (b.file_path, b.line, b.module)) for s, v in stars.items()}
        self.exports = self._exports()

    def _exports(self) -> dict[str, dict[str, str]]:
        """file -> {name: id}: its module-level definitions plus the names its module-level
        imports (``*`` included) bind to project definitions (re-exports, R6), to a fixpoint of at most 10 rounds.

        A name bound to two different definitions (a def and an import, or two imports) is not exported.
        """
        exports = {f: dict(names) for f, names in self.by_file.items()}
        for _ in range(10):
            new = {}
            for f in sorted(self.by_file):
                targets = defaultdict(set, {name: {nid} for name, nid in self.by_file[f].items()})
                for name, bindings in self.imports.get(f, {}).items():
                    for b in bindings:
                        files = [] if b.is_module_alias else self.module_files(b.module, f)
                        # several matching files: ambiguous, so nothing is exported through them
                        if len(files) == 1 and b.imported_name in exports[files[0]]:
                            targets[name].add(exports[files[0]][b.imported_name])
                for b in self.star_imports.get(f, []):
                    files = self.module_files(b.module, f)
                    if len(files) == 1:
                        # shortcut: ignores `__all__` and takes every public name; honour `__all__` if it causes wrong links
                        for name, nid in exports[files[0]].items():
                            if not name.startswith("_"):
                                targets[name].add(nid)
                new[f] = {name: next(iter(t)) for name, t in targets.items() if len(t) == 1}
            if new == exports:
                break
            exports = new
        return exports

    def module_files(self, module: str, importer: str) -> list[str]:
        """Project files for *module* as written in *importer*; relative imports resolve against its package."""
        if not module.startswith("."):
            return self.modules_for(module)
        level = len(module) - len(module.lstrip("."))
        package = _module_name(importer, self.root).split(".")
        if Path(importer).name != "__init__.py":  # a package's __init__ is the package itself
            package = package[:-1]
        if level - 1 > len(package):  # above the project root: not a project module
            return []
        name = ".".join(package[: len(package) - (level - 1)] + [p for p in module.lstrip(".").split(".") if p])
        return [self.modules[name]] if name in self.modules else []

    def modules_for(self, name: str) -> list[str]:
        """Files whose module is *name* or ends with ``.name`` (src-layout: ``src.acte.parser`` serves ``acte.parser``)."""
        return sorted(f for m, f in self.modules.items() if m == name or m.endswith("." + name))


def _module_name(file_path: str, root: Path) -> str:
    """``pkg/sub/mod.py`` -> ``pkg.sub.mod``; a package's ``__init__.py`` is the package itself."""
    module = ".".join(Path(file_path).relative_to(root).with_suffix("").parts)
    return module.removesuffix(".__init__")
