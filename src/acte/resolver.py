"""
Call resolution (spec-001): every CallSite gets exactly one status, and a target only
when nothing else is possible. Not yet wired into the graph.
"""

import builtins
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from acte.parser import CallSite, ImportBinding, LocalBinding, NodeInfo, ParseResult

CANDIDATE_CEILING = 100
_BUILTINS = set(dir(builtins))
_NOT_BINDING_KINDS = {"global", "nonlocal", "attr"}


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


@dataclass
class CallResolution:
    call: CallSite
    status: str  # "resolved" | "ambiguous" | "unresolved" | "external"
    tier: str | None = None  # "T2", "T3", "T3*", ...; None unless resolved
    target: str | None = None  # qualified id, only when resolved
    edge: str | None = None  # "calls" | "instantiates" (target is a Class, D9) | None
    recursive: bool = False  # target == caller: keep the record, emit no arrow (R11)
    candidates: list[str] = field(default_factory=list)  # sorted, ambiguous only; empty over the ceiling
    candidate_count: int = 0
    reason: str | None = None  # "local", "no-match", "ceiling", "builtin", "stdlib", "third-party", "unknown-receiver"


@dataclass
class Resolution:
    calls: list[CallResolution]  # sorted by (file, line, col, caller)
    index: SymbolIndex

    def report(self) -> dict:
        """Counts per status, tier (resolved only), reason and edge kind, plus rate = resolved / (total - external)."""
        def count(values):
            return dict(sorted(Counter(v for v in values if v is not None).items()))
        status = count(c.status for c in self.calls)
        measured = len(self.calls) - status.get("external", 0)
        return {
            "edge": count(c.edge for c in self.calls),
            "rate": status.get("resolved", 0) / measured if measured else None,
            "reason": count(c.reason for c in self.calls),
            "status": status,
            "tier": count(c.tier for c in self.calls),
            "total": len(self.calls),
        }


def resolve(results: list[ParseResult], root: Path) -> Resolution:
    index = SymbolIndex(results, root)
    calls = sorted((c for r in results for c in r.call_sites), key=lambda c: (c.file_path, c.line, c.col, c.caller_id))
    return Resolution([_resolve_call(index, c) for c in calls], index)


def _resolve_call(index: SymbolIndex, call: CallSite) -> CallResolution:
    if call.receiver is not None:
        return CallResolution(call, "unresolved", reason="unknown-receiver")
    if not call.file_path.endswith(".py"):  # other languages have no resolver yet (spec §6)
        return CallResolution(call, "unresolved", reason="no-match")
    name, file = call.target_name, call.file_path
    scope = _binding_scope(index, name, call.caller_id, file)
    if scope is not None:
        return _decide(index, call, scope)
    ids, ambiguous = _star_exports(index, name, file)
    if ids:  # a star-imported project name shadows a builtin, as in Python
        return _pick(index, call, ids, ambiguous, "T3*")
    if name in _BUILTINS:
        return CallResolution(call, "external", reason="builtin")
    return CallResolution(call, "unresolved", reason="no-match")


def _star_exports(index: SymbolIndex, name: str, file: str) -> tuple[set[str], bool]:
    """Ids the module's ``*`` imports bind *name* to, and whether a star module matched several files."""
    ids, ambiguous = set(), False
    # shortcut: only project modules count; an external `from os import *` can't be enumerated, enumerate stdlib if it matters
    for b in index.star_imports.get(file, []):
        files = index.module_files(b.module, file)
        ids |= {index.exports[f][name] for f in files if name in index.exports[f] and not name.startswith("_")}
        ambiguous |= len(files) > 1
    return ids, ambiguous


def _scope_chain(index: SymbolIndex, caller: str, file: str) -> list[str]:
    """The caller's scope, its enclosing function scopes innermost first, then the module (LEGB; classes skipped)."""
    chain = [caller]
    qual = caller.split("::", 1)[1] if caller != file else ""
    while "." in qual:
        qual = qual.rsplit(".", 1)[0].removesuffix(".<locals>")
        node = index.by_id.get(f"{file}::{qual}")
        if node is not None and node.kind == "Function":
            chain.append(f"{file}::{qual}")
    return chain + [file] if caller != file else chain


def _binding_scope(index: SymbolIndex, name: str, caller: str, file: str) -> str | None:
    """The nearest scope that binds *name*, honouring ``global`` (go to the module) and ``nonlocal`` (skip)."""
    for scope in _scope_chain(index, caller, file):
        kinds = {b.kind for b in index.locals.get(scope, {}).get(name, [])}
        if "global" in kinds:
            return file if _bindings(index, scope=file, name=name) != ([], []) else None
        if "nonlocal" in kinds:
            continue
        if _bindings(index, scope, name) != ([], []):
            return scope
    return None


def _bindings(index: SymbolIndex, scope: str, name: str) -> tuple[list[LocalBinding], list[ImportBinding]]:
    local = [b for b in index.locals.get(scope, {}).get(name, []) if b.kind not in _NOT_BINDING_KINDS]
    # `import a.b` is recorded as local name "a.b" but binds `a`
    imports = [b for k, v in sorted(index.imports.get(scope, {}).items()) if k.split(".")[0] == name for b in v]
    return local, imports


def _decide(index: SymbolIndex, call: CallSite, scope: str) -> CallResolution:
    """Resolve a bare call from the bindings of the scope that binds its name (brief: Name lookup, 3)."""
    name, file = call.target_name, call.file_path
    local, imports = _bindings(index, scope, name)
    if any(b.kind not in ("def", "class") for b in local):
        return CallResolution(call, "unresolved", reason="local")  # a variable: values are not followed
    ids = {f"{file}::{name}" if scope == file else f"{scope}.<locals>.{name}" for b in local}
    external, unmatched, ambiguous = set(), False, False
    for b in imports:
        files = index.module_files(b.module, file)
        if not files:
            if b.module.startswith("."):  # a relative import is never external
                unmatched = True
            else:
                external.add("stdlib" if b.module.split(".")[0] in sys.stdlib_module_names else "third-party")
            continue
        hits = set() if b.is_module_alias else {index.exports[f][b.imported_name] for f in files if b.imported_name in index.exports[f]}
        unmatched |= not hits  # a module, or a name that is not a project definition
        ids |= hits
        ambiguous |= len(files) > 1
    if scope == file:  # a later `*` import may rebind the name: its project exports are candidates too
        star_ids, star_ambiguous = _star_exports(index, name, file)
        ids |= star_ids
        ambiguous |= star_ambiguous
    ids = {i for i in ids if i in index.by_id}  # the validation rule
    if not ids:
        if external and not unmatched:
            return CallResolution(call, "external", reason="stdlib" if external == {"stdlib"} else "third-party")
        return CallResolution(call, "unresolved", reason="no-match")
    return _pick(index, call, ids, ambiguous or bool(external) or unmatched, "T2" if local else "T3")


def _pick(index: SymbolIndex, call: CallSite, ids: set[str], ambiguous: bool, tier: str) -> CallResolution:
    """One validated target resolves; several (or a forced ambiguity) are recorded, never chosen (D1)."""
    ids = sorted(i for i in ids if i in index.by_id)
    if len(ids) == 1 and not ambiguous:
        target = ids[0]
        edge = "instantiates" if index.by_id[target].kind == "Class" else "calls"
        return CallResolution(call, "resolved", tier, target, edge, target == call.caller_id)
    if not ids:
        return CallResolution(call, "unresolved", reason="no-match")
    if len(ids) > CANDIDATE_CEILING:
        return CallResolution(call, "ambiguous", candidate_count=len(ids), reason="ceiling")
    return CallResolution(call, "ambiguous", candidates=ids, candidate_count=len(ids))
