# ACTE

Parses codebases with tree-sitter into an RDF knowledge graph (`rdflib`), queried by SPARQL,
to answer dependency and blast-radius questions deterministically for an LLM.

## Orientation

| Path | What |
|---|---|
| `src/acte/parser.py` | Layer 1: tree-sitter AST extraction → `NodeInfo` / `EdgeInfo` |
| `src/acte/rdf_builder.py` | Layer 2: dataclasses → RDF triples, Turtle serialization |
| `src/acte/sparql_engine.py` | Layer 3: named, hardcoded SPARQL queries |
| *(not built)* | Layer 4: MCP server |
| `tests/` | pytest, one file per module |
| `docs/architecture.md` | ontology, identifier scheme, design rationale |
| `docs/roadmap.md` | planned work |

Python 3.11 + Poetry, src-layout.

## Commands

```bash
poetry run pytest -q          # full suite
poetry run python -c "..."    # anything importing acte
```

**Always `poetry run`.** The bare interpreter cannot import `acte`. `python -c "import acte"`
fails with ModuleNotFoundError. This bites constantly.

## Invariants

- **Node URIs and edge endpoints must be the same identifier.** An edge targeting a bare
  short name does not connect to the node that defines it, and multi-hop traversal stops
  there silently. This is currently violated for `CALLS` edges and is the next thing being
  fixed. Any change touching identifier construction must be verified against real parser
  output.
- **Test fixtures must reflect what the pipeline actually emits.** A hand-written graph edge
  in a shape the parser cannot produce will pass a test while the feature is broken in
  practice. Build test graphs by running the real pipeline over real files.
- **The parser's tree-sitter helpers are deliberately API-tolerant.** Each tries the standard
  `tree_sitter` attribute, then the `tree_sitter_language_pack` variant. This is load-bearing
  version-drift defence, not redundancy. Do not "simplify" it.
- **Named SPARQL queries are hardcoded on purpose.** The LLM chooses which query and supplies
  arguments; it never authors SPARQL. `execute_raw_sparql` is an escape hatch for exploration,
  not a production path.
- **Resolution must not guess.** An ambiguous call target is recorded with its candidates,
  never resolved to a plausible-looking choice.

## Working agreement

Plan before building. Settle design questions in writing first, particularly anything
touching how identifiers are constructed or how call targets are resolved. Those decided
implicitly at coding time are how the current defect arose.

Claude orchestrates and designs; the `acte-builder` subagent executes specified tasks.

Small commits, one concern each, test suite green at every step.

## Local notes

A gitignored `notes/` directory may hold working material: defect analysis, detailed
specifications, comparisons with similar tools. Read it when present; it is not part of the
repository and other contributors will not have it.
