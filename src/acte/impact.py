"""
Change impact.
`acte impact PATH` maps the lines a git diff touches to the innermost function or class that
holds each one (its decorators included), then lists what calls those, as blast_radius does. A definition the change
deleted is not in the new graph, so it is not found.
"""

import dataclasses
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from rdflib import Graph, URIRef
from rdflib.namespace import RDF

from acte.cli import _git, build
from acte.mcp_server import _calls, _count, _head, _kinds, stale_warning
from acte.rdf_builder import CODE
from acte.sparql_engine import IMPORT_KINDS, KIND_ORDER, BlastAnswer, SPARQLEngine

_HUNK = re.compile(r"^@@ -\d+(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_IMPORTS = {URIRef(p): kind for p, kind in IMPORT_KINDS.items() if not p.endswith("External")}  # of a project file, by kind
_HEADER = ("def ", "async def ", "class ", "@")  # a removed Python line starting so began a definition


@dataclass
class Changed:
    id: str
    kind: str          # "Function" | "Class"
    file_path: str
    line: int          # lineStart
    change: str        # "added" if every line of its span is new, else "modified"
    blast: BlastAnswer  # affected cut at the limit; total is the count before the cut


@dataclass
class ModuleChange:
    file_path: str
    importers: list[str]  # project files that import it, sorted, cut at the limit
    total: int            # importers before the cut
    importer_kinds: dict[str, list[str]]  # a listed importer with no module-level import of it: its kinds, in KIND_ORDER


@dataclass
class Report:
    base: str
    changed: list[Changed]      # by file and line
    modules: list[ModuleChange]  # files changed outside any definition, by path
    affected: int               # distinct ids in all the uncut lists, the changed definitions and files left out
    completeness: str           # "lower-bound" if any blast radius is, else "exact"


def changed_lines(root: Path, base: str) -> tuple[dict[str, set[int]], dict[str, list[tuple[int, int, bool]]], list[str]] | str:
    """(new lines, deletions) per file relative to *root*, plus the untracked files, from the diff of the work tree
    against *base*; or git's error text. A hunk that removed code and added nothing, or only blank and comment lines,
    is a deletion: (the new-side line before it, the line after it, whether its first removed code line in a .py file
    begins a definition). A hunk that removed only blank and comment lines is no deletion."""
    # the prefixes and quotePath are pinned so a user's diff.noprefix / mnemonicPrefix config cannot change the output
    diff = _git(root, "-c", "core.quotePath=false", "diff", "-U0", "--no-color", "--no-ext-diff", "--no-renames", "--relative",
                "--src-prefix=a/", "--dst-prefix=b/", base, "--")
    untracked = _git(root, "ls-files", "-z", "-o", "--exclude-standard")
    for result in (diff, untracked):
        if result is None or result.returncode:
            return "git is not installed" if result is None else result.stderr.strip()
    new: dict[str, set[int]] = {}
    deleted: dict[str, list[tuple[int, int, bool]]] = {}
    path, left, start, count, removed, added = None, 0, 0, 0, [], []
    for line in diff.stdout.splitlines():
        if left:  # a line of the current hunk; counted, so file headers and content cannot be confused
            if line.startswith("\\"):  # "\ No newline at end of file"
                continue
            left -= 1
            (removed if line.startswith("-") else added).append(line[1:])
            if left or path is None:
                continue
            new.setdefault(path, set()).update(range(start, start + count))
            code = [x for x in removed if _code(x, path)]
            if code and not any(_code(x, path) for x in added):
                before, after = (start, start + 1) if count == 0 else (start - 1, start + count)  # git gives a pure deletion the line before
                deleted.setdefault(path, []).append((before, after, path.endswith(".py") and code[0].lstrip().startswith(_HEADER)))
        elif line.startswith("+++ "):
            # shortcut: a path git quotes (tab, newline or quote in its name) is skipped, parse the quoting if that matters
            path = line[6:] if line.startswith("+++ b/") else None  # "+++ /dev/null": the file was deleted
        elif m := _HUNK.match(line):
            start, count = int(m[2]), 1 if m[3] is None else int(m[3])
            left, removed, added = (1 if m[1] is None else int(m[1])) + count, [], []
    return new, deleted, [f for f in untracked.stdout.split("\0") if f]


def _code(line: str, path: str) -> bool:
    """False for a blank line or one that is only a comment (# in .py files, // otherwise)."""
    text = line.strip()
    return bool(text) and not text.startswith("#" if path.endswith(".py") else "//")


def _blank_or_comment(root: Path, path: str) -> set[int]:
    """The lines of *path* in the work tree that are blank or only a comment."""
    try:
        text = (root / path).read_text(errors="replace")
    except OSError:
        return set()
    return {n for n, line in enumerate(text.splitlines(), 1) if not _code(line, path)}


def impact(root: Path, graph: Graph, new: dict[str, set[int]], deleted: dict[str, list[tuple[int, int, bool]]],
           untracked: list[str], base: str, depth: int = 2, limit: int = 50) -> Report:
    """What the new lines and deletions touch in *graph*, and what that affects. A definition covers its decorators
    too. Blank and comment-only new lines touch nothing. In a .py file, removed body code touches the innermost
    definition holding the line before it; a removed definition, and any deletion in other files, touches the
    innermost definition holding the lines on both sides. Outside any definition, the file changes at module level."""
    engine = SPARQLEngine(graph=graph)
    spans: dict[str, list[tuple[int, int, str, str, int]]] = {}  # file -> (first line, end, id, kind, lineStart) of each definition
    ends = {}  # file -> its File node's lineEnd
    for kind in ("Function", "Class", "File"):
        for node in graph.subjects(RDF.type, CODE[kind]):
            file, start, end, decorated = (graph.value(node, CODE[p]) for p in ("filePath", "lineStart", "lineEnd", "decoratorLine"))
            if kind == "File":
                ends[str(file)] = int(end)
            else:
                spans.setdefault(str(file), []).append((int(decorated or start), int(end), engine._id(node), kind, int(start)))
    new = {f: set(n) for f, n in new.items()}
    for f in untracked:  # all of an untracked file is new
        if f in ends:
            new[f] = set(range(1, ends[f] + 1))

    hit: dict[str, tuple[int, int, str, str, int, str]] = {}  # id -> its span, kind, lineStart and file
    module_files = set()

    def touch(f: str, *lines: int) -> None:
        """Mark the innermost definition of *f* holding all of *lines*, else *f* at module level."""
        holding = [s for s in spans.get(f, []) if all(s[0] <= n <= s[1] for n in lines)]
        if holding:
            s = min(holding, key=lambda s: s[1] - s[0])  # the innermost: spans nest, so the shortest
            hit[s[2]] = (*s, f)
        else:
            module_files.add(f)

    for f in new.keys() | deleted.keys():
        if f not in ends:  # not a source file of the graph
            continue
        for n in new.get(f, set()) - _blank_or_comment(root, f):
            touch(f, n)
        for before, after, header in deleted.get(f, ()):
            if header or not f.endswith(".py"):
                touch(f, before, after)
            else:
                touch(f, before)

    results, seen, lower = [], set(), False
    for d, (first, end, _, kind, start, f) in sorted(hit.items(), key=lambda h: (h[1][5], h[1][4], h[0])):
        a = engine.blast_radius(d, depth, sys.maxsize)[0]  # uncut, so the summary can count distinct ids
        seen |= {i.id for i in a.affected}
        lower |= a.completeness == "lower-bound"
        added = all(n in new.get(f, ()) for n in range(first, end + 1))
        results.append(Changed(d, kind, f, start, "added" if added else "modified",
                               dataclasses.replace(a, affected=a.affected[:limit], truncated=a.total > limit)))
    modules = []
    for f in sorted(module_files):
        kinds: dict[str, set[str]] = {}
        for p, kind in _IMPORTS.items():
            for s in graph.subjects(p, _file(graph, f)):
                kinds.setdefault(str(graph.value(s, CODE.filePath)), set()).add(kind)
        importers = sorted(kinds)
        seen |= set(importers)
        listed = importers[:limit]
        modules.append(ModuleChange(f, listed, len(importers),
                                    {i: [k for k in KIND_ORDER if k in kinds[i]] for i in listed if "module" not in kinds[i]}))
    return Report(base, results, modules, len(seen - set(hit) - module_files), "lower-bound" if lower else "exact")


def _file(graph: Graph, path: str):
    """The File node whose filePath is *path*."""
    return next(f for f in graph.subjects(RDF.type, CODE.File) if str(graph.value(f, CODE.filePath)) == path)


def text(report: Report) -> str:
    """*report* in the style of the MCP answers: a summary line, then each change with what it affects by distance."""
    files = {c.file_path for c in report.changed}
    head = f"{_count(len(report.changed), 'definition', 'definitions')} changed in {_count(len(files), 'file', 'files')}"
    if report.modules:
        head += f", {_count(len(report.modules), 'file', 'files')} changed at module level"
    lines = [f"{head}; {_count(report.affected, 'other', 'others')} affected ({report.completeness})"]
    for c in report.changed:
        a = c.blast
        reasons = _calls({k: n for k, n in a.reasons.items() if k != "depth-limit"})
        if "depth-limit" in a.reasons:
            reasons += (", " if reasons else "") + f"stopped at depth {a.depth} while callers continue"
        label = a.completeness + (f": {reasons}" if reasons else "")
        lines.append(f"{c.change} {c.id}  {c.file_path}:{c.line}: {a.total} affected, {len(a.possible)} possible ({label})")
        for d in range(1, a.depth + 1):
            items = [f"    {i.id}  {i.file_path}:{i.line}  (calls {i.via})" for i in a.affected if i.distance == d]
            lines += [f"  Distance {d}:", *items] if items else []
        if a.truncated:
            lines.append(f"  Showing {len(a.affected)} of {a.total}.")
    for m in report.modules:
        lines.append(f"module-level {m.file_path}: imported by {_count(m.total, 'project file', 'project files')}")
        lines += [f"    {f}{_kinds(m.importer_kinds.get(f, []))}" for f in m.importers]
        if len(m.importers) < m.total:
            lines.append(f"  Showing {len(m.importers)} of {m.total}.")
    return "\n".join(lines)


def run(root: Path, base: str, graph_path: str | None, depth: int, limit: int, fmt: str, use_jedi: bool = True) -> int:
    """`acte impact`: print the report; 2 when *root* is not in a git work tree or git fails."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    if not inside or inside.returncode or inside.stdout.strip() != "true":
        print(f"acte impact: {root} is not in a git work tree", file=sys.stderr)
        return 2
    lines = changed_lines(root, base)
    if isinstance(lines, str):
        print(f"acte impact: git failed: {lines}", file=sys.stderr)
        return 2
    if graph_path:
        try:
            graph = Graph().parse(graph_path, format="turtle")
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # rdflib's turtle BadSyntax is a SyntaxError
            print(f"acte impact: cannot read {graph_path}: {exc}", file=sys.stderr)
            return 2
        print(stale_warning(graph.value(CODE["build"], CODE.commit), _head(root)), end="", file=sys.stderr)
    else:
        graph = build(root, use_jedi)
    report = impact(root, graph, *lines, base, depth, limit)
    print(json.dumps(dataclasses.asdict(report), indent=2) if fmt == "json" else text(report))
    return 0
