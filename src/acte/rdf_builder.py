"""
RDF Knowledge Graph Builder.
Translates AST structural data (NodeInfo / EdgeInfo) into a formal Semantic Web
graph for SPARQL querying.
"""

import urllib.parse
from pathlib import Path
from typing import List

from rdflib import Graph, Literal, Namespace, URIRef
from rdflib.namespace import RDF, XSD

from acte.parser import NodeInfo, EdgeInfo

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
            nodes, edges = parser.parse_file(path)
            builder.build(nodes, edges)   # accumulates across calls
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

    def build(self, nodes: List[NodeInfo], edges: List[EdgeInfo]) -> Graph:
        """Translate NodeInfo / EdgeInfo objects into RDF triples.

        Triples are *accumulated* into self.graph across repeated calls so that
        an entire repository can be ingested file-by-file. Call reset() first
        if you want to start from a clean slate.
        """
        for node in nodes:
            self._add_node(node)
        for edge in edges:
            self._add_edge(edge)
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
        """Return the canonical string identifier for a node.

        This mirrors the parser's _qualify() logic so that node URIs align with
        the source / target strings already embedded in EdgeInfo objects:

          File     →  "<file_path>"                   (name IS the file path)
          Class    →  "<file_path>::<name>"
          Function →  "<file_path>::<parent>.<name>"  (if inside a class)
                   →  "<file_path>::<name>"            (module-level)
        """
        if node.kind == "File":
            return node.name  # NodeInfo.name == file_path_str for File nodes
        if node.parent_name:
            return f"{node.file_path}::{node.parent_name}.{node.name}"
        return f"{node.file_path}::{node.name}"

    def _make_uri(self, identifier: str) -> URIRef:
        """Encode an arbitrary code identifier into a valid RDF URI."""
        safe = urllib.parse.quote(identifier, safe="/:.#")
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