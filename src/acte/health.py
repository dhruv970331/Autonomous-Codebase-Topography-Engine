"""
Map health checks.
Named, hardcoded SPARQL SELECTs over a built graph. Each returns the rows that break
one rule of the graph's shape; a healthy graph returns none.
"""

from collections import defaultdict
from importlib.resources import files

from acte.rdf_builder import (
    CODE,
    RDF,
    RDFS,
    Graph,
    has,
    is_blank,
    load,
    nt,
    objects,
    query_rows,
    subjects,
    text,
    triples,
)
from acte.sparql_engine import coverage_counts

_CODE_NS = "http://acte.local/code#"
_PREFIXES = f"PREFIX code: <{_CODE_NS}>\n"
_STATUSES = '"resolved", "ambiguous", "unresolved", "external"'
_STATUS_SET = {"resolved", "ambiguous", "unresolved", "external"}

_ONTOLOGY = load(files("acte") / "ontology" / "code.ttl")
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
    FILTER (?term NOT IN ({", ".join(nt(t) for t in _DECLARED)}))
}}
ORDER BY ?term
""",
}


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


def _rows(graph: Graph, name: str, suspects: set | None = None) -> list[dict]:
    """The offending rows of check *name*; *suspects*: a slow check's suspects (_SUSPECTS), if already found."""
    query = _PREFIXES + CHECKS[name]
    if name in _SUSPECTS:
        var, find = _SUSPECTS[name]
        suspects = set(find(graph)) if suspects is None else suspects
        if not suspects:
            return []
        if not any(is_blank(n) for n in suspects):  # a blank node cannot be written in VALUES: run it all
            values = f"WHERE {{\n    VALUES ?{var} {{ {' '.join(sorted(nt(n) for n in suspects))} }}"
            query = _PREFIXES + CHECKS[name].replace("WHERE {", values, 1)
    return query_rows(graph, query)


def run_checks(graph: Graph) -> dict[str, list[dict]]:
    """Every check by name, in CHECKS order, with its offending rows ({variable: value as str}); [] is a pass."""
    return {name: _rows(graph, name) for name in CHECKS}


_ARROWS = {CODE.calls: CODE.Function, CODE.instantiates: CODE.Class}  # an arrow and the type its end must have
_IMPORT_ARROWS = {CODE.importsFrom, CODE.importsTypeOnly, CODE.importsInFunction, CODE.importsInMainBlock}


def scan(graph: Graph) -> tuple[dict, dict, dict[str, list[dict]]]:
    """The file, function, class and call-site counts, the coverage and every check of *graph*, as cli._counts and
    run_checks give them, from one pass over its facts: on a database folder, where each lookup reads the disk, many
    times faster than their lookups. A check whose rule a fact may break runs its own query for the rows."""
    types = defaultdict(set)
    values = {p: defaultdict(list) for p in (CODE.status, CODE.tier, CODE.reason, CODE.resolvedTo, CODE.inFunction, CODE.filePath)}
    links = {p: [] for p in (*_ARROWS, *_IMPORT_ARROWS, CODE.inherits)}  # predicate -> its (subject, object) pairs
    predicates = set()
    for s, p, o in triples(graph):  # one dict lookup per fact in most cases: this loop is most of the time
        predicates.add(p)
        if (found := values.get(p)) is not None:
            found[s].append(o)
        elif p == RDF.type:
            types[s].add(o)
        elif (pairs := links.get(p)) is not None:
            pairs.append((s, o))
    arrows = [(s, p, o) for p in _ARROWS for s, o in links[p]]
    imports = [pair for p in _IMPORT_ARROWS for pair in links[p]]
    inherits = links[CODE.inherits]
    status, tier, reason, target, caller, path = (values[p] for p in (
        CODE.status, CODE.tier, CODE.reason, CODE.resolvedTo, CODE.inFunction, CODE.filePath))
    sites = [s for s, ts in types.items() if CODE.CallSite in ts]
    # one row per combination of values, as SPARQLEngine.coverage() makes them with lookups
    everything = [(st, ti, re, kind, fp) for s in sites for st in status.get(s, ())
                  for ti in tier.get(s) or [None] for re in reason.get(s) or [None]
                  for kind in [k for t in target.get(s, ()) for k in types.get(t, ())] or [None]
                  for fp in [x for f in caller.get(s, ()) for x in path.get(f, ())] or [None]]
    cov = coverage_counts(everything)
    counts = {key: sum(1 for ts in types.values() if kind in ts)
              for key, kind in (("files", CODE.File), ("functions", CODE.Function), ("classes", CODE.Class))}
    counts["call_sites"] = cov["total"]
    # what may break each check: the same nodes _SUSPECTS finds, or the facts the check's query looks at
    suspects = {
        "dangling-call-arrows": [a for a in arrows if _ARROWS[a[1]] not in types.get(a[2], ())],
        "dangling-import-arrows": [a for a in imports if CODE.File not in types.get(a[1], ())],
        "dangling-inherits-arrows": [a for a in inherits if CODE.Class not in types.get(a[1], ())],
        "self-call-arrows": [a for a in arrows if a[0] == a[2]],
        "callsite-status": [s for s in sites if len(status.get(s, ())) != 1 or text(status[s][0]) not in _STATUS_SET],
        "resolved-without-target": [s for s in sites if any((text(st) == "resolved") != (s in target) for st in status.get(s, ()))],
        "callsite-without-caller": [s for s in sites if not caller.get(s) or not all(
            CODE.File in types.get(c, ()) or CODE.Function in types.get(c, ()) for c in caller[s])],
        "undeclared-terms": [t for t in predicates | {o for ts in types.values() for o in ts}
                             if text(t).startswith(_CODE_NS) and t not in _DECLARED_SET],
    }
    checks = {name: _rows(graph, name, set(suspects[name])) if suspects[name] else [] for name in CHECKS}
    return counts, cov, checks
