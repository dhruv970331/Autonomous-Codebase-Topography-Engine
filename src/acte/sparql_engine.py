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
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rdflib import Graph

_CODE_NS = "http://acte.local/code#"
_PREFIXES = (
    f"PREFIX code: <{_CODE_NS}>\n"
    "PREFIX xsd:  <http://www.w3.org/2001/XMLSchema#>\n"
)
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


@dataclass
class CallerAnswer:
    target: str             # qualified id of the definition asked about
    callers: list[Caller]   # confirmed: resolved, non-recursive CallSites (calls or instantiates)
    possible: list[Caller]  # ambiguous CallSites listing target as a candidate (R19)
    completeness: str       # "exact" | "lower-bound" (R23)
    reasons: dict[str, int]  # why lower-bound: "ambiguous", "unknown-receiver", "unknown-base", "ceiling" counts


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
        self.graph: Graph = graph if graph is not None else Graph()

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
        return [str(r.callee_name) for r in self.graph.query(q)]

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
            CallSiteResult(int(r.line), int(r.col), str(r.target_name),
                           None if r.receiver is None else str(r.receiver), str(r.status),
                           None if r.resolved is None else self._id(r.resolved),
                           None if r.reason is None else str(r.reason))
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
            FunctionResult(str(r.name), file_path, int(r.start), int(r.end))
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
            ClassResult(str(r.name), file_path, int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def dependencies_of(self, file_path: str) -> list[str]:
        """Return what *file_path* imports, sorted: the path of each project file it imports,
        and the dotted name (``os.path``) of each module outside the project."""
        file_uri = self._uri(file_path)
        q = _PREFIXES + f"""
SELECT DISTINCT ?dep WHERE {{
    {{ {file_uri} code:importsFrom/code:filePath ?dep }}
    UNION
    {{ {file_uri} code:importsExternal ?dep }}
}}
ORDER BY ?dep
"""
        return [str(r.dep) for r in self.graph.query(q)]

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
            FunctionResult(str(r.name), str(r.file), int(r.start), int(r.end))
            for r in self.graph.query(q)
        ]

    def symbols(self) -> list[tuple[str, str, str, str, int]]:
        """Return (id, kind, bare name, file, lineStart) of every Function and Class, sorted by id."""
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
            (self._id(r.d), self._id(r.kind), str(r.name), str(r.file), int(r.start))
            for r in self.graph.query(q)
        )

    def files(self) -> list[str]:
        """Return the path of every File in the graph, sorted."""
        q = _PREFIXES + """
SELECT ?path WHERE { ?f a code:File ; code:filePath ?path . }
ORDER BY ?path
"""
        return [str(r.path) for r in self.graph.query(q)]

    def coverage(self) -> dict:
        """Counts per status, tier (resolved only), reason and edge kind, plus total and
        rate = resolved / (total - external): ``Resolution.report()`` from the graph alone,
        without the jedi fields only the resolver knows. A call's edge kind is the type
        of its target, so a recursive call counts too, as in ``report()``."""
        q = _PREFIXES + """
SELECT ?site ?status ?tier ?reason ?kind WHERE {
    ?site a code:CallSite ;
          code:status ?status .
    OPTIONAL { ?site code:tier ?tier }
    OPTIONAL { ?site code:reason ?reason }
    OPTIONAL { ?site code:resolvedTo/a ?kind }
}
"""
        edges = {f"{_CODE_NS}Function": "calls", f"{_CODE_NS}Class": "instantiates"}
        rows = list(self.graph.query(q))

        def count(values):
            return dict(sorted(Counter(str(v) for v in values if v is not None).items()))
        status = count(r.status for r in rows)
        measured = len(rows) - status.get("external", 0)
        return {
            "edge": count(edges.get(str(r.kind)) for r in rows),
            "rate": status.get("resolved", 0) / measured if measured else None,
            "reason": count(r.reason for r in rows),
            "status": status,
            "tier": count(r.tier for r in rows),
            "total": len(rows),
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
            return [
                {str(var): str(val) for var, val in row.asdict().items()}
                for row in self.graph.query(full_query)
            ]
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
        q = _PREFIXES + """
SELECT ?d ?name WHERE {
    VALUES ?kind { code:Function code:Class }
    ?d a ?kind ; code:name ?name .
}
"""
        defs = sorted((self._id(r.d), str(r.name)) for r in self.graph.query(q))
        exact = [d for d in defs if d[0] == name]
        if exact or "." not in name:
            return exact or [d for d in defs if d[1] == name]
        plain = name.replace(".<locals>", "")
        return [d for d in defs if d[0].split("::", 1)[-1].replace(".<locals>", "") == plain]

    def _caller_answer(self, target: str, bare: str) -> CallerAnswer:
        """Confirmed callers, possible callers (R19) and the exact / lower-bound label (R23) of one definition."""
        reached, names = [target], [bare]
        if target.endswith(".__init__"):  # D9: whoever instantiates C runs C.__init__
            cls = target[: -len(".__init__")]
            reached.append(cls)
            names.append(cls.split("::", 1)[-1].rsplit(".", 1)[-1])
        values = " ".join(self._uri(t) for t in reached)
        callers = self._callers(f"VALUES ?t {{ {values} }} ?site code:resolvedTo ?t . "
                                "FILTER NOT EXISTS { ?site code:recursive true }")
        possible = self._callers(f"VALUES ?t {{ {values} }} ?site code:candidate ?t .")
        reasons = {"ambiguous": len(possible)}
        # a method not found before an external or unknown base may be a project method after it in the MRO
        for status, reason in (("unresolved", "unknown-receiver"), ("unresolved", "unknown-base"), ("ambiguous", "ceiling")):
            q = _PREFIXES + f"""
SELECT (COUNT(?site) AS ?n) WHERE {{
    VALUES ?target_name {{ {" ".join(self._literal(n) for n in names)} }}
    ?site code:status {self._literal(status)} ;
          code:reason {self._literal(reason)} ;
          code:targetName ?target_name .
}}
"""
            reasons[reason] = int(next(iter(self.graph.query(q))).n)
        reasons = {k: v for k, v in reasons.items() if v}
        return CallerAnswer(target, callers, possible, "lower-bound" if reasons else "exact", reasons)

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
                for reason in ("unknown-receiver", "unknown-base", "ceiling"):
                    if a.reasons.get(reason):
                        reasons[reason] = reasons.get(reason, 0) + a.reasons[reason]
                for c in a.callers:
                    if c.id != target and c.id not in found:
                        names[c.id] = c.name
                        found[c.id] = Impact(c.id, c.name, c.file_path, distance, node, c.line)
                        new.append(found[c.id])
            line_start |= self._line_starts([i.id for i in new])
            level = [i.id for i in sorted(new, key=rank) if "::" in i.id]  # File nodes are not expanded
        if possible:
            reasons["ambiguous"] = len(possible)
        cut = sum(1 for n in level if answer(n).possible or answer(n).reasons
                  or any(c.id != target and c.id not in found for c in answer(n).callers))
        if cut:
            reasons["depth-limit"] = cut
        affected = sorted(found.values(), key=rank)
        return BlastAnswer(target, affected[:limit], sorted(possible.values(), key=lambda c: (c.file_path, c.line, c.id)),
                           len(affected), len(affected) > limit, depth, "lower-bound" if reasons else "exact", reasons)

    def _line_starts(self, ids: list[str]) -> dict[str, int]:
        """Return the lineStart of each node in *ids*."""
        if not ids:
            return {}
        q = _PREFIXES + f"""
SELECT ?n ?start WHERE {{
    VALUES ?n {{ {" ".join(self._uri(i) for i in ids)} }}
    ?n code:lineStart ?start .
}}
"""
        return {self._id(r.n): int(r.start) for r in self.graph.query(q)}

    def _callers(self, site_pattern: str) -> list[Caller]:
        """One Caller per CallSite matching *site_pattern* (which binds ?site), by file and line."""
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
            Caller(self._id(r.caller), str(r.name), str(r.file), int(r.line))
            for r in self.graph.query(q)
        ]

    def _id(self, uri: Any) -> str:
        """Return the code identifier a node URI encodes (inverse of ``_uri``)."""
        return urllib.parse.unquote(str(uri)[len(_CODE_NS):])

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