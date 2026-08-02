# Roadmap

Planned work, roughly in dependency order. Each item is more useful once the ones above it
exist.

## Next

- [ ] **Cross-file call resolution.** Call targets are stored as bare names, so a `CALLS`
      edge does not connect to the node defining that function. Multi-hop traversal stops
      after one step and `blast_radius()` returns only direct callers. The fix is a
      resolution pass that indexes every definition, then resolves each call site against
      that index. Ambiguous sites are recorded with their candidates rather than guessed at.
      Everything below depends on this.

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

- [ ] **Receiver type inference.** Resolving `db.connect()` needs the type of `db`. Tracking
      simple assignments and annotations covers a useful fraction of real code. Worth
      building or not will be decided from the measured resolution rate.

- [ ] **Class inheritance.** The `code:inherits` predicate is declared but nothing emits it.

## Later

- [ ] **Graph analysis.** Centrality ranking for the most depended-upon functions, community
      detection for subsystems, and cycle detection for circular dependencies.

- [ ] **Performance.** Content-hash caching so unchanged files are skipped on re-runs. The
      in-memory graph tops out in the low thousands of files; the Turtle output loads into an
      external triplestore beyond that.

- [ ] **Change impact analysis.** Map a git diff to the nodes it touches and report the
      downstream impact. This is the intended use of the blast-radius query in CI.
