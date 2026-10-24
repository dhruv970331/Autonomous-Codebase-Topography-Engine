"""
Command line.
`acte build PATH` parses a project into one Turtle graph, with ids relative to PATH.
`acte stats GRAPH.ttl` summarises a graph written by build.
`acte serve GRAPH.ttl` answers questions about a graph as an MCP server over stdio.
"""

import argparse
import contextlib
import os
import subprocess
from importlib.metadata import version
from pathlib import Path

from rdflib import Graph, Literal
from rdflib.namespace import RDF, XSD

from acte import resolver
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


def build(root: Path, use_jedi: bool = True) -> Graph:
    """The graph of the project at *root*, plus one code:Build node describing the run (root is the directory's name only)."""
    files, commit = source_files(root)
    # The parser records the path it is given, so parsing relative paths from inside root makes
    # every id relative; jedi and the resolver resolve those against the working directory.
    # shortcut: chdir is process-wide so two builds cannot run at once in one process, pass a display path through the parser when that is needed.
    with contextlib.chdir(root):
        graph = build_graph([Path(f) for f in files], Path("."), use_jedi)
    meta = CODE["build"]
    graph.add((meta, RDF.type, CODE.Build))
    graph.add((meta, CODE.root, Literal(root.resolve().name, datatype=XSD.string)))
    graph.add((meta, CODE.acteVersion, Literal(version("acte"), datatype=XSD.string)))
    if commit:
        graph.add((meta, CODE.commit, Literal(commit, datatype=XSD.string)))
    if use_jedi and resolver.jedi:  # the resolver runs tier TJ exactly when both hold
        graph.add((meta, CODE.jediVersion, Literal(resolver.jedi.__version__, datatype=XSD.string)))
    return graph


def _summary(graph: Graph) -> tuple[dict, dict[str, list[dict]]]:
    """Print the counts and coverage rate of *graph*; return its coverage and health check results."""
    cov = SPARQLEngine(graph=graph).coverage()
    print(f"files parsed: {len(set(graph.subjects(RDF.type, CODE.File)))}")
    print(f"functions: {len(set(graph.subjects(RDF.type, CODE.Function)))}")
    print(f"classes: {len(set(graph.subjects(RDF.type, CODE.Class)))}")
    print(f"call sites: {cov['total']}")
    if cov["no_resolver"]:
        print(f"call sites in languages without call resolution: {cov['no_resolver']}")
    rate = cov["rate"]
    print(f"coverage: {'n/a' if rate is None else f'{rate:.1%}'}")
    return cov, run_checks(graph)


def _stats(graph: Graph) -> int:
    """Print the build metadata, summary, coverage breakdown and health of *graph*; 1 if a health check fails."""
    meta = CODE["build"]
    for prop in ("root", "commit", "acteVersion", "jediVersion"):
        if (value := graph.value(meta, CODE[prop])) is not None:
            print(f"{prop}: {value}")
    cov, checks = _summary(graph)
    for key in ("status", "tier", "reason"):
        print(f"{key}: {', '.join(f'{k} {n}' for k, n in cov[key].items()) or 'none'}")
    failing = {name: rows for name, rows in checks.items() if rows}
    print(f"health: {'ok' if not failing else 'failing'}")
    engine = SPARQLEngine()
    for name, rows in failing.items():
        print(f"  {name}: {len(rows)} rows")
        for row in rows[:5]:  # code URIs shown as their ids, as SPARQLEngine._id decodes them
            print("    " + ", ".join(f"{k}={engine._id(v) if v.startswith(str(CODE)) else v}" for k, v in row.items()))
        if len(rows) > 5:
            print(f"    ... and {len(rows) - 5} more")
    return 1 if failing else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="acte")
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build", help="parse a project into a Turtle graph")
    b.add_argument("path", help="the project root")
    b.add_argument("-o", "--output", default="acte-graph.ttl", help="Turtle file to write (default: acte-graph.ttl)")
    b.add_argument("--no-jedi", action="store_true", help="skip the jedi resolution tier")
    st = sub.add_parser("stats", help="summarise a Turtle graph written by build")
    st.add_argument("graph", help="the Turtle file")
    sv = sub.add_parser("serve", help="serve a Turtle graph as an MCP server over stdio")
    sv.add_argument("graph", help="the Turtle file")
    sv.add_argument("--project", help="the project root the graph was built from")
    sv.add_argument("--profile", choices=["small", "full"], default="small", help="which tools to offer (default: small)")
    args = parser.parse_args(argv)

    if args.command == "serve":
        try:
            server = create_server(args.graph, args.project, args.profile)
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # rdflib's turtle BadSyntax is a SyntaxError
            parser.error(f"cannot read {args.graph}: {exc}")
        server.run("stdio")
        return 0

    if args.command == "stats":
        try:
            graph = Graph().parse(args.graph, format="turtle")
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:  # rdflib's turtle BadSyntax is a SyntaxError
            parser.error(f"cannot read {args.graph}: {exc}")
        return _stats(graph)

    root = Path(args.path)
    if not root.is_dir():
        parser.error(f"{args.path} is not a directory")
    graph = build(root, not args.no_jedi)
    graph.serialize(destination=args.output, format="turtle")
    print(f"wrote {args.output}")
    _, checks = _summary(graph)
    failing = [name for name, rows in checks.items() if rows]
    print(f"health: {'ok' if not failing else ', '.join(failing)}")
    return 1 if failing else 0
