"""
Graph analysis.
Reads a graph written by build and finds which definitions the most code depends on, where
file imports loop, and how the files group into subsystems. The graph is only read; no
result is added to it. Every result is sorted, so it is the same on every run.
"""

import posixpath
from collections import Counter
from dataclasses import dataclass

import networkx as nx
from rdflib import Graph

from acte.rdf_builder import text
from acte.sparql_engine import _IMPORTS, _PREFIXES, SPARQLEngine


@dataclass
class Ranked:
    id: str
    file_path: str
    line: int
    score: float
    callers: int  # distinct Functions and Files with a calls / instantiates arrow to it


@dataclass
class Ranking:
    top: list[Ranked]  # highest score first, ties by id
    total: int         # Functions and Classes ranked
    converged: bool    # False: PageRank stopped at its iteration limit, scores are the last iterate


@dataclass
class ImportCycle:
    files: list[str]  # sorted paths of the files that import each other in a loop


@dataclass
class Subsystem:
    hub: str          # its label: the file with the most link weight inside the subsystem, ties by path
    folder: str       # the common folder of its files, "." for the project root
    size: int
    files: list[str]  # up to 10: most link weight inside the subsystem first, then by path


def _pagerank(nodes: list[str], edges: list[tuple[str, str]], alpha: float = 0.85,
              max_iter: int = 100, tol: float = 1.0e-6) -> tuple[dict[str, float], bool]:
    """PageRank with networkx's defaults and semantics: uniform start and teleport, a node with no
    outgoing edge spreads its score evenly, converged when the summed change is under len(nodes) * tol."""
    if not nodes:
        return {}, True
    n = len(nodes)
    out: dict[str, list[str]] = {v: [] for v in nodes}
    for s, t in edges:
        out[s].append(t)
    dangling = [v for v in nodes if not out[v]]
    x = dict.fromkeys(nodes, 1.0 / n)
    for _ in range(max_iter):
        last = x
        x = dict.fromkeys(nodes, (alpha * sum(last[v] for v in dangling) + 1.0 - alpha) / n)
        for v in nodes:
            for t in out[v]:
                x[t] += alpha * last[v] / len(out[v])
        if sum(abs(x[v] - last[v]) for v in nodes) < n * tol:
            return x, True
    return x, False


def rank(graph: Graph, top: int = 10) -> Ranking:
    """PageRank over code:calls and code:instantiates arrows (caller to callee): the *top* Functions and Classes."""
    engine = SPARQLEngine(graph=graph)
    q = _PREFIXES + "SELECT DISTINCT ?s ?t WHERE { ?s code:calls|code:instantiates ?t . }"
    # DISTINCT: several calls from one caller to one target are one edge, as in a networkx DiGraph
    edges = sorted((engine._id(r["s"]), engine._id(r["t"])) for r in graph.query(q))
    defs = {d[0]: (d[3], d[4]) for d in engine.symbols()}
    nodes = sorted(set(defs) | {s for s, _ in edges} | {t for _, t in edges})  # File callers are nodes, never ranked
    score, converged = _pagerank(nodes, edges)
    callers: dict[str, set[str]] = {}
    for s, t in edges:
        callers.setdefault(t, set()).add(s)
    order = sorted(defs, key=lambda d: (-round(score[d], 12), d))  # rounded so float noise is no tie-break
    ranked = [Ranked(d, *defs[d], score[d], len(callers.get(d, ()))) for d in order[:top]]
    return Ranking(ranked, len(defs), converged)


def import_cycles(graph: Graph) -> list[ImportCycle]:
    """Strongly connected components of 2 or more files over code:importsFrom, largest first, then by files.
    Type-only, in-function and main-block imports are left out: they do not run when the module loads, so they make no loop."""
    imports = nx.DiGraph(sorted(_imports(graph, "code:importsFrom")))
    cycles = [sorted(c) for c in nx.strongly_connected_components(imports) if len(c) > 1]
    return [ImportCycle(c) for c in sorted(cycles, key=lambda c: (-len(c), c))]


def subsystems(graph: Graph) -> list[Subsystem]:
    """Louvain communities (seed 0) of the undirected file graph, largest first, then by files.

    The weight between two files is the number of resolved calls from one to the other, plus 1 if
    either imports the other. Calls inside one file and self-imports add nothing. A file with no
    link to another file is a subsystem of its own.
    """
    engine = SPARQLEngine(graph=graph)
    q = _PREFIXES + f"""
SELECT ?a ?b WHERE {{
    ?site code:status {engine._literal("resolved")} ;
          code:inFunction/code:filePath ?a ;
          code:resolvedTo/code:filePath ?b .
}}
"""
    weight = Counter(tuple(sorted((text(r["a"]), text(r["b"])))) for r in graph.query(q))  # one row per call site
    for pair in {tuple(sorted(p)) for p in _imports(graph)}:
        weight[pair] += 1
    files = nx.Graph()
    files.add_nodes_from(engine.files())
    files.add_weighted_edges_from((a, b, n) for (a, b), n in sorted(weight.items()) if a != b)
    # seeded and fed in sorted order, so fixed for one networkx install; another networkx version may split differently
    groups = sorted((sorted(c) for c in nx.community.louvain_communities(files, weight="weight", seed=0)),
                    key=lambda c: (-len(c), c))
    result = []
    for group in groups:
        inside = files.subgraph(group)
        order = sorted(group, key=lambda f: (-inside.degree(f, weight="weight"), f))
        folder = posixpath.commonpath([posixpath.dirname(f) for f in group]) or "."
        result.append(Subsystem(order[0], folder, len(group), order[:10]))
    return result


def _imports(graph: Graph, path: str = _IMPORTS) -> list[tuple[str, str]]:
    """(importer, imported) file paths of every import arrow between project files over the predicate *path*."""
    q = _PREFIXES + f"""
SELECT DISTINCT ?a ?b WHERE {{
    ?f {path} ?t .
    ?f code:filePath ?a .
    ?t code:filePath ?b .
}}
"""
    return [(text(r["a"]), text(r["b"])) for r in graph.query(q)]
