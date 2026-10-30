# Architecture

## Why a graph

Most code assistants find code with vector search: they look for text that is similar to
the question. That is good for finding where something is described. It cannot tell you
that changing `verify_token` breaks four callers two steps away. For that you have to follow
the calls one by one.

ACTE finds those calls once, stores them in a graph, and answers questions by querying the
graph. The answers come straight from the parsed code, so the same query always gives the
same result, and each result can be checked.

ACTE uses RDF instead of a graph database for two reasons. It needs no server: the graph
lives in memory and is saved as one text file. And RDF is a W3C standard, so the `.ttl` file
can be loaded into a triplestore as it is, if a project ever gets too big for memory.

## Pipeline

`build_graph(paths, root)` in `rdf_builder.py` runs the whole pipeline: parse every file,
resolve calls across all of them, build one graph. With `jobs` above 1 and at least 50
files, the files are parsed in a pool of processes and the results are put back in file
order, so the graph is the same as with one process (`acte build --jobs`).

### Layer 1: Extraction (`src/acte/parser.py`)

`tree-sitter` parses each file; a generic walker extracts nodes and edges using a
per-language table of grammar node-type names.

Output is a `ParseResult` per file:

- `NodeInfo`: kind, name, file, line span, language, enclosing scope; for a Python class, its
  bases as written (`Base`, `mod.Base`, `Generic` for `Generic[T]`; keyword arguments such as
  `metaclass=` are not bases, and any other expression is marked unknown)
- `EdgeInfo`: kind, source, target, file, line; for a Python import also its kind, in
  `extra["import_kind"]`
- `CallSite`: the caller, the called name, its receiver (`db` in `db.save()`), line, column
- `ImportBinding`, `LocalBinding`: what each scope imports and binds, for the resolver

Extracted kinds: `File`, `Class`, `Function`.
Extracted edges: `CONTAINS`, `IMPORTS_FROM`. Calls are not edges at this stage; they are
`CallSite` records that the resolver decides.

The tree-sitter helper functions each try the standard `tree_sitter` attribute and then the
`tree_sitter_language_pack` equivalent. The two libraries expose different APIs for the same
data (`node.type` versus `node.kind()`, `start_point` versus `start_position()`). The
fallbacks let ACTE work with either library.

### Layer 1.5: Call resolution (`src/acte/resolver.py`)

A `SymbolIndex` is built over every file's `ParseResult`: definitions by id, module-level
names per file, methods per class, module names, and what each file exports (including
re-exports through imports and `from M import *`).

It also holds each class's bases and its method resolution order (MRO). A base is looked up
like a type name, from the scope the class statement runs in: a project class gives its id,
anything else is `external` (`dict`, `typing.Generic`) or `unknown` (`make_base()`, a
variable). The builtin `object` is no base, since every class has it. The MRO is computed
with C3, as Python does, over the declared bases; each external or unknown base is an opaque
entry of its own. A cycle or an inconsistent MRO makes the whole MRO unknown.

Each `CallSite` is then looked up the way Python looks up a name: the caller's own scope,
enclosing functions, then the module, honouring `global` and `nonlocal`. The first tier that
matches decides:

| Tier | Call shape | Finds the target through |
|---|---|---|
| T1 | `self.m()` / `cls.m()` / `super().m()` | the method on the enclosing class, or through its MRO |
| T2 | `f()` | a definition in the same file or enclosing function (never a method) |
| T3 | `f()` | an import visible from the caller's scope |
| T3* | `f()` | a `from M import *` |
| T4 | `C.m()` | a class defined in or imported into the file, and its MRO |
| T4b | `x.m()`, `self.a.m()`, `x.a.b.m()` | the one known type of the receiver (annotation or `x = C(...)`) whose MRO has method `m` |
| T5 | `mod.f()` | a module alias or dotted module path |
| TJ | `x.m()` | jedi, optional, only for calls still unknown after T1 to T5 |

#### Method lookup through the MRO

For T1, T4 and T4b (and `mod.C.m()` under T5), the class's
own method wins. Otherwise the MRO is walked in order. A method found on a project class
before any external or unknown base resolves to that class's method, with the same tier.
Reaching an external or unknown base first leaves the call `unresolved` with reason
`unknown-base`: the method may live in code ACTE cannot see. `super().m()` in a method of
`C` searches the MRO after `C` (tier T1). `super(B, y).m()` searches after `B`: in `C`'s MRO
when `y` is the method's first parameter (`B` must be in it, else the call fails at run time
and stays unknown), otherwise in `B`'s own MRO, since `y` must be an instance of `B`. None of
these applies when `super` is rebound. Reaching the end of the MRO this way is `unknown-base`
too, not `no-match`: in a mixin, the class after `C` at run time is not known. The MRO is the
one declared from the class the code is written in: a subclass that overrides `m` is not seen
from a call in the base class, and `super()` follows `C`'s MRO. The class of the object at run
time is not known.

#### Types of variables and attributes (T4b)

A name's type is the one every binding of it agrees on: an annotation, `x = C(...)`, or a
function call whose return annotation names a class. A binding that has no type, or two that
disagree, leave it unknown. A binding to `None` takes no part, unless `None` itself has the
method called (`__eq__`, `__bool__`): calling a method on `None` fails, so the call only runs
on the typed value, as with an `Optional[X]` annotation.

The type of `self.a` comes from the bindings of every project class in the method's class's MRO,
and of every project class that inherits from it, since `self` may be one of those:
`self.a = ...` in any method, class-level annotations `a: T` (such as dataclass fields), and
class-level literals (`a = {}` is a `dict`). An outside base is not looked into. The attribute has
no type when its name is a method, property or nested class in any of these classes, or when it
has another class-level value other than `None` (`content_type = ForeignKey(...)` is a
descriptor, whose value on an instance has another type).
A chain `self.a.b.m()` or `x.a.b.m()` is followed one attribute at a time, for up to 3
attributes, and stops, unknown, at the first one with no single project class.

A name imported from a project module that only re-imports it from outside the project
(`from starlette.testclient import TestClient` in `fastapi/testclient.py`) is followed there,
and its calls are `external`, like those of a direct import.

Every call gets exactly one status:

- `resolved`: one target, with its tier.
- `ambiguous`: several candidates, all recorded. Over 100 candidates, only the count is kept
  (reason `ceiling`).
- `unresolved`, with a reason: `local` (a variable is called; values are not followed),
  `no-match`, `unknown-receiver` (the type of `x` in `x.m()` is unknown), or `unknown-base`
  (the method was not found before an external or unknown base in the MRO, or `super()` went
  past the end of the declared MRO).
- `external`, with a reason: `builtin`, `stdlib`, `third-party` or `jedi-external`.

#### The validation rule

A tier may only produce an id that belongs to a real node. If it produces any other id, the
tier counts as not matching. So when a tier gets it wrong, the call gets no link at all.

ACTE does not link a call to a function just because it is the only one in the project with
that name. That shortcut is a common source of wrong links. Files in other languages are parsed, but their calls stay
`unresolved`.

#### Known limits

- Writes to an attribute from outside its class (`obj.attr = x`) are not seen, so they take no
  part in the attribute's type. The parser would have to record them.
- A name bound twice at module level (an import and a later `class` of the same name) is not
  exported, so calls to it from other modules are `no-match`.

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
| IMPORTS_FROM, type-only | `code:importsTypeOnly` the File, or `code:importsTypeOnlyExternal` the module name |
| IMPORTS_FROM, in a function | `code:importsInFunction` the File, or `code:importsInFunctionExternal` the module name |
| IMPORTS_FROM, main block | `code:importsInMainBlock` the File, or `code:importsInMainBlockExternal` the module name |
| class bases | `code:inherits` the base Class, for project bases only |

An import is looked up like a call's module (relative imports included). One project file
gives a `code:importsFrom` arrow to that File node. No project file gives a
`code:importsExternal` string, the module as written (`"os.path"`), unless the import is
relative: a relative import is inside the project, so it records nothing. Several project
files make it ambiguous, and nothing is recorded. Only Python imports are kept.

Each Python import has one of four kinds, and each kind has its own pair of predicates:

| Kind | Where the import is | When it runs |
|---|---|---|
| `module` | top level, also inside a top-level `try`, a plain `if`, or a class body | when the file loads |
| `type-only` | the first block of `if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:`, at any depth | never; only type checkers read it |
| `main-block` | the first block of a module-level `if __name__ == "__main__":` (either operand order, either quote) | only when the file is run as a script |
| `in-function` | anywhere inside a function or method body | when the function runs |

Rules for the edge cases:

- `elif` and `else` blocks keep their usual kind.
- The block decides before the function: a `TYPE_CHECKING` block inside a function is
  type-only, and a function inside a main block holds main-block imports. When blocks nest,
  the innermost one decides.
- Only the conditions above are recognised, written exactly so. `if t.TYPE_CHECKING:` through
  an alias, `if TYPE_CHECKING and x:` and `if (TYPE_CHECKING):` give `module` imports.
- A file that imports the same target in two kinds gets one arrow of each.

The kind does not change how the resolver uses an import: a function-local import still binds
its name for the calls in that function. Import cycles (`acte analyze`) count only `module`
imports, because only those run when the file loads.

Every node also carries `code:name`, `code:filePath`, `code:lineStart`, `code:lineEnd` and
`code:language`. A decorated Python function or class also carries `code:decoratorLine`, the line of its
first decorator. Its `code:lineStart` stays the line of `def` or `class`.

Each Function and Class also has one `code:definedAt` record per definition: a `code:Definition`
with that definition's own `code:lineStart`, `code:lineEnd` and `code:decoratorLine`. An id
defined twice, as in both branches of an `if`, keeps one node with two records. The node itself
then has two of each line fact with nothing to pair them, so `acte impact`, the file listings and
the orphan list read the records. Where one line is shown for such an id, it is the first
definition's. A record's IRI is the node's IRI followed by `!` and its start line, for example
`<code:src/app.py::alpha!9>`. The IRI of an id never holds a bare `!`, because `_make_uri` encodes it.

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
`code:recursive`, but draws no arrow to itself. A resolved call that creates a class also
records the `__init__` it runs, found through the class's MRO as for T1: `code:initTarget`
that method, or `code:initUnknown true` when an external or unknown base comes first.
Neither is recorded when no project class in the MRO defines `__init__`.

Triples accumulate across `build()` calls so a repository can be ingested file by file.

### Layer 3: Query (`src/acte/sparql_engine.py`)

A fixed set of named queries over the graph:

| Query | Answers |
|---|---|
| `callers_of` | who calls this definition: a `CallerAnswer` |
| `blast_radius` | who depends on it, hop by hop: a `BlastAnswer` |
| `callees_of` | what this function's calls resolve to |
| `functions_in_file` / `classes_in_file` | what a file defines |
| `dependencies_of` | what a file imports: a `DependenciesAnswer`, the project file paths and external module names, and the import kinds of each one not imported at module level |
| `orphan_functions` | functions nothing calls (recursion does not count) |
| `execute_raw_sparql` | escape hatch for exploration |

`callers_of` and `blast_radius` accept a qualified id, a dotted name like
`Database.connect`, or a bare name, which matches every function and class with that name.
They return one answer per matching definition, at most 6.

A `CallerAnswer` keeps confirmed callers (resolved calls) apart from possible callers:
ambiguous calls that list the target as a candidate, and, for a method `Sub.m` that
overrides `Base.m` of a project base class, calls resolved to `Base.m` through `self`/`cls`
(T1, not `super()`), a variable typed as the base (T4b) or jedi (TJ), which may run `Sub.m`
at runtime.
It is labelled `exact` or `lower-bound`, and a lower bound says why, with counts:
`ambiguous`, `override`, `unknown-receiver`
(calls like `x.save()` that might reach a `save`), `unknown-base` (calls like `self.save()`
that stopped at a base outside the project, and might reach a project `save` after it),
`ceiling`. Callers of `C.__init__`
include every creation whose `code:initTarget` is `C.__init__`: of `C`, or of a subclass that
inherits it. Creating a subclass of `C` marked `code:initUnknown` is a possible caller, counted
as `unknown-base`.

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
The server builds the engine's name index right after loading the graph.

`acte build` stores one `code:summary` literal on the Build node: JSON with the counts, the
coverage, the row count of each health check, and the graph's triple count. `graph_info` uses
it only when it is valid JSON, the triple count still matches the loaded graph, and the
graph's `code:acteVersion` is the running version. Otherwise it computes everything again.
`acte stats` always computes everything again.

Each tool takes a name or path and returns plain text built for a small model:

- the first line sums up the answer, including `exact` or `lower-bound` and why;
- every item has `file:line`;
- answers stay under about 4,000 characters, cut at whole lines, and say what was left out;
- a name with several definitions gets the list and a request for the full name. The
  server does not pick one;
- an unknown name gets up to 3 close spellings and a prompt to call again with one;
- both of these are normal answers. Errors are only returned when something actually fails;
- with `--project`, every answer warns when the project's git HEAD differs from the
  commit the graph was built from. git is asked at most once every 5 seconds, so the
  warning can lag a new commit by up to 5 seconds.

### Built on the graph: `acte impact` and `acte analyze`

`impact.py` answers "what may this change break?". It reads `git diff -U0` for the given base
(default `HEAD`), builds a fresh graph of the work tree, and maps each changed line to the
innermost Function or Class whose span holds it, starting the span at `code:decoratorLine`
when there is one. Each definition record is its own span. Blank and comment-only lines are skipped. For a deletion in a Python file it reads the
removed text: a removed `def`, `class` or decorator marks the definition around both
neighbouring lines, and removed body code marks the definition just above. In other
languages a deletion marks the definition around both neighbouring lines. A line in no
definition is a module-level change of its file. Each changed definition gets a
`blast_radius`; a module-level change lists the files that import the file, with their
import kinds. Deleted definitions are not named, because they are not in the new graph.

`analysis.py` reads a saved graph and computes three things, all deterministic for one set of
installed versions:

- Ranking: PageRank over `code:calls` and `code:instantiates` arrows, written in about ten
  lines of plain Python with networkx's default settings. `nx.pagerank` needs numpy and
  scipy; the maths takes 0.18 s on django's 45,100 definitions, so the extra install is not
  worth it. A test checks our scores against networkx's pure-Python version.
- Import cycles: strongly connected components of two or more files over `code:importsFrom`
  (module-level imports only).
- Subsystems: Louvain communities (`seed=0`) on an undirected file graph, where an edge's
  weight is the number of resolved calls between two files plus 1 if either imports the
  other. Calls inside one file add nothing. Each subsystem is named after its hub, the file
  with the largest weighted degree inside it.

Neither adds anything to the graph.

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
shapes/point.go::Point.Scale          a Go method on Point or *Point
```

A Go method is scoped by its receiver type, without the pointer `*` or type arguments
(`Pair` for `*Pair[T]`). Its File contains it, since the type may be declared in another
file of the package. Every Go named type is a Class, such as `type Point struct` or
`type UUID [16]byte`. A type alias (`type A = B`) is not.

A call site's id is its caller's id plus line and column. The column is counted in UTF-8
bytes, as tree-sitter and Python's `ast` count it.

Identifiers are percent-encoded and appended to the `code:` namespace to form the node's
URI. `#` is encoded too, so the namespace's `#` stays the only one in the IRI.

Node URIs and edge endpoints must use the same identifier. An edge whose target does not
match a node's id connects to nothing, and multi-hop traversal silently stops there. The
validation rule above is what keeps call arrows on real nodes.

## Design choices

### Named queries are hardcoded

The model picks a query by name and gives it arguments. It does not write SPARQL. A
generated query can be wrong in ways that are hard to spot, and the model would then give a
confident answer based on bad data. `execute_raw_sparql` is still there for exploring the
graph by hand.

### Resolution does not guess

If a call could go to more than one target (for example, two imported modules both define
the function), it is stored as ambiguous with all its candidates. A wrong link that looks
right is worse than a known gap, because nothing later on can tell that it is wrong. Answers
that depend on such gaps are marked `lower-bound`.

### Coverage is measured

Every call site is stored with its status, so the share of resolved calls is a number. It is
shown on every build and checked by the tests. Only calls in Python files count towards it,
since Python is the only language with call resolution. Calls in other languages are
counted apart. Call links are also checked against PyCG's
benchmark (`tests/test_pycg.py`), where no wrong link is allowed.
