# ACTE

Parses codebases with tree-sitter into an RDF knowledge graph (`rdflib`), queried by SPARQL,
to answer dependency and blast-radius questions deterministically for an LLM. The goal is
that the LLM never guesses about structure; it queries proven relationships.

## Orientation

| Path | What |
|---|---|
| `src/acte/parser.py` | Layer 1: tree-sitter AST extraction → `NodeInfo` / `EdgeInfo` |
| `src/acte/rdf_builder.py` | Layer 2: dataclasses → RDF triples, Turtle serialization |
| `src/acte/sparql_engine.py` | Layer 3: named, hardcoded SPARQL queries |
| *(not built)* | Layer 4: MCP server |
| `tests/` | pytest, one file per module |

Python 3.11 + Poetry, src-layout.

## Commands

```bash
poetry run pytest -q          # full suite
poetry run python -c "..."    # anything importing acte
```

**Always `poetry run`.** The bare interpreter cannot import `acte`. `python -c "import acte"`
fails with ModuleNotFoundError. This bites constantly.

## Where the detail lives

Read these when the task calls for them. Do not preload.

- **`initial_arch_n_details.md`**: original vision and four-layer architecture. Read for
  intent and scope questions. Note: it describes the target, not the current build.
- **`.claude/agents/acte-builder.md`**: the executor subagent's contract and hard rules.
- **`docs/plan/`**: design specs, once written. A spec here is the source of truth for
  what to build; the code is not.
- **`notes/`**: gitignored working material, including the verified state of the build and
  its known defects. Read it when present.

## Invariants

- **Node URIs and edge endpoints must be the same identifier.** The project's worst bug came
  from `CALLS` edges pointing at bare short names while function nodes used qualified IDs
  (`file.py::Class.method`), so `code:calls+` could never chain. Any change touching
  identifier construction must be verified against real parser output, not a fixture.
- **Test fixtures must reflect what the pipeline actually emits.** A hand-written graph edge
  in a shape the parser cannot produce is how the above bug stayed hidden through a green
  suite. Build test graphs by running the real pipeline.
- **The parser's tree-sitter helpers are deliberately API-tolerant.** Each tries the standard
  `tree_sitter` attribute, then the `tree_sitter_language_pack` variant. This is load-bearing
  version-drift defence, not redundancy. Do not "simplify" it.
- **Named SPARQL queries are hardcoded on purpose.** The LLM chooses which query and supplies
  arguments; it never authors SPARQL. `execute_raw_sparql` is an escape hatch for exploration,
  not a production path.

## Working agreement

Plan first, then build. Design questions get settled in a spec before code. This project
already shipped a broken headline feature because "how do we identify a call target?" was
answered implicitly at coding time and papered over in a test.

Claude orchestrates and designs; the `acte-builder` subagent executes specified tasks.

## Current phase

Planning. No implementation until a spec is approved. The first spec covers cross-file call
resolution, which fixes the broken blast radius and blocks everything else.
