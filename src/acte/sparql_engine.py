"""
SPARQL Query Engine.
Pre-built, parameterised SPARQL queries over the ACTE RDF knowledge graph.

Design principle: the LLM decides *which* query to run and *with what argument*.
It never writes SPARQL. Every named query is hardcoded and deterministic — that
is what separates ACTE from hallucination-prone RAG approaches.

URI note on CALLS edges
-----------------------
The parser records call targets as short, unqualified names (e.g. "db_connect")
because cross-file call resolution is not yet implemented. The CALLS edge
therefore points to a bare URI such as <code:db_connect>, which is NOT the same
URI as the Function node <code:/path/to/file.py::db_connect>.

Consequence: callers_of() and blast_radius() construct the target URI from the
short name directly. This is intentional and consistent with how rdf_builder
mints those URIs. Cross-file resolution is a V2 concern.
"""

import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

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

    def __init__(self, graph: Optional[Graph] = None) -> None:
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

    def callers_of(self, function_name: str) -> List[FunctionResult]:
        """Return all functions that directly call *function_name*."""
        target = self._uri(function_name)
        q = _PREFIXES + f"""
SELECT DISTINCT ?name ?file ?start ?end WHERE {{
    ?caller a code:Function ;
            code:name      ?name ;
            code:filePath  ?file ;
            code:lineStart ?start ;
            code:lineEnd   ?end ;
            code:calls {target} .
}}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def blast_radius(self, function_name: str) -> List[FunctionResult]:
        """Return every function that transitively calls *function_name*.

        Uses SPARQL 1.1 property path ``code:calls+`` for multi-hop traversal.
        Changing *function_name* may break every function returned here.
        """
        target = self._uri(function_name)
        q = _PREFIXES + f"""
SELECT DISTINCT ?name ?file ?start ?end WHERE {{
    ?caller a code:Function ;
            code:name      ?name ;
            code:filePath  ?file ;
            code:lineStart ?start ;
            code:lineEnd   ?end ;
            code:calls+    {target} .
}}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def callees_of(self, function_name: str, file_path: str) -> List[str]:
        """Return the names of every function directly called by *function_name*.

        *file_path* is required to resolve the correct qualified URI when the
        same function name exists in multiple files.
        """
        source = self._uri(f"{file_path}::{function_name}")
        q = _PREFIXES + f"""
SELECT DISTINCT ?callee_name WHERE {{
    {source} code:calls ?callee_uri .
    BIND(STRAFTER(STR(?callee_uri), "{_CODE_NS}") AS ?callee_name)
}}
ORDER BY ?callee_name
"""
        return [
            urllib.parse.unquote(str(r.callee_name))
            for r in self.graph.query(q)
        ]

    def functions_in_file(self, file_path: str) -> List[FunctionResult]:
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

    def classes_in_file(self, file_path: str) -> List[ClassResult]:
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

    def dependencies_of(self, file_path: str) -> List[str]:
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

    def orphan_functions(self) -> List[FunctionResult]:
        """Return functions that are never called by anything in the graph.

        Useful for dead-code detection. A function is an orphan when no
        ``code:calls`` triple points to its short-name URI.
        """
        q = _PREFIXES + f"""
SELECT DISTINCT ?name ?file ?start ?end WHERE {{
    ?f a code:Function ;
       code:name      ?name ;
       code:filePath   ?file ;
       code:lineStart ?start ;
       code:lineEnd   ?end .
    BIND(URI(CONCAT("{_CODE_NS}", ENCODE_FOR_URI(?name))) AS ?call_target)
    FILTER NOT EXISTS {{ ?anything code:calls ?call_target . }}
}}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def execute_raw_sparql(self, query: str) -> List[Dict[str, Any]]:
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
        except Exception as e:
            return [{"error": str(e)}]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _uri(self, identifier: str) -> str:
        """Return a SPARQL inline URI for *identifier*, e.g. ``<code:foo>``."""
        safe = urllib.parse.quote(identifier, safe="/:.#")
        return f"<{_CODE_NS}{safe}>"

    def _literal(self, value: str) -> str:
        """Return an xsd:string typed SPARQL literal.

        The ``^^xsd:string`` annotation is required because rdflib stores
        Literal(..., datatype=XSD.string) as typed literals. A plain ``"foo"``
        in SPARQL does not match ``"foo"^^xsd:string`` in rdflib's query engine.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"^^xsd:string'