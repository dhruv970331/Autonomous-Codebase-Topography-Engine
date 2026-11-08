# ACTE

Parses codebases with tree-sitter, links each call to the function it calls, and stores the
result as an RDF map in pyoxigraph. Named queries over the map answer dependency and
blast-radius questions deterministically for an LLM, through a CLI and an MCP server.

## Orientation

| Path | What |
|---|---|
| `src/acte/parser.py` | tree-sitter AST extraction into `NodeInfo` / `EdgeInfo` |
| `src/acte/resolver.py` | links each call site to its target, or records why it cannot |
| `src/acte/rdf_builder.py` | builds the map in pyoxigraph; saves and opens map folders |
| `src/acte/sparql_engine.py` | named queries: callers, callees, blast radius, listings |
| `src/acte/health.py` | the health checks and coverage of a map |
| `src/acte/cli.py` | `acte build`, `stats`, `impact`, `analyze`, `serve` |
| `src/acte/impact.py` | what a change in the working tree could break |
| `src/acte/analysis.py` | ranking and other whole-map analysis |
| `src/acte/mcp_server.py` | the MCP server (`acte serve`) |
| `src/acte/ontology/code.ttl` | every class and property the map uses |
| `tests/` | pytest, one file per module; `tests/data/` holds small real projects |
| `docs/architecture.md` | ontology, identifier scheme, map folder, design rationale |
| `docs/roadmap.md` | done and planned work |

Python 3.11+ and Poetry, src layout.

## Commands

```bash
poetry run ruff check         # lint; CI runs it first and stops on an error
poetry run pytest -q          # full suite; CI runs it on Python 3.11 and 3.12
ACTE_REAL_REPOS=1 poetry run pytest -q   # also builds pinned real projects (slow)
poetry run python -c "..."    # anything importing acte
```

**Always `poetry run`.** The bare interpreter cannot import `acte`: `python -c "import acte"`
fails with ModuleNotFoundError.

## Invariants

- **Node URIs and edge endpoints must be the same identifier.** An edge that points at a bare
  short name does not connect to the node that defines it, and multi-hop traversal stops there
  without an error. Any change to how identifiers are built must be checked against real parser
  output.
- **Test fixtures must reflect what the pipeline actually emits.** A hand-written graph edge in
  a shape the parser cannot produce passes a test while the feature is broken in practice. Build
  test maps by running the real pipeline over real files.
- **The parser's tree-sitter helpers are deliberately API-tolerant.** Each tries the standard
  `tree_sitter` attribute, then the `tree_sitter_language_pack` variant. This is a defence
  against version drift, not redundancy. Do not "simplify" it.
- **Named SPARQL queries are hardcoded on purpose.** The LLM chooses which query and supplies
  arguments; it never writes SPARQL. `execute_raw_sparql` is for exploration, not a production
  path.
- **Resolution must not guess.** An ambiguous call target is recorded with its candidates, never
  resolved to a plausible-looking choice.
- **The same code gives the same map.** `map.nt.gz` and the saved index and call links must be
  the same bytes on every build, whatever `--jobs` is. Nothing that varies (time, paths outside
  the project, store order) may go into them.
- **A speed change must not change an answer.** Compare the answers before and after on real
  projects.

## Working agreement

Plan before building. Settle design questions in writing first, particularly anything touching
how identifiers are built or how call targets are resolved. Those decided implicitly at coding
time are how earlier defects arose.

Small commits, one concern each, lint and test suite green at every step.

## Local notes

A gitignored `notes/` directory may hold working material: defect analysis, detailed
specifications, comparisons with similar tools. Read it when present; it is not part of the
repository and other contributors will not have it.
