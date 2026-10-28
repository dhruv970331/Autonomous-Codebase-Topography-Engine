"""
Command line.
`acte build PATH` parses a project into one Turtle graph, with ids relative to PATH.
`acte stats GRAPH.ttl` summarises a graph written by build, as text or JSON.
`acte impact PATH` lists what the uncommitted changes (or those since --base) may break.
`acte serve GRAPH.ttl` answers questions about a graph as an MCP server over stdio.
`acte analyze GRAPH.ttl` ranks definitions and finds import cycles and subsystems.
"""

import argparse
import contextlib
import json
import os
import subprocess
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path

from rdflib import Graph, Literal
from rdflib.namespace import RDF, XSD

from acte import resolver
from acte.analysis import import_cycles, rank, subsystems
from acte.health import run_checks
from acte.mcp_server import create_server
from acte.parser import EXTENSION_TO_LANGUAGE
from acte.rdf_builder import CODE, build_graph
from acte.sparql_engine import SPARQLEngine

# skipped when walking a directory that is not in a git work tree; hidden dirs are skipped too
_SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "__pycache__", ".ipynb_checkpoints",
              ".tox", "dist", "build", "site-packages"}


def _git(root: Path, *args: str) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(["git", "-C", str(root), *args], check=False, capture_output=True, text=True)
    except FileNotFoundError:  # no git installed: treat as not a work tree
        return None


def source_files(root: Path) -> tuple[list[str], str | None]:
    """The files under *root* to parse, as sorted POSIX paths relative to it, and the git HEAD sha
    (None outside a git work tree or before the first commit). In a work tree git lists them,
    honouring every .gitignore; otherwise the tree is walked, skipping _SKIP_DIRS and hidden dirs."""
    inside = _git(root, "rev-parse", "--is-inside-work-tree")
    commit = None
    if inside and inside.returncode == 0 and inside.stdout.strip() == "true":
        listed = _git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").stdout.split("\0")
        head = _git(root, "rev-parse", "HEAD")
        commit = head.stdout.strip() if head.returncode == 0 else None
    else:
        listed = []
        for d, dirs, names in os.walk(root):
            dirs[:] = [x for x in dirs if x not in _SKIP_DIRS and not x.startswith(".")]
            listed += [(Path(d) / n).relative_to(root).as_posix() for n in names]
    files = sorted(
        f for f in listed
        if Path(f).suffix.lower() in EXTENSION_TO_LANGUAGE
        and ".ipynb_checkpoints" not in Path(f).parts
        and (root / f).is_file()  # a tracked file deleted from the work tree is still listed by git
    )
    return files, commit


def build(root: Path, use_jedi: bool = True, jobs: int = 1) -> Graph:
    """The graph of the project at *root*, plus one code:Build node describing the run (root is the directory's name only)."""
    files, commit = source_files(root)
    # The parser records the path it is given, so parsing relative paths from inside root makes
    # every id relative; jedi and the resolver resolve those against the working directory.
    # shortcut: chdir is process-wide so two builds cannot run at once in one process, pass a display path through the parser when that is needed.
    with contextlib.chdir(root):
        graph = build_graph([Path(f) for f in files], Path("."), use_jedi, jobs)
    meta = CODE["build"]
    graph.add((meta, RDF.type, CODE.Build))
    graph.add((meta, CODE.root, Literal(root.resolve().name, datatype=XSD.string)))
    graph.add((meta, CODE.acteVersion, Literal(version("acte"), datatype=XSD.string)))
    if commit:
        graph.add((meta, CODE.commit, Literal(commit, datatype=XSD.string)))
    if use_jedi and resolver.jedi:  # the resolver runs tier TJ exactly when both hold
        graph.add((meta, CODE.jediVersion, Literal(resolver.jedi.__version__, datatype=XSD.string)))
    return graph


def _counts(graph: Graph) -> tuple[dict, dict]:
    """The file, function, class and call-site counts of *graph*, and its coverage."""
    cov = SPARQLEngine(graph=graph).coverage()
    return {"files": len(set(graph.subjects(RDF.type, CODE.File))), "functions": len(set(graph.subjects(RDF.type, CODE.Function))),
            "classes": len(set(graph.subjects(RDF.type, CODE.Class))), "call_sites": cov["total"]}, cov


def _summary(graph: Graph) -> tuple[dict, dict[str, list[dict]]]:
    """Print the counts and coverage rate of *graph*; return its coverage and health check results."""
    counts, cov = _counts(graph)
    for label, key in (("files parsed", "files"), ("functions", "functions"), ("classes", "classes"), ("call sites", "call_sites")):
        print(f"{label}: {counts[key]}")
    if cov["no_resolver"]:
        print(f"call sites in languages without call resolution: {cov['no_resolver']}")
    rate = cov["rate"]
    print(f"coverage: {'n/a' if rate is None else f'{rate:.1%}'}")
    return cov, run_checks(graph)


def _row(row: dict) -> dict:
    """A health check row with its code URIs shown as their ids, as SPARQLEngine._id decodes them."""
    return {k: SPARQLEngine()._id(v) if v.startswith(str(CODE)) else v for k, v in row.items()}


def _stats(graph: Graph, fmt: str = "text") -> int:
    """Print the build metadata, summary, coverage breakdown and health of *graph*; 1 if a health check fails."""
    meta = CODE["build"]
    built = {prop: str(value) for prop in ("root", "commit", "acteVersion", "jediVersion")
             if (value := graph.value(meta, CODE[prop])) is not None}
    if fmt == "json":
        counts, cov = _counts(graph)
        failing = {name: [_row(r) for r in rows] for name, rows in run_checks(graph).items() if rows}
        print(json.dumps({"build": built, **counts, "coverage": cov, "health": "failing" if failing else "ok",
                          "failing": failing}, indent=2))
        return 1 if failing else 0
    for prop, value in built.items():
        print(f"{prop}: {value}")
    cov, checks = _summary(graph)
    for key in ("status", "tier", "reason"):
        print(f"{key}: {', '.join(f'{k} {n}' for k, n in cov[key].items()) or 'none'}")
    failing = {name: rows for name, rows in checks.items() if rows}
    print(f"health: {'ok' if not failing else 'failing'}")
    for name, rows in failing.items():
        print(f"  {name}: {len(rows)} rows")
        for row in rows[:5]:
            print("    " + ", ".join(f"{k}={v}" for k, v in _row(row).items()))
        if len(rows) > 5:
            print(f"    ... and {len(rows) - 5} more")
    return 1 if failing else 0


def _analyze(graph: Graph, top: int, fmt: str) -> None:
    """Print the ranking, import cycles and subsystems of *graph*, as text or as one JSON object."""
    ranking, cycles, groups = rank(graph, top), import_cycles(graph), subsystems(graph)
    if fmt == "json":
        print(json.dumps({"rank": asdict(ranking), "import_cycles": [asdict(c) for c in cycles],
                          "subsystems": [asdict(s) for s in groups]}, indent=2))
        return
    print(f"most depended on: top {len(ranking.top)} of {ranking.total} functions and classes, by PageRank")
    if not ranking.converged:
        print("  PageRank did not settle in 100 rounds, so the scores are approximate")
    for i, r in enumerate(ranking.top, 1):
        print(f"  {i}. {r.file_path}:{r.line} {r.id.split('::', 1)[1]} (score {r.score:.4f}, {r.callers} direct callers)")
    print(f"import cycles: {len(cycles) or 'none'}")
    for c in cycles:
        print(f"  {len(c.files)} files: {', '.join(c.files)}")
    # shortcut: a subsystem of one file is taken to be a file with no links; Louvain could in theory leave a linked file alone
    linked = [s for s in groups if s.size > 1]
    print(f"subsystems: {len(linked) or 'none'}")
    for s in linked:
        more = f", and {s.size - len(s.files)} more" if s.size > len(s.files) else ""
        print(f"  {s.hub}: {s.size} files in {s.folder}: {', '.join(s.files)}{more}")
    if lone := len(groups) - len(linked):
        print(f"{lone} file{'s' if lone > 1 else ''} with no links to other files")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acte")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="parse a project into a Turtle graph")
    b.add_argument("path", help="the project root")
    b.add_argument("-o", "--output", default="acte-graph.ttl", help="Turtle file to write (default: acte-graph.ttl)")
    b.add_argument("--no-jedi", action="store_true", help="skip the jedi resolution tier")
    b.add_argument("--jobs", type=int, default=min(os.cpu_count() or 1, 8),
                   help="processes that parse files; 1 parses in this process (default: CPU count, at most 8)")
    st = sub.add_parser("stats", help="summarise a Turtle graph written by build")
    st.add_argument("graph", help="the Turtle file")
    st.add_argument("--format", choices=["text", "json"], default="text", help="output format (default: text)")
    im = sub.add_parser(
        "impact", help="list what a git diff may break",
        description="Map each line the diff of the work tree against BASE changes to the function or class that holds it, "
                    "and list what calls those. Definitions the change deleted are not detected: they are not in the new graph.")
    im.add_argument("path", nargs="?", default=".", help="the project root, as for build (default: .)")
    im.add_argument("--base", default="HEAD", help="the git revision to diff against (default: HEAD, so uncommitted work)")
    im.add_argument("--graph", help="a Turtle file written by build, used instead of building the work tree afresh")
    im.add_argument("--depth", type=int, default=2, help="hops of callers to follow, 1 to 10 (default: 2)")
    im.add_argument("--limit", type=int, default=50, help="most affected entries listed per change (default: 50)")
    im.add_argument("--format", choices=["text", "json"], default="text", help="output format (default: text)")
    im.add_argument("--no-jedi", action="store_true", help="skip the jedi resolution tier when building")
    sv = sub.add_parser("serve", help="serve a Turtle graph as an MCP server over stdio")
    sv.add_argument("graph", help="the Turtle file")
    sv.add_argument("--project", help="the project root the graph was built from")
    sv.add_argument("--profile", choices=["small", "full"], default="small", help="which tools to offer (default: small)")
    an = sub.add_parser("analyze", help="rank functions and classes, find import cycles and subsystems")
    an.add_argument("graph", help="the Turtle file")
    an.add_argument("--top", type=int, default=10, help="how many ranked functions and classes to show (default: 10)")
    an.add_argument("--format", choices=["text", "json"], default="text", help="output format (default: text)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        try:
            server = create_server(args.graph, args.project, args.profile)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # rdflib's turtle BadSyntax is a SyntaxError
            parser.error(f"cannot read {args.graph}: {exc}")
        server.run("stdio")
        return 0

    if args.command in ("stats", "analyze"):
        if args.command == "analyze" and args.top < 1:
            parser.error("--top must be at least 1")
        try:
            graph = Graph().parse(args.graph, format="turtle")
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # rdflib's turtle BadSyntax is a SyntaxError
            parser.error(f"cannot read {args.graph}: {exc}")
        if args.command == "stats":
            return _stats(graph, args.format)
        _analyze(graph, args.top, args.format)
        return 0

    root = Path(args.path)
    if not root.is_dir():
        parser.error(f"{args.path} is not a directory")
    if args.command == "impact":
        if args.limit < 1:
            parser.error("--limit must be at least 1")
        from acte.impact import run  # imported here: impact imports this module
        return run(root, args.base, args.graph, args.depth, args.limit, args.format, not args.no_jedi)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    graph = build(root, not args.no_jedi, args.jobs)
    # Write beside the output then rename, so a failed write never leaves a half-written map.
    # A plain name, not tempfile's: tempfile creates mode 0600, which the rename would keep.
    tmp = Path(args.output).with_name(f".{Path(args.output).name}.{os.getpid()}.tmp")
    try:
        graph.serialize(destination=tmp, format="turtle")
        os.replace(tmp, args.output)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"wrote {args.output}")
    _, checks = _summary(graph)
    failing = [name for name, rows in checks.items() if rows]
    print(f"health: {'ok' if not failing else ', '.join(failing)}")
    return 1 if failing else 0
