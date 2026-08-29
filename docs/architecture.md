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

### Layer 1: Extraction (`src/acte/parser.py`)

`tree-sitter` parses each file; a generic walker extracts nodes and edges using a
per-language table of grammar node-type names.

Output is two dataclasses:

- `NodeInfo`: kind, name, file, line span, language, enclosing class
- `EdgeInfo`: kind, source, target, file, line

Extracted kinds: `File`, `Class`, `Function`.
Extracted edges: `CONTAINS`, `CALLS`, `IMPORTS_FROM`.

The tree-sitter helper functions each try the standard `tree_sitter` attribute and then the
`tree_sitter_language_pack` equivalent. The two libraries expose different APIs for the same
data (`node.type` versus `node.kind()`, `start_point` versus `start_position()`) and the
fallbacks absorb that difference rather than pinning to one library's shape.

### Layer 2: Graph construction (`src/acte/rdf_builder.py`)

`NodeInfo` and `EdgeInfo` become RDF triples in the namespace
`http://acte.local/code#`, bound to the prefix `code:`.

| Node kind | rdf:type |
|---|---|
| File | `code:File` |
| Class | `code:Class` |
| Function | `code:Function` |

| Edge kind | predicate |
|---|---|
| CONTAINS | `code:contains` |
| CALLS | `code:calls` |
| IMPORTS_FROM | `code:importsFrom` |
| INHERITS | `code:inherits` *(declared, not yet emitted)* |

Every node also carries `code:name`, `code:filePath`, `code:lineStart`, `code:lineEnd` and
`code:language`.

Triples accumulate across `build()` calls so a repository can be ingested file by file.

### Layer 3: Query (`src/acte/sparql_engine.py`)

A fixed set of named queries over the graph:

| Query | Answers |
|---|---|
| `callers_of` | which functions call this one |
| `blast_radius` | which functions transitively depend on this one |
| `callees_of` | what this function calls |
| `functions_in_file` / `classes_in_file` | what a file defines |
| `dependencies_of` | what a file imports |
| `orphan_functions` | functions nothing calls |
| `execute_raw_sparql` | escape hatch for exploration |

### Layer 4: MCP server

Not implemented. Intended to expose the named queries as tools to an LLM.

## Identifiers

Every node has a qualified identifier built from its file path and enclosing scope:

```
src/app.py                          a file
src/app.py::AuthManager             a class
src/app.py::AuthManager.login       a method
src/app.py::main                    a module-level function
```

These are percent-encoded and appended to the `code:` namespace to form the node's URI.

**Node URIs and edge endpoints must be the same identifier.** An edge whose target is a bare
name does not connect to the node that defines it, and multi-hop traversal silently stops at
the first such edge. This is currently violated for `CALLS` edges (see the Limitations
section of the README) and correcting it is the next planned change.

## Design choices

**Named queries are hardcoded.** The model selects a query by name and supplies arguments;
it never authors SPARQL. A generated query can be subtly wrong in ways that are hard to
detect, and the failure mode of a confident answer from a malformed query is precisely what
this project exists to avoid. `execute_raw_sparql` remains available for exploration.

**Resolution should not guess.** When a call target cannot be determined unambiguously (say
five functions share a name and no import distinguishes them) the intended behaviour is to
record the ambiguity and its candidates rather than choose one. A graph containing a
plausible-looking wrong edge is worse than one with a documented gap, because nothing
downstream can tell the difference.

**Coverage should be measurable.** Unresolved calls are intended to be recorded rather than
discarded, so the proportion of call sites successfully resolved is a number that can be
reported and regression-tested.

The last two describe intended behaviour of work that is planned but not yet implemented;
see [`roadmap.md`](roadmap.md).
