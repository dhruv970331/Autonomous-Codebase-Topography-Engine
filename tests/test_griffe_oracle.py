"""Re-exports (SymbolIndex.exports) checked against griffe as an independent answer key.

For ACTE's own src/ and every PyCG snippet, the module-level names griffe reports as
aliases of a project class or function must be exactly the names ACTE exports through
imports, with the same target.
"""
import logging
from pathlib import Path

import griffe

from acte.parser import CodeParser
from acte.resolver import SymbolIndex

ROOT = Path(__file__).parent.parent
SNIPPETS = ROOT / "tests" / "data" / "pycg" / "snippets"
PROJECTS = [(ROOT / "src", ROOT)] + [(d, d) for d in sorted(SNIPPETS.glob("*/*"))]

# "<project relative to repo>" -> {(side, alias name, target id relative to repo): reason}. May only shrink.
EXPECTED_DIFFS: dict[str, dict[tuple[str, str, str], str]] = {}


def _griffe_re_exports(search_path: Path) -> set[tuple[str, str, str]]:
    """(module file, alias name, target id) for aliases of module-level classes and functions."""
    loader = griffe.GriffeLoader(search_paths=[str(search_path)])
    tops = {p.stem for p in search_path.glob("*.py")} | {p.name for p in search_path.iterdir() if (p / "__init__.py").exists()}
    for top in sorted(tops):
        loader.load(top)
    loader.resolve_aliases(implicit=False, external=False)
    found = set()

    def walk(module):
        for name, member in module.members.items():
            if member.is_alias:
                try:
                    target = member.final_target
                except griffe.AliasResolutionError:  # external (stdlib, third-party): not a project definition
                    continue
                if target.kind.value in ("class", "function") and target.parent.is_module:
                    found.add((str(module.filepath), name, f"{target.filepath}::{target.name}"))
            elif member.is_module:
                walk(member)

    for module in loader.modules_collection.members.values():
        walk(module)
    return found


def _acte_re_exports(search_path: Path, root: Path) -> set[tuple[str, str, str]]:
    parser = CodeParser()
    index = SymbolIndex([parser.parse_file(f) for f in sorted(search_path.rglob("*.py"))], root)
    return {(f, name, nid) for f, names in index.exports.items() for name, nid in names.items() if index.by_file[f].get(name) != nid}


def test_exports_match_griffe():
    logging.getLogger("griffe").setLevel(logging.ERROR)  # warnings about docstrings and unresolvable externals
    diffs, matched = {}, 0
    for search_path, root in PROJECTS:
        expected, got = _griffe_re_exports(search_path), _acte_re_exports(search_path, root)
        matched += len(expected & got)
        rel = lambda s: s.replace(f"{ROOT}/", "")
        diff = {("griffe only", name, rel(nid)) for _, name, nid in expected - got}
        diff |= {("acte only", name, rel(nid)) for _, name, nid in got - expected}
        if diff:
            diffs[rel(str(search_path))] = diff

    print(f"griffe oracle: {len(PROJECTS)} projects, {matched} re-exports matched")
    assert matched > 0
    assert diffs == {k: set(v) for k, v in EXPECTED_DIFFS.items()}, diffs
