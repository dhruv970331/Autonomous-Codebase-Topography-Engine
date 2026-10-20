"""
Map health checks.
Named, hardcoded SPARQL SELECTs over a built graph. Each returns the rows that break
one rule of the graph's shape; a healthy graph returns none.
"""

from importlib.resources import files

from rdflib import Graph
from rdflib.namespace import RDF, RDFS
from rdflib.plugins.sparql import prepareQuery

_CODE_NS = "http://acte.local/code#"
_PREFIXES = f"PREFIX code: <{_CODE_NS}>\n"
_STATUSES = '"resolved", "ambiguous", "unresolved", "external"'

_ONTOLOGY = Graph().parse(files("acte") / "ontology" / "code.ttl", format="turtle")
_DECLARED = sorted(set(_ONTOLOGY.subjects(RDF.type, RDFS.Class)) | set(_ONTOLOGY.subjects(RDF.type, RDF.Property)))

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
SELECT ?s ?o WHERE {
    ?s code:importsFrom ?o .
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


def run_checks(graph: Graph) -> dict[str, list[dict]]:
    """Every check by name, in CHECKS order, with its offending rows ({variable: value as str}); [] is a pass."""
    return {
        name: [{k: str(v) for k, v in row.asdict().items()} for row in graph.query(q)]
        for name, q in _PREPARED.items()
    }
