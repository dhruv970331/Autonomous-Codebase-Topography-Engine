"""
SPARQL Query Engine.
Pre-built, parameterised SPARQL queries over the ACTE RDF knowledge graph.

Design principle: the LLM decides *which* query to run and *with what argument*.
It never writes SPARQL. Every named query is hardcoded and deterministic — that
is what separates ACTE from hallucination-prone RAG approaches.

Call shape (spec-001 §7)
------------------------
Every call is a ``code:CallSite`` record with a ``code:status``. Only a resolved
call also draws an arrow between real nodes: ``caller code:calls target`` for a
function, ``caller code:instantiates target`` for a class (D9). The caller is a
Function, or the File for module-level calls. A recursive call keeps its record
but draws no arrow (R11); ambiguous, unresolved and external calls draw none.

Queries take a short ``code:name`` and match every node with that name.
"""

import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdflib import Graph

_CODE_NS = "http://acte.local/code#"
_PREFIXES = (
    f"PREFIX code: <{_CODE_NS}>\n"
    "PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>\n"
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class FunctionResult:
    name: str
    file_path: str
    line_start: int
    line_end: int


@dataclass
class ClassResult:
    name: str
    file_path: str
    line_start: int
    line_end: int


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class SPARQLEngine:
    """Executes deterministic multi-hop queries against the ACTE knowledge graph.

    Typical usage — load from a serialised Turtle file:
        engine = SPARQLEngine.from_turtle("graph.ttl")
        hits = engine.blast_radius("db_connect")

    Or inject a live rdflib Graph directly (useful in tests / Colab):
        engine = SPARQLEngine(graph=builder.graph)
    """

    def __init__(self, graph: Graph | None = None) -> None:
        self.graph: Graph = graph if graph is not None else Graph()

    @classmethod
    def from_turtle(cls, path: str | Path) -> "SPARQLEngine":
        """Load a serialised Turtle (.ttl) file and return a ready engine."""
        g = Graph()
        g.parse(str(path), format="turtle")
        return cls(graph=g)

    # ------------------------------------------------------------------
    # Named queries
    # ------------------------------------------------------------------

    def callers_of(self, function_name: str) -> list[FunctionResult]:
        """Return the functions and files that directly call or instantiate anything named *function_name*."""
        q = _PREFIXES + f"""
SELECT DISTINCT ?name ?file ?start ?end WHERE {{
    ?target code:name {self._literal(function_name)} .
    ?caller code:calls|code:instantiates ?target ;
            code:name      ?name ;
            code:filePath  ?file ;
            code:lineStart ?start ;
            code:lineEnd   ?end .
}}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def blast_radius(self, function_name: str) -> list[FunctionResult]:
        """Return every function or file that transitively calls or instantiates anything named *function_name*.

        Uses the SPARQL 1.1 property path ``(code:calls|code:instantiates)+``.
        Changing *function_name* may break every caller returned here.
        """
        q = _PREFIXES + f"""
SELECT DISTINCT ?name ?file ?start ?end WHERE {{
    ?target code:name {self._literal(function_name)} .
    ?caller (code:calls|code:instantiates)+ ?target ;
            code:name      ?name ;
            code:filePath  ?file ;
            code:lineStart ?start ;
            code:lineEnd   ?end .
}}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def callees_of(self, function_name: str, file_path: str) -> list[str]:
        """Return the names of every function or class a call in *function_name* resolves to (itself too, if recursive).

        *file_path* is required to resolve the correct qualified URI when the
        same function name exists in multiple files.
        """
        source = self._uri(f"{file_path}::{function_name}")
        q = _PREFIXES + f"""
SELECT DISTINCT ?callee_name WHERE {{
    ?site code:inFunction {source} ;
          code:resolvedTo ?callee .
    ?callee code:name ?callee_name .
}}
ORDER BY ?callee_name
"""
        return [str(r.callee_name) for r in self.graph.query(q)]

    def functions_in_file(self, file_path: str) -> list[FunctionResult]:
        """Return all functions defined in *file_path*, ordered by line number."""
        q = _PREFIXES + f"""
SELECT ?name ?start ?end WHERE {{
    ?f a code:Function ;
       code:filePath  {self._literal(file_path)} ;
       code:name      ?name ;
       code:lineStart ?start ;
       code:lineEnd   ?end .
}}
ORDER BY ?start
"""
        return [
            FunctionResult(str(r.name), file_path, int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def classes_in_file(self, file_path: str) -> list[ClassResult]:
        """Return all classes defined in *file_path*, ordered by line number."""
        q = _PREFIXES + f"""
SELECT ?name ?start ?end WHERE {{
    ?c a code:Class ;
       code:filePath  {self._literal(file_path)} ;
       code:name      ?name ;
       code:lineStart ?start ;
       code:lineEnd   ?end .
}}
ORDER BY ?start
"""
        return [
            ClassResult(str(r.name), file_path, int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def dependencies_of(self, file_path: str) -> list[str]:
        """Return the import targets of *file_path* (module names or paths)."""
        file_uri = self._uri(file_path)
        q = _PREFIXES + f"""
SELECT DISTINCT ?dep WHERE {{
    {file_uri} code:importsFrom ?dep_uri .
    BIND(STRAFTER(STR(?dep_uri), "{_CODE_NS}") AS ?dep)
}}
ORDER BY ?dep
"""
        return [
            urllib.parse.unquote(str(r.dep))
            for r in self.graph.query(q)
        ]

    def orphan_functions(self) -> list[FunctionResult]:
        """Return functions that are never called by anything in the graph.

        Useful for dead-code detection. A function is an orphan when no
        calls / instantiates arrow and no resolved CallSite of another caller
        points to it. A function called only by itself is an orphan: its
        recursive CallSite does not count (R11).
        """
        q = _PREFIXES + """
SELECT DISTINCT ?name ?file ?start ?end WHERE {
    ?f a code:Function ;
       code:name      ?name ;
       code:filePath   ?file ;
       code:lineStart ?start ;
       code:lineEnd   ?end .
    FILTER NOT EXISTS { ?anything code:calls|code:instantiates ?f . }
    FILTER NOT EXISTS { ?site code:resolvedTo ?f ; code:inFunction ?caller . FILTER(?caller != ?f) }
}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def execute_raw_sparql(self, query: str) -> list[dict[str, Any]]:
        """Escape hatch for dynamic graph exploration.

        Intended for the MCP server's fallback tool and Colab experimentation —
        not for production LLM calls, where named queries are always preferred.
        Returns a list of row dicts, or a single-element list with an 'error'
        key if the query fails.
        """
        try:
            full_query = _PREFIXES + query if "PREFIX code:" not in query else query
            return [
                {str(var): str(val) for var, val in row.asdict().items()}
                for row in self.graph.query(full_query)
            ]
        except Exception as e:  # noqa: BLE001 - bad user SPARQL is reported, not raised
            return [{"error": str(e)}]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _uri(self, identifier: str) -> str:
        """Return a SPARQL inline URI for *identifier*, e.g. ``<code:foo>``."""
        # "#" is encoded: the namespace's "#" must stay the only one in the IRI.
        safe = urllib.parse.quote(identifier, safe="/:.@")
        return f"<{_CODE_NS}{safe}>"

    def _literal(self, value: str) -> str:
        """Return an xsd:string typed SPARQL literal.

        The ``^^xsd:string`` annotation is required because rdflib stores
        Literal(..., datatype=XSD.string) as typed literals. A plain ``"foo"``
        in SPARQL does not match ``"foo"^^xsd:string`` in rdflib's query engine.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"^^xsd:string'