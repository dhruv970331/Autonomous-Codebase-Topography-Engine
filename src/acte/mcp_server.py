"""
MCP server.
`acte serve GRAPH.ttl` answers questions about one built graph over stdio. Each tool takes
plain arguments and returns plain text; the LLM never writes SPARQL.
"""

import difflib
import threading
from importlib.metadata import version
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from acte.sparql_engine import SPARQLEngine

_BUDGET = 4000  # characters per answer, about 1,000 tokens
_MAX_MATCHES = 10


def _text(headline: str, items: list[str], total: int, narrow: str) -> str:
    """*headline* then whole *items* lines within the budget; says how many were shown when fewer than *total*."""
    lines, size = [headline], len(headline)
    for item in items:
        if size + len(item) + 1 > _BUDGET - 100:  # room for the closing line
            break
        lines.append(item)
        size += len(item) + 1
    if len(lines) - 1 < total:
        lines.append(f"Showing {len(lines) - 1} of {total}. Narrow with {narrow}.")
    return "\n".join(lines)


def create_server(graph_path: str | Path, project: str | Path | None = None, profile: str = "small") -> MCPServer:
    """A server over the graph in *graph_path*, loaded once and read under one lock (the SDK runs sync tools on worker threads)."""
    engine = SPARQLEngine.from_turtle(graph_path)
    lock = threading.Lock()
    server = MCPServer("acte", version=version("acte"))

    @server.tool(structured_output=False, description=(
        "Find functions and classes by name. Use first when you only know part of a name: it accepts "
        "a full id, Class.method, a bare name, any letter case, and suggests close names on a typo. "
        "Returns up to 10 matches as id (kind) file:line."))
    def find_symbol(name: str) -> str:
        with lock:
            symbols = engine.symbols()
            exact = {d for d, _ in engine._definitions(name)}  # id, qualname, bare name: as callers_of matches
        keys = {s[0]: (s[0], s[0].split("::", 1)[-1], s[2]) for s in symbols}  # id, qualname, bare name
        matches = [s for s in symbols if s[0] in exact]
        if not matches:
            matches = [s for s in symbols if name.lower() in (k.lower() for k in keys[s[0]])]
        if not matches:
            close = difflib.get_close_matches(name, sorted({k for ks in keys.values() for k in ks}))
            return f"No symbol named {name}." + (f" Did you mean: {', '.join(close)}?" if close else "")
        items = [f"{d} ({kind}) {file}:{line}" for d, kind, _, file, line in matches[:_MAX_MATCHES]]
        noun = "match" if len(matches) == 1 else "matches"
        return _text(f'find_symbol "{name}": {len(matches)} {noun}', items, len(matches), "a fuller name, such as Class.method")

    return server
