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

- [x] Change impact from a git diff. `acte impact` maps a diff to the functions and classes
      it touches, decorators, deletions and commented-out lines included, and lists their
      callers. `--format json` for CI.

- [x] Graph analysis. `acte analyze` ranks the most depended-upon functions with PageRank,
      finds import cycles from imports that run at load time, and groups files into
      subsystems.

- [x] Go and TSX structure, and JS/TS function values. Go and TSX files give structure like
      Java and TypeScript. Calls inside JS/TS arrow functions are kept, and
      `const f = () => ...` is a function named `f`.

- [x] Speed of questions on large projects. `callers_of`, `blast_radius` and the health checks
      use direct lookups instead of large SPARQL queries. On django's main folder, `callers_of`
      went from 231 s to 0.13 s and the health checks from 102 s to 3.5 s, with the same answers.

- [x] More calls found without jedi: attributes set on `self` in another method (also in base
      classes and subclasses), attribute chains such as `loader.graph.leaf_nodes()` up to 3
      attributes, `super(C, self).m()`, and names re-exported from outside the project.

- [x] An accuracy check that does not depend on ACTE or jedi: the calls that really happen
      while django's own test suite runs.

Measured results are in the README.

## Next

- [ ] Faster building and opening on large projects.

- [ ] Other languages. Call resolution for Java, JavaScript, TypeScript, TSX and Go, which
      now extract structure only.

- [ ] Incremental builds. Skip files whose content has not changed since the last build.

## Later

Nothing planned here yet.
