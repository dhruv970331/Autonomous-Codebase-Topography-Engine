# Roadmap

Planned work, roughly in dependency order. Each item is more useful once the ones above it
exist.

## Done

- [x] **Cross-file call resolution.** Every call site is resolved against an index of all
      definitions, following Python's scope rules and imports (including re-exports and
      `import *`). Ambiguous sites are recorded with their candidates rather than guessed
      at, and `blast_radius()` now follows calls across files, hop by hop.

- [x] **Receiver type inference.** `x.connect()` resolves when `x` has one known project
      type, from an annotation or an assignment like `x = Database()` (tier T4b). jedi, if
      installed, handles more receivers (tier TJ).

Measured: against PyCG's call-graph benchmark, precision 1.00 (one documented case
excluded) and recall 0.46. Over ACTE's own `src/`, 0.59 of non-external calls resolve
without jedi and 0.89 with it. On that code jedi's gain is all from recognising library
calls as external; it added no new links.

## Next

- [ ] **Repository ingestion and CLI.** A file walker that honours `.gitignore`, plus a
      command-line entry point that ingests a directory and writes a `.ttl` artifact. There
      is currently no way to point ACTE at a repository.

- [ ] **Validation against real repositories.** Run the full pipeline over well-known
      open-source Python projects pinned to fixed commits, asserting on resolution rate,
      graph size, and query correctness. Intended for CI so regressions surface immediately.

## After that

- [ ] **MCP server (layer 4).** Expose the named queries as tools over the Model Context
      Protocol so an LLM can query the graph directly. Small tool surface, human-readable
      arguments, single-call answers, bounded responses that report truncation.

- [ ] **Language coverage.** Go and TSX map to file extensions but have no grammar rules, so
      they currently extract nothing. Java, JavaScript and TypeScript extract structure but
      have no call resolution. Extraction and resolution will be documented as separate tiers
      per language.

- [ ] **Class inheritance.** The `code:inherits` predicate is declared but nothing emits it.

## Later

- [ ] **Graph analysis.** Centrality ranking for the most depended-upon functions, community
      detection for subsystems, and cycle detection for circular dependencies.

- [ ] **Performance.** Content-hash caching so unchanged files are skipped on re-runs. The
      in-memory graph tops out in the low thousands of files; the Turtle output loads into an
      external triplestore beyond that.

- [ ] **Change impact analysis.** Map a git diff to the nodes it touches and report the
      downstream impact. This is the intended use of the blast-radius query in CI.
