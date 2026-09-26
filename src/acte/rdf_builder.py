"""
RDF Knowledge Graph Builder.
Translates AST structural data (NodeInfo / EdgeInfo) into a formal Semantic Web
graph for SPARQL querying.
"""

import urllib.parse
from pathlib import Path

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from acte.parser import CodeParser, EdgeInfo, NodeInfo
from acte.resolver import CallResolution, Resolution, resolve

# Formal namespace for the codebase topography ontology
CODE = Namespace("http://acte.local/code#")


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
        builder.serialize("graph.ttl")

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

        Triples are *accumulated* into self.graph across repeated calls so that
        an entire repository can be ingested file-by-file. Call reset() first
        if you want to start from a clean slate.
        """
        for node in nodes:
            self._add_node(node)
        for edge in edges:
            self._add_edge(edge)
        for call in resolution.calls if resolution else []:
            self._add_call(call)
        return self.graph

    def reset(self) -> None:
        """Clear all triples and start fresh, preserving namespace bindings."""
        self.graph = Graph()
        self.graph.bind("code", CODE)

    def serialize(self, filepath: str | Path, format: str = "turtle") -> None:
        """Export the graph to disk (default: Turtle)."""
        self.graph.serialize(destination=str(filepath), format=format)

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
        return URIRef(CODE[safe])

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
        self.graph.add((uri, CODE.name,      Literal(node.name,      datatype=XSD.string)))
        self.graph.add((uri, CODE.filePath,  Literal(node.file_path, datatype=XSD.string)))
        self.graph.add((uri, CODE.lineStart, Literal(node.line_start, datatype=XSD.integer)))
        self.graph.add((uri, CODE.lineEnd,   Literal(node.line_end,   datatype=XSD.integer)))

        if node.language:
            self.graph.add((uri, CODE.language, Literal(node.language, datatype=XSD.string)))

    def _add_edge(self, edge: EdgeInfo) -> None:
        source_uri = self._make_uri(edge.source)
        target_uri = self._make_uri(edge.target)

        predicate_map = {
            "CONTAINS":    CODE.contains,
            "CALLS":       CODE.calls,
            "IMPORTS_FROM": CODE.importsFrom,
            "INHERITS":    CODE.inherits,
        }
        predicate = predicate_map.get(edge.kind)
        if predicate:
            self.graph.add((source_uri, predicate, target_uri))

    def _add_call(self, r: CallResolution) -> None:
        """One code:CallSite record, plus the calls / instantiates arrow when resolved (spec §7, D9, R11)."""
        call = r.call
        uri = self._make_uri(f"{call.caller_id}@L{call.line}c{call.col}")
        caller = self._make_uri(call.caller_id)
        g = self.graph
        g.add((uri, RDF.type, CODE.CallSite))
        g.add((uri, CODE.inFunction, caller))
        g.add((uri, CODE.line, Literal(call.line, datatype=XSD.integer)))
        g.add((uri, CODE.column, Literal(call.col, datatype=XSD.integer)))
        g.add((uri, CODE.targetName, Literal(call.target_name, datatype=XSD.string)))
        if call.receiver is not None:
            g.add((uri, CODE.receiver, Literal(call.receiver, datatype=XSD.string)))
        g.add((uri, CODE.status, Literal(r.status, datatype=XSD.string)))
        if r.reason:
            g.add((uri, CODE.reason, Literal(r.reason, datatype=XSD.string)))
        if r.status == "resolved":
            target = self._make_uri(r.target)
            g.add((uri, CODE.tier, Literal(r.tier, datatype=XSD.string)))
            g.add((uri, CODE.resolvedTo, target))
            if r.recursive:
                g.add((uri, CODE.recursive, Literal(True, datatype=XSD.boolean)))
            else:  # R11: a recursive call keeps its record but draws no self-edge
                g.add((caller, CODE[r.edge], target))
        elif r.status == "ambiguous":
            g.add((uri, CODE.candidateCount, Literal(r.candidate_count, datatype=XSD.integer)))
            for c in r.candidates:
                g.add((uri, CODE.candidate, self._make_uri(c)))


def build_graph(paths: list[Path], root: Path, use_jedi: bool = True) -> Graph:
    """The whole pipeline over *paths*: parse, resolve calls across all of them, build one graph."""
    parser = CodeParser()
    results = [parser.parse_file(p) for p in paths]
    builder = KnowledgeGraphBuilder()
    for result in results:
        builder.build(result.nodes, result.edges)
    return builder.build([], [], resolve(results, root, use_jedi))
