"""
SPARQL Query Engine.
Pre-built, parameterised SPARQL queries over the ACTE RDF knowledge graph.

Design principle: the LLM decides *which* query to run and *with what argument*.
It never writes SPARQL. Every named query is hardcoded and deterministic — that
is what separates ACTE from hallucination-prone RAG approaches.

Call shape (spec-001 §7)
------------------------
Every call is a ``code:CallSite`` record with a ``code:status``. Only a resolved
call also draws an arrow between real nodes: ``caller code:calls target`` for a
function, ``caller code:instantiates target`` for a class (D9). The caller is a
Function, or the File for module-level calls. A recursive call keeps its record
but draws no arrow (R11); ambiguous, unresolved and external calls draw none.

``callers_of`` and ``blast_radius`` take a qualified id, qualname or short name
(see ``callers_of``); the other queries take a short ``code:name``.
"""

import urllib.parse
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acte.rdf_builder import (
    CODE,
    RDF,
    Graph,
    has,
    iri,
    lit,
    new_graph,
    nt,
    objects,
    query_rows,
    subjects,
    text,
)

_CODE_NS = "http://acte.local/code#"
_PREFIXES = (
    f"PREFIX code: <{_CODE_NS}>\n"
    "PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>\n"
)
# every kind of import of a project file, and of an outside module (ontology/code.ttl)
_IMPORTS = "code:importsFrom|code:importsTypeOnly|code:importsInFunction|code:importsInMainBlock"
_IMPORTS_EXTERNAL = "code:importsExternal|code:importsTypeOnlyExternal|code:importsInFunctionExternal|code:importsInMainBlockExternal"
# the kind of each import predicate, and the order kinds other than "module" are listed in
IMPORT_KINDS = {_CODE_NS + p: kind for p, kind in {
    "importsFrom": "module",          "importsExternal": "module",
    "importsTypeOnly": "type-only",   "importsTypeOnlyExternal": "type-only",
    "importsInFunction": "in-function", "importsInFunctionExternal": "in-function",
    "importsInMainBlock": "main-block", "importsInMainBlockExternal": "main-block",
}.items()}
KIND_ORDER = ("type-only", "in-function", "main-block")
_MAX_ANSWERS = 6  # R23: per-definition answers for an ambiguous name
_MAX_DEPTH = 10  # D3: hard ceiling on blast_radius hops


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


@dataclass
class CallSiteResult:
    line: int
    column: int
    target_name: str
    receiver: str | None     # "db" in db.save()
    status: str              # "resolved" | "ambiguous" | "unresolved" | "external"
    resolved_to: str | None  # target id when resolved
    reason: str | None


@dataclass
class Caller:
    id: str
    name: str
    file_path: str
    line: int  # the call's line, not the caller's definition
    reason: str | None = None  # a possible caller's: "ambiguous", "override" (A8) or "unknown-base" (D9)


@dataclass
class CallerAnswer:
    target: str             # qualified id of the definition asked about
    callers: list[Caller]   # confirmed: resolved, non-recursive CallSites (calls or instantiates)
    possible: list[Caller]  # ambiguous CallSites listing target as a candidate (R19), calls to a method target overrides (A8),
                            # and, for C.__init__, creations of a subclass whose MRO stops at an outside base first (D9)
    completeness: str       # "exact" | "lower-bound" (R23)
    reasons: dict[str, int]  # why lower-bound: "ambiguous", "override", "unknown-receiver", "unknown-base", "ceiling" counts


@dataclass
class DependenciesAnswer:
    dependencies: list[str]      # project file paths and outside module names, sorted
    kinds: dict[str, list[str]]  # a dependency with no module-level import: its kinds, in KIND_ORDER


@dataclass
class Impact:
    id: str
    name: str
    file_path: str
    distance: int  # hops from the target (1 = direct caller)
    via: str       # id of the node one hop closer that it calls (first in rank order)
    line: int      # the line of its call into via (the first, as Caller.line)


@dataclass
class BlastAnswer:
    target: str
    affected: list[Impact]   # ranked D5: distance, in-degree desc, file, line_start
    possible: list[Caller]   # possible callers of any expanded node (R19), not followed
    total: int               # affected count before truncation
    truncated: bool
    depth: int               # depth actually used
    completeness: str        # "exact" | "lower-bound" (R23)
    reasons: dict[str, int]  # summed over expanded nodes, plus "depth-limit"


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

    def __init__(self, graph: Graph | None = None) -> None:
        self.graph: Graph = graph if graph is not None else new_graph()
        self._index: tuple | None = None  # (graph size, symbols, by id, by bare name, by qualname), see _indexed()

    @classmethod
    def from_turtle(cls, path: str | Path) -> "SPARQLEngine":
        """Load a serialised Turtle (.ttl) file and return a ready engine."""
        g = Graph()
        g.parse(str(path), format="turtle")
        return cls(graph=g)

    # ------------------------------------------------------------------
    # Named queries
    # ------------------------------------------------------------------

    def callers_of(self, name: str) -> list[CallerAnswer]:
        """Return who calls each definition matching *name*: confirmed callers, possible callers, and how complete that is.

        *name* is a qualified id; else, if dotted, a qualname (``Database.connect``,
        ``<locals>`` optional); else a bare ``code:name`` of any Function or Class,
        methods included. One answer per matching definition, most confirmed
        callers first, at most 6 (R23).
        """
        answers = [self._caller_answer(*d) for d in self._definitions(name)]
        answers.sort(key=lambda a: (-len(a.callers), a.target))
        return answers[:_MAX_ANSWERS]

    def blast_radius(self, name: str, depth: int = 3, limit: int = 50) -> list[BlastAnswer]:
        """Return everything that transitively calls or instantiates each definition matching *name*, ranked (D5).

        Stepped in Python one hop at a time (D3), at most *depth* hops (1..10).
        Each hop reuses ``_caller_answer``, so D9 and the lower-bound reasons
        carry over; possible callers are reported but not followed (R19). Name
        matching and the 6-answer cap are those of ``callers_of``.
        """
        depth = min(max(depth, 1), _MAX_DEPTH)
        answers = [self._blast(*d, depth, limit) for d in self._definitions(name)]
        answers.sort(key=lambda a: (-a.total, a.target))
        return answers[:_MAX_ANSWERS]

    def callees_of(self, function_name: str, file_path: str) -> list[str]:
        """Return the names of every function or class a call in *function_name* resolves to (itself too, if recursive).

        *file_path* is required to resolve the correct qualified URI when the
        same function name exists in multiple files.
        """
        source = self._uri(f"{file_path}::{function_name}")
        q = _PREFIXES + f"""
SELECT DISTINCT ?callee_name WHERE {{
    ?site code:inFunction {source} ;
          code:resolvedTo ?callee .
    ?callee code:name ?callee_name .
}}
ORDER BY ?callee_name
"""
        return [text(r["callee_name"]) for r in self.graph.query(q)]

    def call_sites(self, caller_id: str) -> list[CallSiteResult]:
        """Return every call made directly in the Function (or File) with id *caller_id*, by line and column."""
        q = _PREFIXES + f"""
SELECT ?line ?col ?target_name ?receiver ?status ?resolved ?reason WHERE {{
    ?site code:inFunction {self._uri(caller_id)} ;
          code:line       ?line ;
          code:column     ?col ;
          code:targetName ?target_name ;
          code:status     ?status .
    OPTIONAL {{ ?site code:receiver   ?receiver }}
    OPTIONAL {{ ?site code:resolvedTo ?resolved }}
    OPTIONAL {{ ?site code:reason     ?reason }}
}}
ORDER BY ?line ?col
"""
        return [
            CallSiteResult(int(text(r["line"])), int(text(r["col"])), text(r["target_name"]),
                           None if r["receiver"] is None else text(r["receiver"]), text(r["status"]),
                           None if r["resolved"] is None else self._id(r["resolved"]),
                           None if r["reason"] is None else text(r["reason"]))
            for r in self.graph.query(q)
        ]

    def functions_in_file(self, file_path: str) -> list[FunctionResult]:
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
            FunctionResult(text(r["name"]), file_path, int(text(r["start"])), int(text(r["end"])))
            for r in self.graph.query(q)
        ]

    def classes_in_file(self, file_path: str) -> list[ClassResult]:
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
            ClassResult(text(r["name"]), file_path, int(text(r["start"])), int(text(r["end"])))
            for r in self.graph.query(q)
        ]

    def dependencies_of(self, file_path: str) -> DependenciesAnswer:
        """Return what *file_path* imports, sorted: the path of each project file it imports,
        and the dotted name (``os.path``) of each module outside the project. Each one it never
        imports at module level gets its kinds of import ("type-only", "in-function", "main-block")."""
        file_uri = self._uri(file_path)
        q = _PREFIXES + f"""
SELECT DISTINCT ?dep ?p WHERE {{
    {{ {file_uri} ?p ?t . ?t code:filePath ?dep . VALUES ?p {{ {_IMPORTS.replace("|", " ")} }} }}
    UNION
    {{ {file_uri} ?p ?dep . VALUES ?p {{ {_IMPORTS_EXTERNAL.replace("|", " ")} }} }}
}}
"""
        found: dict[str, set[str]] = {}
        for r in self.graph.query(q):
            found.setdefault(text(r["dep"]), set()).add(IMPORT_KINDS[text(r["p"])])
        kinds = {d: [k for k in KIND_ORDER if k in ks] for d, ks in found.items() if "module" not in ks}
        return DependenciesAnswer(sorted(found), kinds)

    def orphan_functions(self) -> list[FunctionResult]:
        """Return functions that are never called by anything in the graph.

        Useful for dead-code detection. A function is an orphan when no
        calls / instantiates arrow and no resolved CallSite of another caller
        points to it. A function called only by itself is an orphan: its
        recursive CallSite does not count (R11).
        """
        q = _PREFIXES + """
SELECT DISTINCT ?name ?file ?start ?end WHERE {
    ?f a code:Function ;
       code:name      ?name ;
       code:filePath   ?file ;
       code:lineStart ?start ;
       code:lineEnd   ?end .
    FILTER NOT EXISTS { ?anything code:calls|code:instantiates ?f . }
    FILTER NOT EXISTS { ?site code:resolvedTo ?f ; code:inFunction ?caller . FILTER(?caller != ?f) }
}
ORDER BY ?file ?start
"""
        return [
            FunctionResult(text(r["name"]), text(r["file"]), int(text(r["start"])), int(text(r["end"])))
            for r in self.graph.query(q)
        ]

    def symbols(self) -> list[tuple[str, str, str, str, int]]:
        """Return (id, kind, bare name, file, lineStart) of every Function and Class, sorted by id."""
        return list(self._indexed()[1])

    def _symbols(self) -> list[tuple[str, str, str, str, int]]:
        q = _PREFIXES + """
SELECT ?d ?kind ?name ?file ?start WHERE {
    VALUES ?kind { code:Function code:Class }
    ?d a ?kind ;
       code:name      ?name ;
       code:filePath  ?file ;
       code:lineStart ?start .
}
"""
        return sorted(
            (self._id(r["d"]), self._id(r["kind"]), text(r["name"]), text(r["file"]), int(text(r["start"])))
            for r in self.graph.query(q)
        )

    def files(self) -> list[str]:
        """Return the path of every File in the graph, sorted."""
        q = _PREFIXES + """
SELECT ?path WHERE { ?f a code:File ; code:filePath ?path . }
ORDER BY ?path
"""
        return [text(r["path"]) for r in self.graph.query(q)]

    def coverage(self) -> dict:
        """Counts per status, tier (resolved only), reason and edge kind, plus total and
        rate = resolved / (calls - external): ``Resolution.report()`` from the graph alone,
        without the jedi fields only the resolver knows. A call's edge kind is the type
        of its target, so a recursive call counts too, as in ``report()``. Only calls in
        languages with call resolution are measured; no_resolver counts the others, total all."""
        # triples() lookups give one row per combination of values, as the OPTIONALs of the old query
        # (kept in the tests as an oracle) did: rdflib's SPARQL engine took 49 s for it on django
        g, edges = self.graph, {CODE.Function: "calls", CODE.Class: "instantiates"}
        everything = [(status, tier, reason, kind, path)
                      for site in subjects(g, RDF.type, CODE.CallSite) for status in objects(g, site, CODE.status)
                      for tier in list(objects(g, site, CODE.tier)) or [None]
                      for reason in list(objects(g, site, CODE.reason)) or [None]
                      for kind in [k for t in objects(g, site, CODE.resolvedTo) for k in objects(g, t, RDF.type)] or [None]
                      for path in [p for f in objects(g, site, CODE.inFunction) for p in objects(g, f, CODE.filePath)] or [None]]
        rows = [r for r in everything if text(r[4]).endswith(".py")]  # as the resolver: only Python is resolved

        def count(values):
            return dict(sorted(Counter(text(v) for v in values if v is not None).items()))
        status = count(r[0] for r in rows)
        measured = len(rows) - status.get("external", 0)
        return {
            "edge": count(edges.get(r[3]) for r in rows),
            "no_resolver": len(everything) - len(rows),
            "rate": status.get("resolved", 0) / measured if measured else None,
            "reason": count(r[2] for r in rows),
            "status": status,
            "tier": count(r[1] for r in rows),
            "total": len(everything),
        }

    def execute_raw_sparql(self, query: str) -> list[dict[str, Any]]:
        """Escape hatch for dynamic graph exploration.

        Intended for the MCP server's fallback tool and Colab experimentation —
        not for production LLM calls, where named queries are always preferred.
        Returns a list of row dicts, or a single-element list with an 'error'
        key if the query fails.
        """
        try:
            full_query = _PREFIXES + query if "PREFIX code:" not in query else query
            return query_rows(self.graph, full_query)
        except Exception as e:  # noqa: BLE001 - bad user SPARQL is reported, not raised
            return [{"error": str(e)}]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _definitions(self, name: str) -> list[tuple[str, str]]:
        """Return (id, bare name) of every Function / Class matching *name*.

        An exact id wins. Otherwise a dotted *name* is a qualname (``<locals>``
        optional) and an undotted one matches every definition with that bare name.
        """
        _, _, by_id, by_bare, by_qual = self._indexed()
        exact = by_id.get(name, [])
        if exact or "." not in name:
            return list(exact or by_bare.get(name, []))
        return list(by_qual.get(name.replace(".<locals>", ""), []))

    def _indexed(self) -> tuple:
        """The definitions index, built on first use and again when the graph has changed."""
        # change check: the triple count. A change that keeps the count the same is not seen; the
        # engine's users load a graph once and only read it.
        size = len(self.graph)
        if self._index is None or self._index[0] != size:
            g = self.graph  # triples() lookups: the same rows as a SPARQL query over every definition, several times faster
            defs = sorted((self._id(d), text(n)) for kind in (CODE.Function, CODE.Class)
                          for d in subjects(g, RDF.type, kind) for n in objects(g, d, CODE.name))
            by_id, by_bare, by_qual = defaultdict(list), defaultdict(list), defaultdict(list)
            for d in defs:  # each list keeps the sorted order
                by_id[d[0]].append(d)
                by_bare[d[1]].append(d)
                by_qual[d[0].split("::", 1)[-1].replace(".<locals>", "")].append(d)
            self._index = (size, self._symbols(), by_id, by_bare, by_qual)
        return self._index

    def _caller_answer(self, target: str, bare: str) -> CallerAnswer:
        """Confirmed callers, possible callers (R19) and the exact / lower-bound label (R23) of one definition."""
        reached, names, uri = [target], [bare], self._uri(target)
        if target.endswith(".__init__"):  # D9: an unsure creation of C may run C.__init__
            cls = target[: -len(".__init__")]
            reached.append(cls)
            names.append(cls.split("::", 1)[-1].rsplit(".", 1)[-1])
        values = " ".join(self._uri(t) for t in reached)
        # D9: creating a class runs the __init__ its MRO finds, recorded by the resolver as initTarget
        callers = self._callers(f"{{ ?site code:resolvedTo {uri} }} UNION {{ ?site code:initTarget {uri} }} "
                                "FILTER NOT EXISTS { ?site code:recursive true }")
        possible = self._callers(f"VALUES ?t {{ {values} }} ?site code:candidate ?t .", "ambiguous")
        # A8: a call resolved to a method target overrides, through self / cls, a variable typed as that class
        # or jedi, may run target when the object is of target's class
        # The sites are picked with triples(), each as often as the SPARQL pattern it replaces matched it: rdflib
        # evaluated the tier and receiver literals over every call site first (about 3 s per call on django).
        g = self.graph
        sites = [s for t in self._overridden(target) for s in subjects(g, CODE.resolvedTo, t) for tier in objects(g, s, CODE.tier)
                 for r in ([None] if tier in (lit("T4b"), lit("TJ")) else objects(g, s, CODE.receiver) if tier == lit("T1") else [])
                 if r is None or r in (lit("self"), lit("cls"))]
        override = self._callers(f"VALUES ?site {{ {' '.join(nt(s) for s in sites)} }}", "override") if sites else []
        if target.endswith(".__init__"):  # a subclass whose MRO reaches an outside or unknown base before any __init__
            possible += self._callers(f"?site code:initUnknown true ; code:resolvedTo/code:inherits+ {self._uri(cls)} .", "unknown-base")
        possible = sorted(possible + override, key=lambda c: (c.file_path, c.line, c.id))
        listed = Counter(c.reason for c in possible)
        reasons = {"ambiguous": listed["ambiguous"], "override": listed["override"]}
        # a method not found before an external or unknown base may be a project method after it in the MRO
        # (counted with triples(): SPARQL took 0.06 s per count)
        for status, reason in (("unresolved", "unknown-receiver"), ("unresolved", "unknown-base"), ("ambiguous", "ceiling")):
            reasons[reason] = listed[reason] + sum(
                1 for n in names for site in subjects(g, CODE.targetName, lit(n))
                if has(g, site, CODE.status, lit(status)) and has(g, site, CODE.reason, lit(reason)))
        reasons = {k: v for k, v in reasons.items() if v}
        return CallerAnswer(target, callers, possible, "lower-bound" if reasons else "exact", reasons)

    def _overridden(self, target: str) -> list:
        """Every Function of *target*'s name in a class its class reaches by ``code:inherits+``, other than *target*.

        Walked with triples() lookups: as one SPARQL pattern, rdflib evaluated ``code:inherits+``
        from every node in the graph (about 65 s on fastapi)."""
        g, uri = self.graph, iri(self._uri(target)[1:-1])
        found = []
        for sub in subjects(g, CODE.contains, uri):
            if not has(g, sub, RDF.type, CODE.Class):
                continue
            bases, todo = set(), [sub]  # one or more steps, as inherits+: sub itself only through a cycle
            while todo:
                for b in objects(g, todo.pop(), CODE.inherits):
                    if b not in bases:
                        bases.add(b)
                        todo.append(b)
            found += [t for m in objects(g, uri, CODE.name) for base in bases for t in objects(g, base, CODE.contains)
                      if t != uri and has(g, t, RDF.type, CODE.Function) and has(g, t, CODE.name, m)]
        return found

    def _blast(self, target: str, bare: str, depth: int, limit: int) -> BlastAnswer:
        """Breadth-first over confirmed callers of *target*; each node visited once, so cycles end."""
        answers: dict[str, CallerAnswer] = {}
        found: dict[str, Impact] = {}
        possible: dict[tuple[str, int], Caller] = {}  # shortcut: dedupes on (caller, line), so two unsure calls on one line count once
        reasons: dict[str, int] = {}
        names, line_start = {target: bare}, {}

        def answer(node: str) -> CallerAnswer:
            if node not in answers:
                answers[node] = self._caller_answer(node, names[node])
            return answers[node]

        def rank(i: Impact) -> tuple:  # D5; File nodes are never called, so their in-degree is 0
            return (i.distance, -len(answer(i.id).callers) if "::" in i.id else 0, i.file_path, line_start[i.id], i.id)

        level = [target]
        for distance in range(1, depth + 1):
            new = []
            for node in level:
                a = answer(node)
                possible.update({(c.id, c.line): c for c in a.possible})
                listed = Counter(c.reason for c in a.possible)  # counted once below, over the deduplicated list
                for reason in ("unknown-receiver", "unknown-base", "ceiling"):
                    if a.reasons.get(reason, 0) - listed[reason]:
                        reasons[reason] = reasons.get(reason, 0) + a.reasons[reason] - listed[reason]
                for c in a.callers:
                    if c.id != target and c.id not in found:
                        names[c.id] = c.name
                        found[c.id] = Impact(c.id, c.name, c.file_path, distance, node, c.line)
                        new.append(found[c.id])
            line_start |= self._line_starts([i.id for i in new])
            level = [i.id for i in sorted(new, key=rank) if "::" in i.id]  # File nodes are not expanded
        for reason, n in sorted(Counter(c.reason for c in possible.values()).items()):
            reasons[reason] = reasons.get(reason, 0) + n
        cut = sum(1 for n in level if answer(n).possible or answer(n).reasons
                  or any(c.id != target and c.id not in found for c in answer(n).callers))
        if cut:
            reasons["depth-limit"] = cut
        affected = sorted(found.values(), key=rank)
        return BlastAnswer(target, affected[:limit], sorted(possible.values(), key=lambda c: (c.file_path, c.line, c.id)),
                           len(affected), len(affected) > limit, depth, "lower-bound" if reasons else "exact", reasons)

    def _line_starts(self, ids: list[str]) -> dict[str, int]:
        """Return the lineStart of each node in *ids*: the smallest, for an id defined more than once."""
        if not ids:
            return {}
        # MIN: an id defined twice (both branches of an if) has two lineStarts, and the row order depends on the store
        q = _PREFIXES + f"""
SELECT ?n (MIN(?s) AS ?start) WHERE {{
    VALUES ?n {{ {" ".join(self._uri(i) for i in ids)} }}
    ?n code:lineStart ?s .
}}
GROUP BY ?n
"""
        return {self._id(r["n"]): int(text(r["start"])) for r in self.graph.query(q)}

    def _callers(self, site_pattern: str, reason: str | None = None) -> list[Caller]:
        """One Caller per CallSite matching *site_pattern* (which binds ?site), by file and line, each with *reason*."""
        q = _PREFIXES + f"""
SELECT ?caller ?name ?file ?line WHERE {{
    {site_pattern}
    ?site code:inFunction ?caller ;
          code:line       ?line .
    ?caller code:name     ?name ;
            code:filePath ?file .
}}
ORDER BY ?file ?line ?caller
"""
        return [
            Caller(self._id(r["caller"]), text(r["name"]), text(r["file"]), int(text(r["line"])), reason)
            for r in self.graph.query(q)
        ]

    def _id(self, uri: Any) -> str:
        """Return the code identifier a node URI encodes (inverse of ``_uri``)."""
        return urllib.parse.unquote(text(uri)[len(_CODE_NS):])

    def _uri(self, identifier: str) -> str:
        """Return a SPARQL inline URI for *identifier*, e.g. ``<code:foo>``."""
        # "#" is encoded: the namespace's "#" must stay the only one in the IRI.
        safe = urllib.parse.quote(identifier, safe="/:.@")
        return f"<{_CODE_NS}{safe}>"

    def _literal(self, value: str) -> str:
        """Return an xsd:string typed SPARQL literal.

        The ``^^xsd:string`` annotation is required because rdflib stores
        Literal(..., datatype=XSD.string) as typed literals. A plain ``"foo"``
        in SPARQL does not match ``"foo"^^xsd:string`` in rdflib's query engine.
        """
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"^^xsd:string'