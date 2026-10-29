"""
RDF Knowledge Graph Builder.
Translates AST structural data (NodeInfo / EdgeInfo) into a formal Semantic Web
graph for SPARQL querying.
"""

import os
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, RDFS, XSD  # noqa: F401 - RDFS is for health.py

from acte.parser import CodeParser, EdgeInfo, NodeInfo, ParseResult
from acte.resolver import CallResolution, Resolution, SymbolIndex, resolve

# Formal namespace for the codebase topography ontology
CODE = Namespace("http://acte.local/code#")


# Terms of a map. Every module makes and reads terms through these, so the store behind them can change in one place.
def lit(value: str | int | bool) -> Literal:
    """A literal typed by its Python type: xsd:boolean, xsd:integer or xsd:string."""
    datatype = XSD.boolean if isinstance(value, bool) else XSD.integer if isinstance(value, int) else XSD.string
    return Literal(value, datatype=datatype)


def iri(text: str) -> URIRef:
    """The node with the full IRI *text*."""
    return URIRef(text)


def text(term) -> str:
    """An IRI's text, or a literal's value as written in the map."""
    return str(term)


def is_blank(term) -> bool:
    return isinstance(term, BNode)


def nt(term) -> str:
    """*term* as N-Triples and SPARQL write it, e.g. ``<http://...>``."""
    return term.n3()


# Lookups and edits. None matches anything; remove() takes away every fact matching the pattern.
def new_graph() -> Graph:
    return Graph()


def triples(graph: Graph, s=None, p=None, o=None):
    return graph.triples((s, p, o))


def subjects(graph: Graph, p=None, o=None):
    return graph.subjects(p, o)


def objects(graph: Graph, s=None, p=None):
    return graph.objects(s, p)


def value(graph: Graph, s, p):
    """One object of (s, p), or None."""
    return graph.value(s, p)


def has(graph: Graph, s=None, p=None, o=None) -> bool:
    return (s, p, o) in graph


def add(graph: Graph, s, p, o) -> None:
    graph.add((s, p, o))


def remove(graph: Graph, s=None, p=None, o=None) -> None:
    graph.remove((s, p, o))


def load(path: str | Path) -> Graph:
    """The map in the Turtle file at *path*."""
    return Graph().parse(str(path), format="turtle")


def save(graph: Graph, path: str | Path) -> None:
    """Write *graph* to *path* as Turtle."""
    graph.serialize(destination=str(path), format="turtle")


def query_rows(graph: Graph, query) -> list[dict[str, str]]:
    """The rows of a SELECT as {variable: text}, without the variables a row leaves unbound."""
    result = graph.query(query)
    return [{str(v): text(row[v]) for v in result.vars if row[v] is not None} for row in result]

# An import's kind (parser EdgeInfo.extra["import_kind"]) -> its (project file, outside module) predicates
_IMPORT_PREDICATES = {
    "module":      (CODE.importsFrom, CODE.importsExternal),
    "type-only":   (CODE.importsTypeOnly, CODE.importsTypeOnlyExternal),
    "in-function": (CODE.importsInFunction, CODE.importsInFunctionExternal),
    "main-block":  (CODE.importsInMainBlock, CODE.importsInMainBlockExternal),
}


class KnowledgeGraphBuilder:
    """Constructs an RDF Knowledge Graph from extracted AST nodes and edges.

    Usage pattern — single file:
        builder = KnowledgeGraphBuilder()
        builder.build(nodes, edges)
        graph = builder.graph

    Usage pattern — whole repository (many files):
        builder = KnowledgeGraphBuilder()
        for path in repo_files:
            result = parser.parse_file(path)
            builder.build(result.nodes, result.edges)   # accumulates across calls
        save(builder.graph, "graph.ttl")

    Call reset() between runs if you need a clean graph without creating a new
    instance (e.g. in tests or repeated ingestion jobs).
    """

    def __init__(self) -> None:
        self.graph = Graph()
        self.graph.bind("code", CODE)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, nodes: list[NodeInfo], edges: list[EdgeInfo], resolution: Resolution | None = None) -> Graph:
        """Translate NodeInfo / EdgeInfo objects, and the CallSites of *resolution* if given, into RDF triples.

        Imports and inheritance need the index of *resolution*; without it they are left out.

        Triples are *accumulated* into self.graph across repeated calls so that
        an entire repository can be ingested file-by-file. Call reset() first
        if you want to start from a clean slate.
        """
        for node in nodes:
            self._add_node(node)
        for edge in edges:
            self._add_edge(edge, resolution.index if resolution else None)
        for cls, bases in sorted(resolution.index.bases.items()) if resolution else []:
            for base in bases:
                if base in resolution.index.by_class:  # project bases only; "external" and "unknown" draw no arrow
                    self.graph.add((self._make_uri(cls), CODE.inherits, self._make_uri(base)))
        for call in resolution.calls if resolution else []:
            self._add_call(call)
        return self.graph

    def reset(self) -> None:
        """Clear all triples and start fresh, preserving namespace bindings."""
        self.graph = Graph()
        self.graph.bind("code", CODE)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _node_id(self, node: NodeInfo) -> str:
        """Return the canonical string identifier for a node (see NodeInfo.qualified_id)."""
        return node.qualified_id

    def _make_uri(self, identifier: str) -> URIRef:
        """Encode an arbitrary code identifier into a valid RDF URI."""
        # "#" is encoded: the namespace's "#" must stay the only one in the IRI.
        safe = urllib.parse.quote(identifier, safe="/:.@")
        return iri(CODE[safe])

    def _add_node(self, node: NodeInfo) -> None:
        uri = self._make_uri(self._node_id(node))

        # rdf:type
        type_map = {
            "Class":    CODE.Class,
            "Function": CODE.Function,
            "File":     CODE.File,
        }
        rdf_type = type_map.get(node.kind)
        if rdf_type:
            self.graph.add((uri, RDF.type, rdf_type))

        # Literal properties
        self.graph.add((uri, CODE.name,      lit(node.name)))
        self.graph.add((uri, CODE.filePath,  lit(node.file_path)))
        self.graph.add((uri, CODE.lineStart, lit(node.line_start)))
        self.graph.add((uri, CODE.lineEnd,   lit(node.line_end)))
        if node.decorator_line is not None:
            self.graph.add((uri, CODE.decoratorLine, lit(node.decorator_line)))

        if node.language:
            self.graph.add((uri, CODE.language, lit(node.language)))

    def _add_edge(self, edge: EdgeInfo, index: SymbolIndex | None = None) -> None:
        source_uri = self._make_uri(edge.source)

        if edge.kind == "CONTAINS":
            self.graph.add((source_uri, CODE.contains, self._make_uri(edge.target)))
        elif edge.kind == "IMPORTS_FROM" and index and edge.source.endswith(".py"):  # the index only knows Python modules
            files = index.module_files(edge.target, edge.source)
            project, external = _IMPORT_PREDICATES[edge.extra.get("import_kind", "module")]
            if len(files) == 1:
                self.graph.add((source_uri, project, self._make_uri(files[0])))
            elif not files and not edge.target.startswith("."):  # a relative import is inside the project, never external
                self.graph.add((source_uri, external, lit(edge.target)))
            # several files: ambiguous, so no arrow

    def _add_call(self, r: CallResolution) -> None:
        """One code:CallSite record, plus the calls / instantiates arrow when resolved (spec §7, D9, R11)."""
        call = r.call
        uri = self._make_uri(f"{call.caller_id}@L{call.line}c{call.col}")
        caller = self._make_uri(call.caller_id)
        g = self.graph
        g.add((uri, RDF.type, CODE.CallSite))
        g.add((uri, CODE.inFunction, caller))
        g.add((uri, CODE.line, lit(call.line)))
        g.add((uri, CODE.column, lit(call.col)))
        g.add((uri, CODE.targetName, lit(call.target_name)))
        if call.receiver is not None:
            g.add((uri, CODE.receiver, lit(call.receiver)))
        g.add((uri, CODE.status, lit(r.status)))
        if r.reason:
            g.add((uri, CODE.reason, lit(r.reason)))
        if r.status == "resolved":
            target = self._make_uri(r.target)
            g.add((uri, CODE.tier, lit(r.tier)))
            g.add((uri, CODE.resolvedTo, target))
            if r.init:
                g.add((uri, CODE.initTarget, self._make_uri(r.init)))
            if r.init_unknown:
                g.add((uri, CODE.initUnknown, lit(True)))
            if r.recursive:
                g.add((uri, CODE.recursive, lit(True)))
            else:  # R11: a recursive call keeps its record but draws no self-edge
                g.add((caller, CODE[r.edge], target))
        elif r.status == "ambiguous":
            g.add((uri, CODE.candidateCount, lit(r.candidate_count)))
            for c in r.candidates:
                g.add((uri, CODE.candidate, self._make_uri(c)))


# below this many files a process pool costs more than it saves
MIN_PARALLEL_FILES = 50
_worker_parser: CodeParser | None = None


def _parse_in_worker(path: Path) -> ParseResult:
    global _worker_parser
    if _worker_parser is None:  # set already when the worker was forked from the parent
        _worker_parser = CodeParser()
    return _worker_parser.parse_file(path)


def build_graph(paths: list[Path], root: Path, use_jedi: bool = True, jobs: int = 1) -> Graph:
    """The whole pipeline over *paths*: parse, resolve calls across all of them, build one graph.
    With *jobs* > 1 and enough files, files are parsed in that many processes; the results keep the order of *paths*."""
    global _worker_parser
    parser = CodeParser()
    if jobs <= 1 or len(paths) < MIN_PARALLEL_FILES:
        results = [parser.parse_file(p) for p in paths]
    else:
        # Load every grammar here first, so workers never race to download or cache one in HOME.
        for language in {parser.detect_language(p) for p in paths} - {None}:
            parser._get_parser(language)
        _worker_parser = parser
        try:
            # relative paths resolve against the working directory, so each worker starts in this one, whatever the start method
            with ProcessPoolExecutor(jobs, initializer=os.chdir, initargs=(os.getcwd(),)) as pool:
                results = list(pool.map(_parse_in_worker, paths, chunksize=max(1, len(paths) // (jobs * 4))))
        finally:
            _worker_parser = None
    nodes = [n for r in results for n in r.nodes]
    edges = [e for r in results for e in r.edges]
    return KnowledgeGraphBuilder().build(nodes, edges, resolve(results, root, use_jedi))
