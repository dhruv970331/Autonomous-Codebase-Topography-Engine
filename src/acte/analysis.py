"""
Graph analysis.
Reads a graph written by build and finds where file imports loop. The graph is only
read; no result is added to it. Every result is sorted, so it is the same on every run.
"""

from dataclasses import dataclass

import networkx as nx
from rdflib import Graph

from acte.sparql_engine import _PREFIXES


@dataclass
class ImportCycle:
    files: list[str]  # sorted paths of the files that import each other in a loop


def import_cycles(graph: Graph) -> list[ImportCycle]:
    """Strongly connected components of 2 or more files over code:importsFrom, largest first, then by files."""
    q = _PREFIXES + """
SELECT ?a ?b WHERE {
    ?f code:importsFrom ?t .
    ?f code:filePath ?a .
    ?t code:filePath ?b .
}
"""
    imports = nx.DiGraph(sorted((str(r.a), str(r.b)) for r in graph.query(q)))
    cycles = [sorted(c) for c in nx.strongly_connected_components(imports) if len(c) > 1]
    return [ImportCycle(c) for c in sorted(cycles, key=lambda c: (-len(c), c))]
