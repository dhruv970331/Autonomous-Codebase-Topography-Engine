"""
MCP server.
`acte serve GRAPH.ttl` answers questions about one built graph over stdio. Each tool takes
plain arguments and returns plain text; the LLM never writes SPARQL.
"""

import difflib
import threading
from collections import Counter
from importlib.metadata import version
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from acte.sparql_engine import SPARQLEngine

_BUDGET = 4000  # characters per answer, about 1,000 tokens
_MAX_MATCHES = 10
_MAX_EXTERNAL = 10  # external call names listed by callees_of
_MAX_DEPTH = 3  # blast_radius hops in the small profile
_NARROW = "Narrow with a fuller name, such as Class.method."
_REASONS = {  # why a call is not known for sure, in plain words: "2 calls on objects of unknown type"
    "unknown-receiver": "on objects of unknown type",
    "local": "through local variables",
    "no-match": "to names not found",
    "ceiling": "with too many candidates",
    "ambiguous": "that could go to several functions",
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


def _calls(reasons: dict[str, int]) -> str:
    """``{"local": 2}`` as "2 calls through local variables"."""
    return ", ".join(f"{n} call{'' if n == 1 else 's'} {_REASONS.get(r, r)}" for r, n in reasons.items() if n)


def _symbol(s: tuple[str, str, str, str, int]) -> str:
    d, kind, _, file, line = s
    return f"{d} ({kind}) {file}:{line}"


def _case_insensitive(name: str, symbols: list[tuple[str, str, str, str, int]]) -> list[tuple[str, str, str, str, int]]:
    """The symbols whose id, qualname or bare name is *name* in any letter case."""
    return [s for s in symbols if name.lower() in (k.lower() for k in (s[0], s[0].split("::", 1)[-1], s[2]))]


def _not_found(name: str, symbols: list[tuple[str, str, str, str, int]]) -> str:
    keys = {k for d, _, bare, _, _ in symbols for k in (d, d.split("::", 1)[-1], bare)}  # id, qualname, bare name
    close = difflib.get_close_matches(name, sorted(keys))
    return f"No symbol named {name}." + (f" Did you mean: {', '.join(close)}?" if close else "")


def create_server(graph_path: str | Path, project: str | Path | None = None, profile: str = "small") -> MCPServer:
    """A server over the graph in *graph_path*, loaded once and read under one lock (the SDK runs sync tools on worker threads)."""
    engine = SPARQLEngine.from_turtle(graph_path)
    lock = threading.Lock()
    server = MCPServer("acte", version=version("acte"))

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

    @server.tool(structured_output=False, description=(
        "Find functions and classes by name. Use first when you only know part of a name: it accepts "
        "a full id, Class.method, a bare name, any letter case, and suggests close names on a typo. "
        "Returns up to 10 matches as id (kind) file:line."))
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

    @server.tool(structured_output=False, description=(
        "List who calls a function or creates a class, each with file:line. Use before changing or removing "
        "it. Accepts a full id, Class.method or a bare name. Separates confirmed callers from possible ones "
        "and says if the list is exact or a lower bound, and why."))
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
            ("Possible (could not tell which function is called):", [f"{c.id}  {c.file_path}:{c.line}" for c in a.possible]),
        ])  # one definition already, so no advice: a fuller name would give the same list

    @server.tool(structured_output=False, description=(
        "List what a function calls: the project functions and classes it reaches, with the line of each "
        "call, plus counts of calls to outside code and calls that could not be resolved. Use to see what "
        "a function depends on. Accepts a full id, Class.method or a bare name."))
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

    @server.tool(structured_output=False, description=(
        "List everything that would be affected by changing a function or class: its callers, their "
        "callers, and so on, grouped by distance. depth is 1 to 3 (default 2). Use to judge the risk of a "
        "change. Accepts a full id, Class.method or a bare name."))
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
        return _text(headline, sections, "Use a smaller depth." if a.depth > 1 else None)

    return server
