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
_NARROW = "a fuller name, such as Class.method"
_REASONS = {  # why a call is not known for sure, in plain words: "2 calls on objects of unknown type"
    "unknown-receiver": "on objects of unknown type",
    "local": "through local variables",
    "no-match": "to names not found",
    "ceiling": "with too many candidates",
    "ambiguous": "that could go to several functions",
}


def _text(headline: str, sections: list[tuple[str | None, list[str]]], narrow: str,
          total: int | None = None, tail: tuple[str, ...] = ()) -> str:
    """*headline*, then each non-empty (title, items) section, whole lines within the budget, then *tail*.
    Ends with a Showing line when fewer items than *total* (default: all items given) were shown."""
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
        lines.append(f"Showing {shown} of {total}. Narrow with {narrow}.")
    return "\n".join(lines)


def _calls(reasons: dict[str, int]) -> str:
    """``{"local": 2}`` as "2 calls through local variables"."""
    return ", ".join(f"{n} call{'' if n == 1 else 's'} {_REASONS.get(r, r)}" for r, n in reasons.items() if n)


def _symbol(s: tuple[str, str, str, str, int]) -> str:
    d, kind, _, file, line = s
    return f"{d} ({kind}) {file}:{line}"


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
            matches = [s for s in symbols if name.lower() in (k.lower() for k in (s[0], s[0].split("::", 1)[-1], s[2]))]
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
        ], _NARROW)

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
        unsure = Counter(s.reason or "ambiguous" for s in sites if s.status in ("unresolved", "ambiguous"))
        tail = []
        if external:
            names = [n for n, _ in external.most_common(_MAX_EXTERNAL)] + (["..."] if len(external) > _MAX_EXTERNAL else [])
            tail.append(f"External: {', '.join(names)} ({external.total()} calls)")
        if unsure:
            tail.append(f"Unresolved: {_calls(unsure)}")
        headline = f"callees_of {target}: {len(resolved)} resolved, {external.total()} external, {unsure.total()} unresolved"
        return _text(headline, [("Calls:", [f"{s.resolved_to}  (line {s.line})" for s in resolved])], _NARROW, tail=tuple(tail))

    return server
