"""Definitions check against PyCG's micro-benchmark (spec D11).

Every function and class named in a case's callgraph.json must be a node the parser
emits, under its D10 id. The PyCG name -> ACTE id mapping is built with Python's own
`ast`, so it does not depend on the parser under test.
"""
import ast
import json
from pathlib import Path

from acte.parser import CodeParser
from acte.rdf_builder import KnowledgeGraphBuilder

SNIPPETS = Path(__file__).parent / "data" / "pycg" / "snippets"

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
