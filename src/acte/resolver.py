"""
Call resolution (spec-001): every CallSite gets exactly one status, and a target only
when nothing else is possible.
Used by `rdf_builder.build_graph`.
"""

import builtins
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from acte.parser import CallSite, ImportBinding, LocalBinding, NodeInfo, ParseResult

try:
    import jedi
except ImportError:  # jedi is optional (D7): without it tier TJ is skipped
    jedi = None

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
        self.src_modules: dict[str, str] = {}  # src-layout: src/pkg/mod.py is also pkg.mod, as once the package is installed
        self.by_loc: dict[tuple[str, int], str] = {}
        self.first_param: dict[str, str] = {}  # function scope -> its first parameter's name (T1: is it self/cls?)
        locals_: dict[str, dict[str, list[LocalBinding]]] = defaultdict(lambda: defaultdict(list))
        imports: dict[str, dict[str, list[ImportBinding]]] = defaultdict(lambda: defaultdict(list))
        stars: dict[str, list[ImportBinding]] = defaultdict(list)

        nodes = [n for r in results for n in r.nodes]
        src = root / "src"
        # a src/__init__.py makes src a package imported as `src.x`, not a source root
        src_root = not any(n.kind == "File" and Path(n.file_path) == src / "__init__.py" for n in nodes)
        for n in nodes:
            nid = n.qualified_id
            if n.kind == "File":
                self.by_file.setdefault(n.file_path, {})
                if n.file_path.endswith(".py"):
                    _add_module(self.modules, _module_name(n.file_path, root), n.file_path)
                    if src_root and Path(n.file_path).is_relative_to(src):
                        _add_module(self.src_modules, _module_name(n.file_path, src), n.file_path)
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
                if b.kind == "param":  # the parser emits parameters in source order
                    self.first_param.setdefault(b.scope_id, b.name)
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
        """The files whose module is *name*, relative to the root or to ``src/`` (``src/acte/parser.py``
        is ``acte.parser``); a package wins over a module, and two of a kind are ambiguous. Only when
        there is none, files whose root-relative module ends with ``.name``."""
        exact = {t[name] for t in (self.modules, self.src_modules) if name in t}
        if exact:  # as in Python: `nested` is the package, not `nested.nested`
            return sorted({f for f in exact if Path(f).name == "__init__.py"} or exact)
        return sorted(f for m, f in self.modules.items() if m.endswith("." + name))


def _add_module(modules: dict[str, str], module: str, file: str) -> None:
    if Path(modules.get(module, "")).name != "__init__.py":  # pkg/__init__.py wins over pkg.py, as in Python
        modules[module] = file


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
    reason: str | None = None  # "local", "no-match", "ceiling", "builtin", "stdlib", "third-party", "unknown-receiver", "jedi-external"


@dataclass
class Resolution:
    calls: list[CallResolution]  # sorted by (file, line, col, caller)
    index: SymbolIndex
    jedi_version: str | None = None  # the jedi that ran tier TJ; None when it did not run
    jedi_unmapped: int = 0  # jedi definitions in a parsed file that are no project node (ignored)
    jedi_errors: int = 0  # goto calls that raised; those calls stay unknown-receiver

    def report(self) -> dict:
        """Counts per status, tier (resolved only), reason and edge kind, plus rate = resolved / (total - external)."""
        def count(values):
            return dict(sorted(Counter(v for v in values if v is not None).items()))
        status = count(c.status for c in self.calls)
        measured = len(self.calls) - status.get("external", 0)
        return {
            "edge": count(c.edge for c in self.calls),
            "jedi_errors": self.jedi_errors,
            "jedi_unmapped": self.jedi_unmapped,
            "jedi_version": self.jedi_version,
            "rate": status.get("resolved", 0) / measured if measured else None,
            "reason": count(c.reason for c in self.calls),
            "status": status,
            "tier": count(c.tier for c in self.calls),
            "total": len(self.calls),
        }


def resolve(results: list[ParseResult], root: Path, use_jedi: bool = True) -> Resolution:
    index = SymbolIndex(results, root)
    calls = sorted((c for r in results for c in r.call_sites), key=lambda c: (c.file_path, c.line, c.col, c.caller_id))
    resolved = [_resolve_call(index, c) for c in calls]
    if not (use_jedi and jedi):
        return Resolution(resolved, index)
    calls, unmapped, errors = _jedi_tier(index, resolved)
    return Resolution(calls, index, jedi.__version__, unmapped, errors)


def _jedi_tier(index: SymbolIndex, resolved: list[CallResolution]) -> tuple[list[CallResolution], int, int]:
    """TJ (D7): ask jedi where each receiver call left at unknown-receiver goes. Returns the calls,
    how many definitions jedi found in a parsed file that map to no project node, and how many goto calls raised."""
    project = jedi.Project(index.root.resolve())
    files = {Path(f).resolve(): f for f in index.by_file}  # by_loc keys are the parser's path strings
    scripts, lines, unmapped, errors, out = {}, {}, 0, 0, []
    for r in resolved:
        call = r.call
        if r.reason != "unknown-receiver" or call.receiver is None or not call.file_path.endswith(".py"):
            out.append(r)
            continue
        if call.file_path not in scripts:
            scripts[call.file_path] = jedi.Script(path=call.file_path, project=project)
            lines[call.file_path] = Path(call.file_path).read_bytes().split(b"\n")
        col = len(lines[call.file_path][call.line - 1][: call.col].decode("utf-8"))  # CallSite.col counts UTF-8 bytes, jedi characters
        ids, external, other = set(), False, False
        try:
            found = scripts[call.file_path].goto(call.line, col, follow_imports=True)
        except Exception:  # noqa: BLE001 -- a jedi failure on one call must not stop the run; it is counted
            errors += 1
            out.append(r)
            continue
        for d in found:
            path = d.module_path.resolve() if d.module_path else None
            if path not in files:  # only parsed files are the project; a `.venv` or vendored code under the root is not
                external = True
                continue
            nid = index.by_loc.get((files.get(path), d.line))
            if nid in index.by_id and d.type in ("function", "class"):  # a parameter shares its function's line
                ids.add(nid)
            else:
                unmapped += 1
                other = True
        if ids:  # an external or unmapped answer beside a project one: several answers, so ambiguous
            out.append(_pick(index, call, ids, external or other, "TJ"))
        elif external and not other:
            out.append(CallResolution(call, "external", reason="jedi-external"))
        else:
            out.append(r)
    return out, unmapped, errors


def _resolve_call(index: SymbolIndex, call: CallSite, typed: bool = True) -> CallResolution:
    if not call.file_path.endswith(".py"):  # other languages have no resolver yet (spec §6)
        return CallResolution(call, "unresolved", reason="no-match" if call.receiver is None else "unknown-receiver")
    if call.receiver is not None:
        return _resolve_receiver(index, call, typed)
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


def _resolve_receiver(index: SymbolIndex, call: CallSite, typed: bool = True) -> CallResolution:
    """A call on a receiver (brief: Receiver calls): T1 self/cls, T4 a class name, T5 a module path, T4b a typed variable.

    *typed* is False while looking up a type name, so a type is never itself taken from a variable's type.
    """
    parts, name, file = call.receiver.split("."), call.target_name, call.file_path
    unknown = CallResolution(call, "unresolved", reason="unknown-receiver")
    if not all(p.isidentifier() for p in parts):  # a literal, call or subscript: values are not followed (N2)
        callee = call.receiver[:-1].split("(")[0]
        if typed and call.receiver.endswith(")") and call.receiver.count("(") == 1 and all(p.isidentifier() for p in callee.split(".")):
            return _typed(index, call, callee, False, call.caller_id)  # R24a: `C(...).m()`
        return unknown
    head, rest = parts[0], parts[1:]
    scope = _binding_scope(index, head, call.caller_id, file)
    cls = _self_class(index, scope, head) if scope else None
    if cls and not rest:
        return _pick(index, call, {index.by_class[cls].get(name)} - {None}, False, "T1")
    if cls and typed and head == "self" and len(rest) == 1:  # `self.attr`: its types are looked up from C's file
        return _typed(index, call, *_one_type(index.locals.get(cls, {}).get(f"self.{rest[0]}", [])), cls.split("::")[0])
    if cls:
        return unknown
    local, imports = _bindings(index, scope, head) if scope else ([], [])
    if any(b.kind not in ("def", "class") for b in local):
        # a variable: T4b when its type is certain; an import or `*` export of the same name makes it uncertain
        certain = typed and not rest and not imports and not (scope == file and _star_exports(index, head, file)[0])
        return _typed(index, call, *_one_type(index.locals[scope][head]), scope) if certain else unknown
    if typed and not rest and not local and len(imports) == 1 and not (scope in (None, file) and _star_exports(index, head, file)[0]):
        variable = _imported_variable(index, imports[0], file)
        if variable:  # T4b on a variable another project module binds
            return _typed(index, call, *variable)
    defs = {f"{file}::{head}" if scope == file else f"{scope}.<locals>.{head}" for b in local}
    modules, external, misses, ambiguous = set(), set(), set(), False  # misses: why a binding gave no target
    for b in imports:
        files = index.module_files(b.module, file)
        if not b.is_module_alias:
            hits = {index.exports[f][b.imported_name] for f in files if b.imported_name in index.exports[f]}
            submodule = b.module + ("" if b.module.endswith(".") else ".") + b.imported_name
            if hits:
                defs |= hits
                ambiguous |= len(files) > 1
            elif index.module_files(submodule, file):  # `from pkg import sub`
                modules.add(submodule)
            elif files or b.module.startswith("."):  # a project name that is not a definition (a variable)
                misses.add("unknown-receiver")
            else:
                external.add("stdlib" if b.module.split(".")[0] in sys.stdlib_module_names else "third-party")
        elif files:  # `import a.b` binds module `a`; `import a.b as m` binds `a.b`
            modules.add(head if "." in b.local_name else b.module)
        else:
            external.add("stdlib" if b.module.split(".")[0] in sys.stdlib_module_names else "third-party")
    if scope in (None, file):  # a module-level `*` import may bind it too
        star_ids, star_ambiguous = _star_exports(index, head, file)
        defs |= star_ids
        ambiguous |= star_ambiguous
    if scope is None and not defs:
        return CallResolution(call, "external", reason="builtin") if head in _BUILTINS else unknown

    targets = {}  # id -> tier

    def method(cls_id: str | None, tier: str) -> None:
        if cls_id not in index.by_class:
            misses.add("unknown-receiver")  # not a class: a function, or a path we don't follow
        elif name not in index.by_class[cls_id]:
            misses.add("no-match")
        else:
            targets[index.by_class[cls_id][name]] = tier

    for d in sorted(defs):
        method(None if rest else d, "T4")
    for base in sorted(modules):
        for k in range(len(rest), -1, -1):  # the longest prefix of the receiver that names a module
            files = index.module_files(".".join([base, *rest[:k]]), file)
            if files:
                break
        ambiguous |= len(files) > 1
        for f in files:
            if k < len(rest):  # `mod.C.m()`: what is left must be one class
                method(index.exports[f].get(rest[k]) if k == len(rest) - 1 else None, "T5")
            elif name in index.exports[f]:
                targets[index.exports[f][name]] = "T5"
            else:
                misses.add("no-match")
        if not files:  # `import a.b` where `a` is a namespace package and the path names no module
            misses.add("unknown-receiver")
    if targets:
        return _pick(index, call, set(targets), ambiguous or bool(misses or external), min(targets.values()))
    if external and not misses:
        return CallResolution(call, "external", reason="stdlib" if external == {"stdlib"} else "third-party")
    return CallResolution(call, "unresolved", reason="no-match" if misses == {"no-match"} else "unknown-receiver")


def _imported_variable(index: SymbolIndex, b: ImportBinding, file: str) -> tuple[str | None, bool, str] | None:
    """Follow ``from M import name`` (re-imports too, at most 10 hops) to a module-level variable:
    its type (``_one_type``) and the file it is bound in. None when the name is not one such variable."""
    seen = set()
    for _ in range(10):
        files = [] if b.is_module_alias else index.module_files(b.module, file)
        submodule = b.module + ("" if b.module.endswith(".") else ".") + b.imported_name
        if len(files) != 1 or (files[0], b.imported_name) in seen or index.module_files(submodule, file):
            return None
        file, name = files[0], b.imported_name
        seen.add((file, name))
        if name in index.exports[file] or _star_exports(index, name, file)[0]:
            return None  # a definition, or a name a `*` import may bind: the ladder's own business
        local, imports = _bindings(index, file, name)
        if local and not imports:
            return *_one_type(index.locals[file][name]), file
        if local or len(imports) != 1:
            return None
        b = imports[0]
    return None


def _one_type(bindings: list[LocalBinding]) -> tuple[str | None, bool]:
    """The type every binding of a name agrees on (None when one is untyped or two disagree, R7), and whether an annotation says so."""
    bindings = [b for b in bindings if b.kind not in ("del", "global", "nonlocal")]
    types = {b.type_name for b in bindings}
    return types.pop() if len(types) == 1 else None, any(b.kind in ("param", "annotation") for b in bindings)


def _typed(index: SymbolIndex, call: CallSite, type_name: str | None, annotated: bool, scope: str, hop: bool = True) -> CallResolution:
    """T4b: look *type_name* up from *scope* as if it were called there; a project class gives its method,
    a project function its return annotation's class (R24b, one *hop* only)."""
    if type_name is None:
        return CallResolution(call, "unresolved", reason="unknown-receiver")
    receiver, _, cls_name = type_name.rpartition(".")
    file = scope.split("::")[0]
    found = _resolve_call(index, CallSite(scope, cls_name, receiver or None, file, call.line, call.col), typed=False)
    # a builtin function (max, getattr) may return a project object; so do super() and type(x), though they are classes
    builtin_class = isinstance(getattr(builtins, cls_name, None), type) and cls_name not in ("super", "type")
    # an external name that is only called may be a function returning a project object (copy.deepcopy); an annotation names a class
    if found.status == "external" and (builtin_class if found.reason == "builtin" else annotated):
        return CallResolution(call, "external", reason=found.reason)
    if hop and found.status == "resolved" and index.by_id[found.target].kind == "Function":
        fn = index.by_id[found.target]  # its annotation is looked up from its own module
        result = _typed(index, call, fn.return_type, False, fn.file_path, hop=False)
        return CallResolution(call, "unresolved", reason="unknown-receiver") if result.status == "external" else result
    classes = [found.target] if found.status == "resolved" else found.candidates
    if not classes or any(index.by_id[c].kind != "Class" for c in classes):
        return CallResolution(call, "unresolved", reason="unknown-receiver")
    return _pick(index, call, {index.by_class[c][call.target_name] for c in classes if call.target_name in index.by_class[c]}, found.status == "ambiguous", "T4b")


def _self_class(index: SymbolIndex, scope: str, name: str) -> str | None:
    """The class whose method takes *name* (``self``/``cls``) as its first parameter and never rebinds it (T1)."""
    node = index.by_id.get(scope)
    if name not in ("self", "cls") or node is None or node.kind != "Function" or index.first_param.get(scope) != name:
        return None
    owner = f"{node.file_path}::{node.parent_name}"
    return owner if owner in index.by_class and len(index.locals[scope][name]) == 1 else None


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
