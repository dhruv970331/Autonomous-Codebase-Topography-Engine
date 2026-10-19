"""Checks against PyCG's micro-benchmark (spec D11).

Definitions: every function and class named in a case's callgraph.json must be a node the
parser emits, under its D10 id. The PyCG name -> ACTE id mapping is built with Python's own
`ast`, so it does not depend on the parser under test.

Call graph: ACTE's resolved links, in PyCG names, against each case's callgraph.json, with
a ratchet file of known misses and known wrong links.
"""
import ast
import json
import time
from collections import defaultdict
from pathlib import Path

from acte.parser import CodeParser
from acte.rdf_builder import KnowledgeGraphBuilder
from acte.resolver import _method, resolve

SNIPPETS = Path(__file__).parent / "data" / "pycg" / "snippets"
RATCHET = Path(__file__).parent / "data" / "pycg_ratchet.json"

# "<category>/<case>: <PyCG name>" -> one-line reason. May only shrink.
EXPECTED_MISSES: dict[str, str] = {}


def _modules(case: Path) -> dict[str, Path]:
    """PyCG module name -> file, e.g. "nested.relative" -> nested/relative.py."""
    mods = {}
    for f in sorted(case.rglob("*.py")):
        parts = list(f.relative_to(case).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if parts:  # the case root's own __init__.py has no module name
            mods[".".join(parts)] = f
    return mods


def _definitions(mods: dict[str, Path]) -> dict[str, str]:
    """PyCG name of every def/class -> its D10 id (PyCG writes outer.inner, D10 outer.<locals>.inner)."""
    out = {}

    def walk(node, file, pycg, qual):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{qual}{child.name}"
                out[f"{pycg}.{child.name}"] = f"{file}::{q}"
                walk(child, file, f"{pycg}.{child.name}", q + ("." if isinstance(child, ast.ClassDef) else ".<locals>."))
            else:
                walk(child, file, pycg, qual)

    for mod, file in mods.items():
        walk(ast.parse(file.read_bytes()), file, mod, "")
    return out


def _check_case(key_file: Path) -> tuple[int, list[str]]:
    case = key_file.parent
    mods = _modules(case)
    defs = _definitions(mods)
    builder, parser = KnowledgeGraphBuilder(), CodeParser()
    node_ids = {builder._node_id(n) for f in mods.values() for n in parser.parse_file(f).nodes}

    key = json.loads(key_file.read_text())
    names = set(key) | {c for callees in key.values() for c in callees}
    checked, missing = 0, []
    for name in sorted(names):
        if name.startswith("<") or "<lambda" in name or name in mods:
            continue  # builtins (<builtin>.print), lambdas (no lambda nodes yet), modules
        parts = name.split(".")
        if not any(".".join(parts[:i]) in mods for i in range(1, len(parts))):
            continue  # third-party: its module is not part of the case
        checked += 1
        if defs.get(name) not in node_ids:
            missing.append(f"{case.parent.name}/{case.name}: {name}")
    return checked, missing


def test_pycg_definitions_are_extracted():
    checked, missing = 0, []
    for key_file in sorted(SNIPPETS.glob("*/*/callgraph.json")):
        n, m = _check_case(key_file)
        checked += n
        missing += m

    assert checked == 262, f"PyCG name mapping changed: {checked} definitions checked"
    assert sorted(missing) == sorted(EXPECTED_MISSES)


def _in_case(name: str, mods: dict[str, Path]) -> bool:
    """A PyCG name for code in the case: not a builtin (<builtin>.print), not from a module outside the case."""
    parts = name.split(".")
    return not name.startswith("<") and any(".".join(parts[:i]) in mods for i in range(1, len(parts) + 1))


def _expected_links(key: dict[str, list[str]], mods: dict[str, Path]) -> set[str]:
    """"caller -> callee" for every key edge inside the case, minus lambdas (ACTE has no lambda
    nodes) and self-edges (R11: a recursive call draws no arrow)."""
    return {
        f"{caller} -> {callee}"
        for caller, callees in key.items() for callee in callees
        if _in_case(caller, mods) and _in_case(callee, mods)
        and "<lambda" not in caller + callee and caller != callee
    }


def _actual_links(case: Path, mods: dict[str, Path]) -> set[str]:
    """ACTE's resolved links (ladder only, D8), in PyCG names: module + qualname without <locals>."""
    parser = CodeParser()
    resolution = resolve([parser.parse_file(f) for f in mods.values()], case, use_jedi=False)
    module_of = {str(f): mod for mod, f in mods.items()}

    def pycg(acte_id: str) -> str:
        file, _, qual = acte_id.partition("::")
        return ".".join([module_of[file], *qual.replace(".<locals>", "").split(".")]) if qual else module_of[file]

    links = set()
    for r in resolution.calls:
        if r.status != "resolved" or r.recursive:
            continue
        target = r.target
        if r.edge == "instantiates":  # PyCG lists the constructor: C's own __init__ or one C inherits; none is no link
            target, _ = _method(resolution.index, target, "__init__")
            if target is None:
                continue
        links.add(f"{pycg(r.call.caller_id)} -> {pycg(target)}")
    return links


def test_pycg_call_graph():
    ratchet = json.loads(RATCHET.read_text())
    per_cat = defaultdict(lambda: defaultdict(int))
    missing, wrong = {}, {}
    start = time.perf_counter()
    for key_file in sorted(SNIPPETS.glob("*/*/callgraph.json")):
        case = key_file.parent
        name = f"{case.parent.name}/{case.name}"
        mods = _modules(case)
        expected = _expected_links(json.loads(key_file.read_text()), mods)
        actual = _actual_links(case, mods)
        if expected - actual:
            missing[name] = sorted(expected - actual)
        if actual - expected:
            wrong[name] = sorted(actual - expected)
        known_wrong = set(ratchet["wrong"].get(name, {}))
        counts = per_cat[case.parent.name]
        counts["expected"] += len(expected)
        counts["found"] += len(expected & actual)
        counts["wrong"] += len(actual - expected - known_wrong)
        counts["known_wrong"] += len((actual - expected) & known_wrong)
    elapsed = time.perf_counter() - start

    total = defaultdict(int)
    print(f"\n{'category':<14} {'precision':>9} {'recall':>6} {'expected':>8} {'found':>5} {'wrong':>5} {'known':>5}")
    for cat in [*sorted(per_cat), "TOTAL"]:
        c = total if cat == "TOTAL" else per_cat[cat]
        if cat != "TOTAL":
            for k, v in c.items():
                total[k] += v
        precision = c["found"] / (c["found"] + c["wrong"]) if c["found"] + c["wrong"] else None
        recall = c["found"] / c["expected"] if c["expected"] else None
        fmt = lambda x: "-" if x is None else f"{x:.2f}"
        print(f"{cat:<14} {fmt(precision):>9} {fmt(recall):>6} {c['expected']:>8} {c['found']:>5} {c['wrong']:>5} {c['known_wrong']:>5}")
    print(f"wall time {elapsed:.2f} s")

    for name in sorted(set(missing) | set(ratchet["missing"])):
        new, fixed = set(missing.get(name, [])), set(ratchet["missing"].get(name, []))
        assert not new - fixed, f"{name}: new miss {sorted(new - fixed)}"
        assert not fixed - new, f"{name}: {sorted(fixed - new)} now found: delete it from the ratchet"
    for name in sorted(set(wrong) | set(ratchet["wrong"])):
        new, known = set(wrong.get(name, [])), set(ratchet["wrong"].get(name, {}))
        assert not new - known, f"{name}: new wrong link {sorted(new - known)}"
        assert not known - new, f"{name}: {sorted(known - new)} no longer produced: delete it from the ratchet"
    assert total["wrong"] == 0  # precision 1.00, the ratchet's known wrong links excluded
    assert total["found"] / total["expected"] >= 0.40
    assert per_cat["imports"]["found"] == per_cat["imports"]["expected"]
    assert elapsed < 3
