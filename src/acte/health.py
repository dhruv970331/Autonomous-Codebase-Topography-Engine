"""
Map health checks.
Named, hardcoded SPARQL SELECTs over a built graph. Each returns the rows that break
one rule of the graph's shape; a healthy graph returns none.
"""

from importlib.resources import files

from rdflib.plugins.sparql import prepareQuery

from acte.rdf_builder import (
    CODE,
    RDF,
    RDFS,
    Graph,
    has,
    is_blank,
    nt,
    objects,
    query_rows,
    subjects,
    text,
    triples,
)

_CODE_NS = "http://acte.local/code#"
_PREFIXES = f"PREFIX code: <{_CODE_NS}>\n"
_STATUSES = '"resolved", "ambiguous", "unresolved", "external"'
_STATUS_SET = {"resolved", "ambiguous", "unresolved", "external"}

_ONTOLOGY = Graph().parse(files("acte") / "ontology" / "code.ttl", format="turtle")
_DECLARED = sorted(set(subjects(_ONTOLOGY, RDF.type, RDFS.Class)) | set(subjects(_ONTOLOGY, RDF.type, RDF.Property)))
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
    return list(objects(graph, site, CODE.status))


def _one_known_status(graph: Graph, site) -> bool:
    statuses = _statuses(graph, site)
    return len(statuses) == 1 and text(statuses[0]) in _STATUS_SET


def _callers_known(graph: Graph, site) -> bool:
    callers = list(objects(graph, site, CODE.inFunction))
    return bool(callers) and all(has(graph, c, RDF.type, CODE.File) or has(graph, c, RDF.type, CODE.Function) for c in callers)


# The slow checks, as (variable, the nodes that might break the check): a superset found with triples() lookups, so
# the check's query only runs over those (not at all on a healthy graph). Each row is still the query's own.
_SUSPECTS = {
    "callsite-status": ("site", lambda g: [s for s in subjects(g, RDF.type, CODE.CallSite) if not _one_known_status(g, s)]),
    "resolved-without-target": ("site", lambda g: [
        s for s in subjects(g, RDF.type, CODE.CallSite)
        if any((text(st) == "resolved") != has(g, s, CODE.resolvedTo) for st in _statuses(g, s))]),
    "callsite-without-caller": ("site", lambda g: [
        s for s in subjects(g, RDF.type, CODE.CallSite) if not _callers_known(g, s)]),
    "undeclared-terms": ("term", lambda g: [
        t for t in {p for _, p, _ in triples(g)} | set(objects(g, None, RDF.type))
        if text(t).startswith(_CODE_NS) and t not in _DECLARED_SET]),
}


def _rows(graph: Graph, name: str) -> list[dict]:
    """The offending rows of check *name*."""
    query = _PREPARED[name]
    if name in _SUSPECTS:
        var, find = _SUSPECTS[name]
        suspects = set(find(graph))
        if not suspects:
            return []
        if not any(is_blank(n) for n in suspects):  # a blank node cannot be written in VALUES: run it all
            values = f"WHERE {{\n    VALUES ?{var} {{ {' '.join(sorted(nt(n) for n in suspects))} }}"
            query = _PREFIXES + CHECKS[name].replace("WHERE {", values, 1)
    return query_rows(graph, query)


def run_checks(graph: Graph) -> dict[str, list[dict]]:
    """Every check by name, in CHECKS order, with its offending rows ({variable: value as str}); [] is a pass."""
    return {name: _rows(graph, name) for name in CHECKS}
