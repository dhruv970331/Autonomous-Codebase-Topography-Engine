# Roadmap

Planned work, roughly in dependency order. Each item is more useful once the ones above it
exist.

## Done

- [x] Cross-file call resolution. Every call site is resolved against an index of all
      definitions, following Python's scope rules and imports (including re-exports and
      `import *`). A call with more than one possible target is stored with all
      its candidates, and `blast_radius()` now follows calls across files, hop by hop.

- [x] Receiver type inference. `x.connect()` resolves when `x` has one known project
      type, from an annotation or an assignment like `x = Database()` (tier T4b). jedi, if
      installed, handles more receivers (tier TJ).

- [x] Command line. `acte build` walks a project (honouring `.gitignore` in a git repo)
      and writes one Turtle graph; `acte stats` summarises a saved graph.

- [x] Validation. Health checks on every build, the PyCG benchmark, and pinned releases
      of rich, httpx, click and flask checked against recorded baselines.

- [x] Class inheritance. Each class records its bases (`code:inherits` for project
      bases), and `self.m()`, `cls.m()`, `C.m()`, typed `x.m()` and `super().m()` find
      inherited methods through the C3 MRO. The lookup stops at a base class from outside
      the project. PyCG's `mro` category went from recall 0.31 (5 of 16)
      to 0.88 (14 of 16), still with no wrong link.

- [x] MCP server (layer 4). `acte serve` offers the named queries as tools, with short
      plain-text answers that say when they were cut short. Tested with opencode and
      Claude Code, and measured with a small model in `eval/`.

Measured results are in the README.

## Next

- [ ] Other languages. Call resolution for Java, JavaScript, TypeScript, TSX and Go, which
      now extract structure only.

- [ ] Change impact from a git diff. Map a diff to the definitions it touches and report
      their blast radius, for review and CI.

- [ ] Faster store. rdflib in memory is fine to about 5,000 files; beyond that, load the
      Turtle output into a dedicated triplestore.

- [ ] Incremental builds. Skip files whose content has not changed since the last build.

## Later

- [x] Graph analysis. Centrality ranking for the most depended-upon functions, community
      detection for subsystems, and cycle detection for circular dependencies.
