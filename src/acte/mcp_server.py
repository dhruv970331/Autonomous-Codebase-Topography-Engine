"""
MCP server.
`acte serve GRAPH.ttl` answers questions about one built graph over stdio. Each tool takes
plain arguments and returns plain text; the LLM never writes SPARQL.
"""

import difflib
import functools
import subprocess
import threading
from collections import Counter, defaultdict
from importlib.metadata import version
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from rdflib.namespace import RDF

from acte.health import run_checks
from acte.rdf_builder import CODE
from acte.sparql_engine import SPARQLEngine

_BUDGET = 4000  # characters per answer, about 1,000 tokens
_MAX_MATCHES = 10
_MAX_EXTERNAL = 10  # external call names listed by callees_of
_MAX_DEPTH = 3  # blast_radius hops
_NARROW = "Narrow with a fuller name, such as Class.method."
_REASONS = {  # why a call is not known for sure, in plain words: "2 calls on objects of unknown type"
    "unknown-receiver": "on objects of unknown type",
    "unknown-base": "to methods that may be in base classes outside the project or unknown",
    "local": "through local variables",
    "no-match": "to names not found",
    "ceiling": "with too many candidates",
    "ambiguous": "that could go to several functions",
    "override": "to the base-class version that may run this override",
    "builtin": "to builtins",
    "stdlib": "to the standard library",
    "third-party": "to third-party packages",
    "jedi-external": "to outside code found by jedi",
}
_TIERS = {  # how a call was resolved, in plain words: "3 calls on self or cls"
    "T1": "on self or cls",
    "T2": "to names in the same file",
    "T3": "through imports",
    "T3*": "through star imports",
    "T4": "on a class name",
    "T4b": "on variables of known type",
    "T5": "through module names",
    "TJ": "found by jedi",
}


def _text(headline: str, sections: list[tuple[str | None, list[str]]], advice: str | None = None,
          total: int | None = None, tail: tuple[str, ...] = ()) -> str:
    """*headline*, then each non-empty (title, items) section, whole lines within the budget, then *tail*.
    Ends with a Showing line, plus *advice* if any, when fewer items than *total* (default: all items given) were shown."""
    lines, shown, full = [headline], 0, False
    room = _BUDGET - 100 - len(headline) - sum(len(t) + 1 for t in tail)  # 100: room for the Showing line
    for title, items in sections:
        head = [title] if title else []
        for item in items:
            cost = sum(len(x) + 1 for x in head) + len(item) + 1
            if full or cost > room:
                full = True
                break
            lines += head + [item]
            room, shown, head = room - cost, shown + 1, []
    lines += tail
    total = sum(len(items) for _, items in sections) if total is None else total
    if shown < total:
        lines.append(f"Showing {shown} of {total}." + (f" {advice}" if advice else ""))
    return "\n".join(lines)


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _calls(reasons: dict[str, int], words: dict[str, str] = _REASONS) -> str:
    """``{"local": 2}`` as "2 calls through local variables"."""
    return ", ".join(f"{n} call{'' if n == 1 else 's'} {words.get(r, r)}" for r, n in reasons.items() if n)


def _symbol(s: tuple[str, str, str, str, int]) -> str:
    d, kind, _, file, line = s
    return f"{d} ({kind}) {file}:{line}"


def _case_insensitive(name: str, symbols: list[tuple[str, str, str, str, int]]) -> list[tuple[str, str, str, str, int]]:
    """The symbols whose id, qualname or bare name is *name* in any letter case."""
    return [s for s in symbols if name.lower() in (k.lower() for k in (s[0], s[0].split("::", 1)[-1], s[2]))]


def _not_found(name: str, symbols: list[tuple[str, str, str, str, int]]) -> str:
    names = defaultdict(set)  # each spelling (id, qualname, bare name) -> the symbols it names
    for d, _, bare, _, _ in symbols:
        for k in (d, d.split("::", 1)[-1], bare):
            names[k].add(d)
    return _did_you_mean(f"No symbol named {name}.", name, names)


def _did_you_mean(message: str, name: str, known: set[str] | list[str] | dict[str, set[str]]) -> str:
    """*message*, plus up to 3 close spellings from *known*. When *known* maps spellings to the
    symbols they name, spellings naming the same symbols are one suggestion: the shortest."""
    best = {}  # what a spelling names -> its shortest close spelling, in the order of the best match
    for k in difflib.get_close_matches(name, sorted(known), n=10):
        same = frozenset(known[k]) if isinstance(known, dict) else k
        if same not in best or len(k) < len(best[same]):
            best[same] = k
    close = list(best.values())[:3]
    # the retry nudge: without it models ask the user back instead of trying a suggestion
    return message + (f" Did you mean: {', '.join(close)}? Call the tool again with one of these names." if close else "")


def _head(project: str | Path) -> str | None:
    """The HEAD sha of the git work tree at *project*, or None on any git failure."""
    try:
        head = subprocess.run(["git", "-C", str(project), "rev-parse", "HEAD"], check=False, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    return (head.stdout.strip() or None) if head.returncode == 0 else None


def create_server(graph_path: str | Path, project: str | Path | None = None, profile: str = "small") -> MCPServer:
    """A server over the graph in *graph_path*, loaded once and read under one lock (the SDK runs sync tools on worker threads)."""
    engine = SPARQLEngine.from_turtle(graph_path)
    lock = threading.Lock()
    server = MCPServer("acte", version=version("acte"))
    built = engine.graph.value(CODE["build"], CODE.commit)

    def stale() -> str:
        """The warning line to put first when the project has moved on from the graph's commit, else ""."""
        now = _head(project) if project and built else None
        if now is None or now == str(built):
            return ""
        return f"Warning: this map was built at commit {built[:7]} but the project is now at {now[:7]}; answers may be out of date.\n"

    def tool(description: str, full: bool = False):
        """Register the decorated function as a text tool whose answers carry the stale warning; *full*: full profile only."""
        def register(fn):
            if full and profile != "full":
                return fn

            @functools.wraps(fn)
            def answer(*args, **kwargs) -> str:
                return stale() + fn(*args, **kwargs)
            server.tool(structured_output=False, description=description)(answer)
            return fn
        return register

    def known_file(tool_name: str, path: str) -> str | None:
        """None if *path* is a File in the graph, else the answer to give instead. Call under the lock."""
        files = engine.files()
        return None if path in files else _did_you_mean(f"{tool_name}: no file {path} in the map.", path, files)

    def one_definition(tool: str, name: str) -> tuple[str | None, str | None]:
        """(the one definition id *name* matches, None), or (None, the answer to give instead). Call under the lock."""
        defs = [d for d, _ in engine._definitions(name)]
        if len(defs) == 1:
            return defs[0], None
        symbols = engine.symbols()
        defs = defs or [s[0] for s in _case_insensitive(name, symbols)]  # the same levels as find_symbol
        if len(defs) == 1:
            return defs[0], None
        if not defs:
            return None, _not_found(name, symbols)
        where = {s[0]: s for s in symbols}
        headline = f'{tool} "{name}": {len(defs)} definitions match. Ask again with one full name:'
        return None, _text(headline, [(None, [_symbol(where[d]) for d in defs])], _NARROW)

    @tool(
        "Find functions and classes by name. Use first when you only know part of a name: it accepts "
        "a full id, Class.method, a bare name, any letter case, and suggests close names on a typo. "
        "Returns up to 10 matches as id (kind) file:line.")
    def find_symbol(name: str) -> str:
        with lock:
            symbols = engine.symbols()
            exact = {d for d, _ in engine._definitions(name)}  # id, qualname, bare name: as callers_of matches
        matches = [s for s in symbols if s[0] in exact]
        if not matches:
            matches = _case_insensitive(name, symbols)
        if not matches:
            return _not_found(name, symbols)
        noun = "match" if len(matches) == 1 else "matches"
        return _text(f'find_symbol "{name}": {len(matches)} {noun}', [(None, [_symbol(s) for s in matches[:_MAX_MATCHES]])],
                     _NARROW, len(matches))

    @tool(
        "List who calls a function or creates a class, each with file:line. Use before changing or removing "
        "it. Accepts a full id, Class.method or a bare name. Separates confirmed callers from possible ones "
        "and says if the list is exact or a lower bound, and why.")
    def callers_of(name: str) -> str:
        with lock:
            target, answer = one_definition("callers_of", name)
            if answer:
                return answer
            a = engine.callers_of(target)[0]
        label = a.completeness + (f": {_calls(a.reasons)}" if a.reasons else "")
        headline = f"callers_of {a.target}: {len(a.callers)} confirmed, {len(a.possible)} possible ({label})"
        return _text(headline, [
            ("Confirmed:", [f"{c.id}  {c.file_path}:{c.line}" for c in a.callers]),
            ("Possible (could not tell which function is called):", [f"{c.id}  {c.file_path}:{c.line}" for c in a.possible if c.reason == "ambiguous"]),
            ("Possible (call the base-class version, which may run this override):",
             [f"{c.id}  {c.file_path}:{c.line}" for c in a.possible if c.reason == "override"]),
            ("Possible (create a subclass that may get its __init__ from a base class outside the project):",
             [f"{c.id}  {c.file_path}:{c.line}" for c in a.possible if c.reason == "unknown-base"]),
        ])  # one definition already, so no advice: a fuller name would give the same list

    @tool(
        "List what a function calls: the project functions and classes it reaches, with the line of each "
        "call, plus counts of calls to outside code and calls that could not be resolved. Use to see what "
        "a function depends on. Accepts a full id, Class.method or a bare name.")
    def callees_of(name: str) -> str:
        with lock:
            target, answer = one_definition("callees_of", name)
            if answer:
                return answer
            sites = engine.call_sites(target)
        resolved = [s for s in sites if s.status == "resolved"]
        external = Counter(f"{s.receiver}.{s.target_name}" if s.receiver else s.target_name for s in sites if s.status == "external")
        unresolved = Counter(s.reason for s in sites if s.status == "unresolved")
        ambiguous = Counter(s.reason or "ambiguous" for s in sites if s.status == "ambiguous")
        tail = []
        if external:
            names = [n for n, _ in external.most_common(_MAX_EXTERNAL)] + (["..."] if len(external) > _MAX_EXTERNAL else [])
            tail.append(f"External: {', '.join(names)} ({external.total()} calls)")
        if unresolved:
            tail.append(f"Unresolved: {_calls(unresolved)}")
        if ambiguous:
            tail.append(f"Ambiguous: {_calls(ambiguous)}")
        headline = (f"callees_of {target}: {len(resolved)} resolved, {external.total()} external, "
                    f"{unresolved.total()} unresolved, {ambiguous.total()} ambiguous")
        calls = [f"{s.resolved_to}  (line {s.line})" + ("  (recursive)" if s.resolved_to == target else "") for s in resolved]
        return _text(headline, [("Calls:", calls)], tail=tuple(tail))

    @tool(
        "List everything that would be affected by changing a function or class: its callers, their "
        "callers, and so on, grouped by distance. depth is 1 to 3 (default 2). Use to judge the risk of a "
        "change. Accepts a full id, Class.method or a bare name.")
    def blast_radius(name: str, depth: int = 2) -> str:
        depth = min(max(depth, 1), _MAX_DEPTH)
        with lock:
            target, answer = one_definition("blast_radius", name)
            if answer:
                return answer
            a = engine.blast_radius(target, depth, limit=_BUDGET)[0]  # the character budget cuts, not the count
        reasons = _calls({k: n for k, n in a.reasons.items() if k != "depth-limit"})
        if "depth-limit" in a.reasons:
            reasons += (", " if reasons else "") + f"stopped at depth {a.depth} while callers continue"
        label = a.completeness + (f": {reasons}" if reasons else "")
        headline = f"blast_radius {a.target} (depth {a.depth}): {a.total} affected, {len(a.possible)} possible ({label})"
        sections = [(f"Distance {d}:", [f"{i.id}  {i.file_path}:{i.line}  (calls {i.via})" for i in a.affected if i.distance == d])
                    for d in range(1, a.depth + 1)]
        sections.append(("Possible:", [f"{c.id}  {c.file_path}:{c.line}" for c in a.possible]))
        # a smaller depth only helps when something lies beyond distance 1
        return _text(headline, sections, "Use a smaller depth." if any(i.distance > 1 for i in a.affected) else None)

    @tool(
        "List the functions and classes defined in one source file, with their lines. Use to see what a "
        "file contains. path is relative to the project root, as in the other answers.", full=True)
    def functions_in_file(path: str) -> str:
        with lock:
            answer = known_file("functions_in_file", path)
            if answer:
                return answer
            defs = sorted((s for s in engine.symbols() if s[3] == path), key=lambda s: (s[4], s[0]))
        kinds = Counter(s[1] for s in defs)
        headline = f"functions_in_file {path}: {_count(kinds['Function'], 'function', 'functions')}, {_count(kinds['Class'], 'class', 'classes')}"
        return _text(headline, [(None, [_symbol(s) for s in defs])])

    @tool(
        "List what one source file imports: the project files and the outside modules. Use to see what a "
        "file depends on. path is relative to the project root, as in the other answers.", full=True)
    def dependencies_of(path: str) -> str:
        with lock:
            answer = known_file("dependencies_of", path)
            if answer:
                return answer
            files = set(engine.files())
            deps = engine.dependencies_of(path)
        project = [d for d in deps if d in files]
        outside = [d for d in deps if d not in files]
        headline = (f"dependencies_of {path}: {_count(len(project), 'project file', 'project files')}, "
                    f"{_count(len(outside), 'outside module', 'outside modules')}")
        return _text(headline, [("Project files:", project), ("Outside modules:", outside)])

    @tool(
        "List functions that no resolved call reaches: candidates for dead code. Many are not dead: entry "
        "points, __init__, callbacks and code called dynamically also appear. Use to find code to check, "
        "not to delete.", full=True)
    def orphan_functions() -> str:
        with lock:
            orphans = engine.orphan_functions()
            ids = {(s[3], s[4], s[2]): s[0] for s in engine.symbols() if s[1] == "Function"}
        headline = (f"orphan_functions: {_count(len(orphans), 'function', 'functions')} no resolved call reaches; entry points, __init__, "
                    "callbacks and dynamically called code also appear here")
        return _text(headline, [(None, [f"{ids[(f.file_path, f.line_start, f.name)]}  {f.file_path}:{f.line_start}"
                                         for f in orphans])])

    @tool(
        "Describe the map itself: which project and commit it was built from, how many calls were "
        "resolved and how, and whether its health checks pass. Use when answers look incomplete.", full=True)
    def graph_info() -> str:
        g = engine.graph
        meta = CODE["build"]
        with lock:
            cov = engine.coverage()
            failing = [name for name, rows in run_checks(g).items() if rows]
            counts = {kind: len(set(g.subjects(RDF.type, CODE[kind]))) for kind in ("File", "Function", "Class")}
            root, acte, jedi = (g.value(meta, CODE[p]) for p in ("root", "acteVersion", "jediVersion"))
        rate = "n/a" if cov["rate"] is None else f"{cov['rate']:.1%}"
        return "\n".join([
            f"graph_info: {root}, commit {str(built)[:7] if built else 'unknown'}, built by acte {acte}"
            + (f" with jedi {jedi}" if jedi else " without jedi"),
            f"Size: {counts['File']} files, {counts['Function']} functions, {counts['Class']} classes, {cov['total']} calls",
            f"Coverage: {rate} of calls resolved (calls to outside code not counted)",
            f"Status: {', '.join(f'{n} {k}' for k, n in cov['status'].items()) or 'no calls'}",
            f"Resolved: {_calls(cov['tier'], _TIERS) or 'none'}",
            f"Not resolved to project code: {_calls(cov['reason']) or 'none'}",
            f"Health: {'ok' if not failing else 'failing: ' + ', '.join(failing)}",
        ])

    return server
