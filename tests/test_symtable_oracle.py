"""Local bindings checked against the stdlib `symtable` as an independent answer key.

For the module scope and every function scope of ACTE's own source and the PyCG
snippets, the names Python binds locally must be exactly the names ACTE records
(LocalBinding + ImportBinding), and global/nonlocal declarations must match.
"""
import symtable
from collections import defaultdict
from pathlib import Path

from acte.parser import CodeParser

ROOT = Path(__file__).parent.parent
FILES = sorted((ROOT / "src" / "acte").glob("*.py")) + sorted((ROOT / "tests" / "data" / "pycg" / "snippets").rglob("*.py"))

# Kinds that are not names of the scope they are recorded in (see LocalBinding).
_NOT_LOCAL_KINDS = {"comprehension", "lambda", "global", "nonlocal", "attr"}

# "<path relative to repo>::<qualname>" -> {name: one-line reason}. May only shrink.
EXPECTED_DIFFS: dict[str, dict[str, str]] = {}


def _symtable_scopes(path: Path):
    """scope id -> (local names, declared-global names, nonlocal names), module and functions."""
    scopes = defaultdict(lambda: (set(), set(), set()))

    def walk(table, qual, is_func):
        if is_func or not qual:  # class bodies are walked through but not compared
            locals_, globals_, nonlocals = scopes[f"{path}::{qual}" if qual else str(path)]
            for sym in table.get_symbols():
                if sym.is_local():
                    locals_.add(sym.get_name())
                # the module table also flags names that nested functions declare global
                if is_func and sym.is_declared_global():
                    globals_.add(sym.get_name())
                if sym.is_nonlocal():
                    nonlocals.add(sym.get_name())
        for child in table.get_children():
            kind = child.get_type()
            # lambdas and comprehensions are not scopes in ACTE's ids
            if kind not in ("function", "class") or child.get_name() == "lambda" or ".0" in child.get_identifiers():
                continue
            prefix = f"{qual}.<locals>." if is_func else (f"{qual}." if qual else "")
            walk(child, prefix + child.get_name(), kind == "function")

    walk(symtable.symtable(path.read_text(), str(path), "exec"), "", False)
    return scopes


def _acte_scopes(path: Path):
    """scope id -> (local names, global names, nonlocal names) from ACTE's records."""
    result = CodeParser().parse_file(path)
    names, globals_, nonlocals, comp_only = defaultdict(set), defaultdict(set), defaultdict(set), defaultdict(set)
    for b in result.local_bindings:
        if b.kind == "global":
            globals_[b.scope_id].add(b.name)
        elif b.kind == "nonlocal":
            nonlocals[b.scope_id].add(b.name)
        elif b.kind == "comprehension":
            comp_only[b.scope_id].add(b.name)
        elif b.kind not in _NOT_LOCAL_KINDS:
            names[b.scope_id].add(b.name)
    for b in result.import_bindings:
        names[b.scope_id].add(b.local_name.split(".")[0])  # `import a.b` binds `a`; local_name keeps "a.b"
    return names, globals_, nonlocals, comp_only


def test_local_bindings_match_symtable():
    diffs, scopes_compared, names_matched = {}, 0, 0
    for path in FILES:
        names, globals_, nonlocals, comp_only = _acte_scopes(path)
        for scope_id, (st_locals, st_globals, st_nonlocals) in _symtable_scopes(path).items():
            scopes_compared += 1
            # Python 3.12+ inlines list/set/dict comprehensions, so symtable lists their
            # targets as locals; ACTE records them as `comprehension`, which is not a local.
            st_locals = st_locals - (comp_only[scope_id] - names[scope_id])
            acte = names[scope_id] - globals_[scope_id] - nonlocals[scope_id]
            key = scope_id.replace(f"{ROOT}/", "")
            got = {n: "missing in ACTE" for n in st_locals - acte} | {n: "extra in ACTE" for n in acte - st_locals}
            got |= {n: "global mismatch" for n in st_globals ^ globals_[scope_id]}
            got |= {n: "nonlocal mismatch" for n in st_nonlocals ^ nonlocals[scope_id]}
            if got:
                diffs[key] = got
            names_matched += len(st_locals & acte)

    print(f"symtable oracle: {scopes_compared} scopes, {names_matched} names matched")
    assert {k: set(v) for k, v in diffs.items()} == {k: set(v) for k, v in EXPECTED_DIFFS.items()}, diffs
