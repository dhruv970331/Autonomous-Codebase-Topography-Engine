# ACTE: Autonomous Codebase Topography Engine

ACTE parses source code into an RDF knowledge graph and answers structural questions about
it with SPARQL. The goal is that a language model reasoning about a codebase queries proven
relationships instead of inferring them from retrieved text.

Vector search over code returns passages that *look* relevant. A graph query returns the
functions that actually call the one you changed. ACTE is built on the second.

![Architecture](ACTE_arch_flowchart.png)

## How it works

| Layer | Does |
|---|---|
| **1. Parse** | tree-sitter walks the AST and extracts files, classes, functions, and the edges between them |
| **2. Build** | those become RDF triples in a `code:` namespace, serialisable to Turtle (`.ttl`) |
| **3. Query** | a fixed set of named SPARQL queries answers callers, callees, dependencies, and dead code |
| **4. Serve** | an MCP server that offers those queries to an LLM as tools |

Queries are hardcoded by design. The model chooses which named query to run and supplies
arguments; it never writes SPARQL.

## Status

Early, but usable end to end: build a map of a Python project, then let an LLM client ask it
questions over MCP.

| | State |
|---|---|
| AST extraction | Working for Python; Java, JavaScript and TypeScript extract structure only |
| RDF graph + Turtle output | Working |
| Call resolution | Python only, see Limitations |
| Command line (`acte build`, `acte stats`, `acte serve`) | Working |
| Validation on real repositories | Working (rich, httpx, click, flask) |
| MCP server | Working; tested with opencode and Claude Code |

## Quickstart

```bash
pip install "acte[jedi] @ git+https://github.com/dhruv970331/Autonomous-Codebase-Topography-Engine.git"
cd your-project
acte build .            # writes acte-graph.ttl
```

Then connect a client: copy [`examples/opencode.json`](examples/opencode.json) or
[`examples/mcp.json`](examples/mcp.json) (as `.mcp.json` for Claude Code) into the project
folder. See [`examples/README.md`](examples/README.md).

## Requirements

Python 3.11+ and [Poetry](https://python-poetry.org/).

```bash
poetry install
poetry run pytest -q
```

`ACTE_REAL_REPOS=1 poetry run pytest tests/test_real_repos.py -s` also builds pinned releases
of rich, httpx, click and flask and checks them against recorded baselines. It is skipped by
default, needs git and the network on first run, and takes a few minutes.

## Usage

### Command line

```bash
poetry run acte build src -o graph.ttl   # parse a project folder into one Turtle graph
poetry run acte stats graph.ttl          # what a saved graph holds, and whether it is healthy
```

`acte build PATH` parses every source file under `PATH`. In a git repository it takes the
files git knows about, so everything in `.gitignore` is left out; elsewhere it skips folders
such as `.venv`, `node_modules` and hidden ones. Ids are relative to `PATH`
(`shop/db.py::Database.connect`), so the graph does not depend on where the project sits.
`--no-jedi` skips the jedi step. It prints how many files, functions, classes and calls it
found, the share of calls resolved, and the result of the health checks.

`acte stats GRAPH` prints the same summary for a saved graph, plus how it was built (folder
name, git commit, versions) and, for any failing health check, the first rows that break it.

Both commands exit with 1 when a health check fails.

### MCP server

```bash
acte serve acte-graph.ttl [--project PATH] [--profile small|full]
```

It speaks MCP over stdio. Every answer is plain text, starts with a one-line summary, gives
`file:line` for each item, and stays under about 4,000 characters, saying what it left out.
An unknown name gets "did you mean" suggestions; a name with several definitions gets the
list, never a guess. With `--project`, answers warn when the project's git HEAD is not the
commit the map was built from.

| Tool | Profile | Answers |
|---|---|---|
| `find_symbol` | small | functions and classes matching a name, forgiving of case and typos |
| `callers_of` | small | who calls a function or creates a class; exact or a lower bound, and why |
| `callees_of` | small | what a function calls, plus counts of outside and unresolved calls |
| `blast_radius` | small | everything affected by a change, grouped by distance (depth 1 to 3) |
| `functions_in_file` | full | what one file defines |
| `dependencies_of` | full | what one file imports |
| `orphan_functions` | full | functions no resolved call reaches (dead code candidates) |
| `graph_info` | full | how the map was built, its coverage and health |

`small` (the default) offers the first four; `full` offers all eight.

### Python

The same pipeline from Python:

```python
from pathlib import Path
from acte.rdf_builder import build_graph
from acte.sparql_engine import SPARQLEngine

root  = Path("src")
graph = build_graph(sorted(root.rglob("*.py")), root)   # parse, resolve calls, build
graph.serialize("graph.ttl", format="turtle")

engine = SPARQLEngine(graph=graph)
engine.callers_of("db_connect")
engine.blast_radius("db_connect", depth=3)
engine.functions_in_file("src/app.py")
engine.dependencies_of("src/app.py")
engine.orphan_functions()
```

## Measured

- **PyCG call-graph benchmark** (`tests/test_pycg.py`): precision 1.00, with one documented
  case excluded, and recall 0.50 (117 of 233 expected links).
- **Real repositories** (`tests/test_real_repos.py`, pinned releases). Coverage is the share
  of calls into project code that are resolved, with jedi. Link precision and recall are for
  the build without jedi, against jedi's references over 40 sampled functions; jedi's key
  has known misses, so precision below 1.00 is not always a wrong link.

  | Repo | Files | Coverage | Link precision | Link recall |
  |---|---|---|---|---|
  | rich 13.9.4 | 190 | 88.9% | 0.98 | 0.94 |
  | httpx 0.28.1 | 60 | 87.4% | 0.99 | 0.96 |
  | click 8.1.8 | 16 | 84.2% | 0.98 | 0.76 |
  | flask 3.1.0 | 24 | 58.5% | 1.00 | 0.86 |

- **A small model using the MCP tools** (`eval/`): Qwen 3.6 35B-A3B answered 41 of 41
  questions correctly at temperature 0. The question set is small and is about ACTE's own
  source, so treat this as a smoke test, not a benchmark.

## Limitations

Stated plainly, because they affect what the results mean.

- **Call resolution is Python only.** Java, JavaScript and TypeScript files give their
  structure but no call links. Go and TSX map to file extensions but have no grammar rules,
  so those files produce a file node and nothing else.
- **Not every call is resolved.** A call is linked only when its target is certain. Calls
  that could go to several places are recorded as ambiguous, and `callers_of` and
  `blast_radius` label their answers `lower-bound` when such calls might reach the target.
- **Values are not followed.** Method calls on variables resolve only when the variable's
  type is known, from an annotation or an assignment like `db = Database()`. Functions
  passed around as callbacks, stored in dicts or wrapped by decorators are not followed.
- **Inheritance is followed statically.** A method is looked up through the MRO of the
  class the code declares, not the object's actual class. Calls to a base-class method
  through `self`, `cls` or a variable typed as the base are listed as possible callers of
  each project override, and make the answer `lower-bound`. A base outside the project,
  such as `dict` or a library class, blocks the lookup: an inherited method not found
  before it stays unresolved, and answers that it might reach are labelled `lower-bound`.
- **The dead-code list over-reports.** Entry points, `__init__` methods, properties and
  functions passed as values or reached only through unresolved calls show up in
  `orphan_functions`.
- **The graph is held in memory** by rdflib. The practical ceiling is about 5,000 files; the
  largest project measured so far is rich, 190 files in about 24 seconds.

## Design notes

See [`docs/architecture.md`](docs/architecture.md) for the ontology and the reasoning behind
the main design choices, and [`docs/roadmap.md`](docs/roadmap.md) for planned work.

## Licence

Not yet chosen.
