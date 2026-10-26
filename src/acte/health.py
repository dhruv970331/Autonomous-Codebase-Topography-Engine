"""
Map health checks.
Named, hardcoded SPARQL SELECTs over a built graph. Each returns the rows that break
one rule of the graph's shape; a healthy graph returns none.
"""

from importlib.resources import files

from rdflib import BNode, Graph, Namespace
from rdflib.namespace import RDF, RDFS
from rdflib.plugins.sparql import prepareQuery

_CODE_NS = "http://acte.local/code#"
_CODE = Namespace(_CODE_NS)
_PREFIXES = f"PREFIX code: <{_CODE_NS}>\n"
_STATUSES = '"resolved", "ambiguous", "unresolved", "external"'
_STATUS_SET = {"resolved", "ambiguous", "unresolved", "external"}

_ONTOLOGY = Graph().parse(files("acte") / "ontology" / "code.ttl", format="turtle")
_DECLARED = sorted(set(_ONTOLOGY.subjects(RDF.type, RDFS.Class)) | set(_ONTOLOGY.subjects(RDF.type, RDF.Property)))
_DECLARED_SET = set(_DECLARED)

CHECKS: dict[str, str] = {
    # a calls arrow must end on a Function, an instantiates arrow on a Class
    "dangling-call-arrows": """
SELECT ?s ?p ?o WHERE {
    VALUES (?p ?type) { (code:calls code:Function) (code:instantiates code:Class) }
    ?s ?p ?o .
    FILTER NOT EXISTS { ?o a ?type }
}
ORDER BY ?s ?p ?o
""",
    "dangling-import-arrows": """
SELECT DISTINCT ?s ?o WHERE {
    ?s code:importsFrom|code:importsTypeOnly|code:importsInFunction|code:importsInMainBlock ?o .
    FILTER NOT EXISTS { ?o a code:File }
}
ORDER BY ?s ?o
""",
    "dangling-inherits-arrows": """
SELECT ?s ?o WHERE {
    ?s code:inherits ?o .
    FILTER NOT EXISTS { ?o a code:Class }
}
ORDER BY ?s ?o
""",
    # a recursive call keeps its CallSite but draws no arrow to itself
    "self-call-arrows": """
SELECT ?s ?p WHERE {
    VALUES ?p { code:calls code:instantiates }
    ?s ?p ?s .
}
ORDER BY ?s ?p
""",
    # one row per missing, unknown or extra status
    "callsite-status": f"""
SELECT ?site ?status WHERE {{
    ?site a code:CallSite .
    OPTIONAL {{ ?site code:status ?status }}
    FILTER (!BOUND(?status) || STR(?status) NOT IN ({_STATUSES})
            || EXISTS {{ ?site code:status ?other FILTER (?other != ?status) }})
}}
ORDER BY ?site ?status
""",
    "resolved-without-target": """
SELECT ?site ?status WHERE {
    ?site a code:CallSite ;
          code:status ?status .
    FILTER ((STR(?status) = "resolved") != EXISTS { ?site code:resolvedTo ?target })
}
ORDER BY ?site ?status
""",
    "callsite-without-caller": """
SELECT ?site ?caller WHERE {
    ?site a code:CallSite .
    OPTIONAL { ?site code:inFunction ?caller }
    FILTER (!BOUND(?caller) || (NOT EXISTS { ?caller a code:File } && NOT EXISTS { ?caller a code:Function }))
}
ORDER BY ?site ?caller
""",
    # every type and predicate in the code namespace must be declared in ontology/code.ttl
    "undeclared-terms": f"""
SELECT DISTINCT ?term WHERE {{
    {{ ?s a ?term }} UNION {{ ?s ?term ?o }}
    FILTER (STRSTARTS(STR(?term), "{_CODE_NS}"))
    FILTER (?term NOT IN ({", ".join(f"<{t}>" for t in _DECLARED)}))
}}
ORDER BY ?term
""",
}


_PREPARED = {name: prepareQuery(_PREFIXES + q) for name, q in CHECKS.items()}  # parsing dominates on small graphs


def _statuses(graph: Graph, site) -> list:
    return list(graph.objects(site, _CODE.status))


def _one_known_status(graph: Graph, site) -> bool:
    statuses = _statuses(graph, site)
    return len(statuses) == 1 and str(statuses[0]) in _STATUS_SET


def _callers_known(graph: Graph, site) -> bool:
    callers = list(graph.objects(site, _CODE.inFunction))
    return bool(callers) and all((c, RDF.type, _CODE.File) in graph or (c, RDF.type, _CODE.Function) in graph for c in callers)


# The slow checks, as (variable, the nodes that might break the check): a superset found with triples() lookups, so
# the check's query only runs over those (not at all on a healthy graph). Each row is still the query's own.
_SUSPECTS = {
    "callsite-status": ("site", lambda g: [s for s in g.subjects(RDF.type, _CODE.CallSite) if not _one_known_status(g, s)]),
    "resolved-without-target": ("site", lambda g: [
        s for s in g.subjects(RDF.type, _CODE.CallSite)
        if any((str(st) == "resolved") != ((s, _CODE.resolvedTo, None) in g) for st in _statuses(g, s))]),
    "callsite-without-caller": ("site", lambda g: [
        s for s in g.subjects(RDF.type, _CODE.CallSite) if not _callers_known(g, s)]),
    "undeclared-terms": ("term", lambda g: [
        t for t in set(g.predicates()) | set(g.objects(None, RDF.type))
        if str(t).startswith(_CODE_NS) and t not in _DECLARED_SET]),
}


def _rows(graph: Graph, name: str) -> list[dict]:
    """The offending rows of check *name*."""
    query = _PREPARED[name]
    if name in _SUSPECTS:
        var, find = _SUSPECTS[name]
        suspects = set(find(graph))
        if not suspects:
            return []
        if not any(isinstance(n, BNode) for n in suspects):  # a blank node cannot be written in VALUES: run it all
            values = f"WHERE {{\n    VALUES ?{var} {{ {' '.join(sorted(n.n3() for n in suspects))} }}"
            query = _PREFIXES + CHECKS[name].replace("WHERE {", values, 1)
    return [{k: str(v) for k, v in row.asdict().items()} for row in graph.query(query)]


def run_checks(graph: Graph) -> dict[str, list[dict]]:
    """Every check by name, in CHECKS order, with its offending rows ({variable: value as str}); [] is a pass."""
    return {name: _rows(graph, name) for name in CHECKS}
