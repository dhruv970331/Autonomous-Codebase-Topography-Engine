# Architecture

## Why a graph

Retrieval-augmented generation answers "what code is relevant to this question" with vector
similarity. That works well for prose and poorly for structure: similarity cannot tell you
that changing `verify_token` breaks four callers two hops away. Relational facts have to be
traversed, not approximated.

ACTE extracts those facts once, stores them as a graph, and answers structural questions by
querying it. Results are derived from the parse, so they are reproducible and checkable.

RDF was chosen over a dedicated graph database for two reasons. It needs no server, since the
graph lives in memory and serialises to a single text file. And the output is a W3C
standard, so a `.ttl` artifact loads into an external triplestore unchanged if the project
ever outgrows in-memory processing.

## Pipeline

`build_graph(paths, root)` in `rdf_builder.py` runs the whole pipeline: parse every file,
resolve calls across all of them, build one graph.

### Layer 1: Extraction (`src/acte/parser.py`)

`tree-sitter` parses each file; a generic walker extracts nodes and edges using a
per-language table of grammar node-type names.

Output is a `ParseResult` per file:

- `NodeInfo`: kind, name, file, line span, language, enclosing scope
- `EdgeInfo`: kind, source, target, file, line
- `CallSite`: the caller, the called name, its receiver (`db` in `db.save()`), line, column
- `ImportBinding`, `LocalBinding`: what each scope imports and binds, for the resolver

Extracted kinds: `File`, `Class`, `Function`.
Extracted edges: `CONTAINS`, `IMPORTS_FROM`. Calls are not edges at this stage; they are
`CallSite` records that the resolver decides.

The tree-sitter helper functions each try the standard `tree_sitter` attribute and then the
`tree_sitter_language_pack` equivalent. The two libraries expose different APIs for the same
data (`node.type` versus `node.kind()`, `start_point` versus `start_position()`) and the
fallbacks absorb that difference rather than pinning to one library's shape.

### Layer 1.5: Call resolution (`src/acte/resolver.py`)

A `SymbolIndex` is built over every file's `ParseResult`: definitions by id, module-level
names per file, methods per class, module names, and what each file exports (including
re-exports through imports and `from M import *`).

Each `CallSite` is then looked up the way Python looks up a name: the caller's own scope,
enclosing functions, then the module, honouring `global` and `nonlocal`. The first tier that
matches decides:

| Tier | Call shape | Finds the target through |
|---|---|---|
| T1 | `self.m()` / `cls.m()` | the method on the enclosing class |
| T2 | `f()` | a definition in the same file or enclosing function (never a method) |
| T3 | `f()` | an import visible from the caller's scope |
| T3* | `f()` | a `from M import *` |
| T4 | `C.m()` | a class defined in or imported into the file |
| T4b | `x.m()` | the one known type of `x` (annotation or `x = C(...)`) that has method `m` |
| T5 | `mod.f()` | a module alias or dotted module path |
| TJ | `x.m()` | jedi, optional, only for calls still unknown after T1 to T5 |

Every call gets exactly one status:

- `resolved`: one target, with its tier.
- `ambiguous`: several candidates, all recorded. Over 100 candidates, only the count is kept
  (reason `ceiling`).
- `unresolved`, with a reason: `local` (a variable is called; values are not followed),
  `no-match`, or `unknown-receiver` (the type of `x` in `x.m()` is unknown).
- `external`, with a reason: `builtin`, `stdlib`, `third-party` or `jedi-external`.

**The validation rule.** A tier may only produce an id that a real node owns. If it does not,
that tier did not match. So a fallible heuristic can produce no edge, never a wrong one.

There is no project-wide "only function with that name" matching: it is what produces
plausible wrong links. Files in other languages are parsed, but their calls stay
`unresolved`.

### Layer 2: Graph construction (`src/acte/rdf_builder.py`)

Nodes, edges and resolved calls become RDF triples in the namespace
`http://acte.local/code#`, bound to the prefix `code:`.

| Node kind | rdf:type |
|---|---|
| File | `code:File` |
| Class | `code:Class` |
| Function | `code:Function` |

| Edge kind | predicate |
|---|---|
| CONTAINS | `code:contains` |
| IMPORTS_FROM | `code:importsFrom` the imported project File, or `code:importsExternal` its module name |

An import is looked up like a call's module (relative imports included). One project file
gives a `code:importsFrom` arrow to that File node. No project file gives a
`code:importsExternal` string, the module as written (`"os.path"`), unless the import is
relative: a relative import is inside the project, so it records nothing. Several project
files make it ambiguous, and nothing is recorded. Only Python imports are kept.

Every node also carries `code:name`, `code:filePath`, `code:lineStart`, `code:lineEnd` and
`code:language`.

Every call becomes a `code:CallSite` record:

```turtle
<code:src/app.py::AuthManager.login@L12c8> a code:CallSite ;
    code:inFunction <code:src/app.py::AuthManager.login> ;
    code:line 12 ; code:column 8 ;
    code:targetName "verify_token" ; code:receiver "self" ;
    code:status "resolved" ; code:tier "T1" ;
    code:resolvedTo <code:src/app.py::AuthManager.verify_token> .
```

Ambiguous calls carry `code:candidate` (one per candidate) and `code:candidateCount`;
unresolved and external calls carry `code:reason`.

Only a resolved call also draws an arrow between two nodes: `caller code:calls function`, or
`caller code:instantiates class` for a constructor call like `Database()`. The caller is a
Function, or the File for module-level code. A recursive call keeps its record, flagged
`code:recursive`, but draws no arrow to itself.

Triples accumulate across `build()` calls so a repository can be ingested file by file.

### Layer 3: Query (`src/acte/sparql_engine.py`)

A fixed set of named queries over the graph:

| Query | Answers |
|---|---|
| `callers_of` | who calls this definition: a `CallerAnswer` |
| `blast_radius` | who depends on it, hop by hop: a `BlastAnswer` |
| `callees_of` | what this function's calls resolve to |
| `functions_in_file` / `classes_in_file` | what a file defines |
| `dependencies_of` | what a file imports: project file paths and external module names |
| `orphan_functions` | functions nothing calls (recursion does not count) |
| `execute_raw_sparql` | escape hatch for exploration |

`callers_of` and `blast_radius` accept a qualified id, a dotted name like
`Database.connect`, or a bare name, which matches every function and class with that name.
They return one answer per matching definition, at most 6.

A `CallerAnswer` keeps confirmed callers (resolved calls) apart from possible callers
(ambiguous calls that list the target as a candidate). It is labelled `exact` or
`lower-bound`, and a lower bound says why, with counts: `ambiguous`, `unknown-receiver`
(calls like `x.save()` that might reach a `save`), `ceiling`. Callers of `C.__init__`
include everything that creates a `C`.

A `BlastAnswer` steps outwards from the target one hop at a time in Python, up to `depth`
hops (default 3, at most 10). Each affected node has its distance and the node it reaches
the target through. Results are ranked by distance, then by how many callers a node has,
then file and line, and cut to `limit` (default 50) with the full total reported. Reaching
the depth limit with callers still beyond it makes the answer `lower-bound`
(`depth-limit`).

### Layer 4: MCP server (`src/acte/mcp_server.py`)

`acte serve GRAPH.ttl` loads one graph, read-only, and offers the named queries as MCP tools
over stdio, built on the official `mcp` SDK. Queries run one at a time under a lock. The
`small` profile (default) has `find_symbol`, `callers_of`, `callees_of` and `blast_radius`;
`full` adds `functions_in_file`, `dependencies_of`, `orphan_functions` and `graph_info`.
`execute_raw_sparql` is not offered: the model never writes SPARQL.

Each tool takes a name or path and returns plain text built for a small model:

- the first line sums up the answer, including `exact` or `lower-bound` and why;
- every item has `file:line`;
- answers stay under about 4,000 characters, cut at whole lines, and say what was left out;
- a name with several definitions gets the list and a request for the full name, never a
  pick; an unknown name gets up to 3 close spellings and a prompt to call again with one;
- these are normal answers, not errors. Errors are kept for real malfunctions;
- with `--project`, every answer warns when the project's git HEAD differs from the
  commit the graph was built from.

## Identifiers

Every node has a qualified identifier built from its file path and enclosing scope. Nested
definitions follow Python's own `__qualname__`:

```
src/app.py                            a file
src/app.py::AuthManager               a class
src/app.py::AuthManager.login         a method
src/app.py::main                      a module-level function
src/app.py::outer.<locals>.helper     a function defined inside outer
src/app.py::main@L12c8                a call site in main
```

A call site's id is its caller's id plus line and column. The column is counted in UTF-8
bytes, as tree-sitter and Python's `ast` count it.

Identifiers are percent-encoded and appended to the `code:` namespace to form the node's
URI. `#` is encoded too, so the namespace's `#` stays the only one in the IRI.

**Node URIs and edge endpoints must be the same identifier.** An edge whose target does not
match a node's id connects to nothing, and multi-hop traversal silently stops there. The
validation rule above is what keeps call arrows on real nodes.

## Design choices

**Named queries are hardcoded.** The model selects a query by name and supplies arguments;
it never authors SPARQL. A generated query can be subtly wrong in ways that are hard to
detect, and the failure mode of a confident answer from a malformed query is precisely what
this project exists to avoid. `execute_raw_sparql` remains available for exploration.

**Resolution does not guess.** When a call target cannot be determined unambiguously (say
two imported modules both define it) the call is recorded as ambiguous with its candidates
rather than resolved to one. A graph containing a plausible-looking wrong edge is worse than
one with a documented gap, because nothing downstream can tell the difference. Answers built
on such gaps say so: they are labelled `lower-bound`.

**Coverage is measurable.** Every call site is recorded with its status, so the proportion
resolved is a number that is reported and regression-tested. Call links are also graded
against PyCG's benchmark (`tests/test_pycg.py`), where no wrong link is allowed.
