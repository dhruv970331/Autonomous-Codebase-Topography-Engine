"""
RDF Knowledge Graph Builder.
Translates AST structural data (NodeInfo / EdgeInfo) into a formal Semantic Web
graph for SPARQL querying.
"""

import gzip
import hashlib
import json
import os
import shutil
import time
import urllib.parse
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import pyoxigraph as ox

from acte.parser import CodeParser, EdgeInfo, NodeInfo, ParseResult
from acte.resolver import CallResolution, Resolution, SymbolIndex, resolve

Graph = ox.Store  # a map: facts in the default graph of a pyoxigraph store


class Namespace(str):
    """An IRI prefix: ``NS.x`` and ``NS["x"]`` are the node whose IRI is the prefix plus x."""

    def __getattr__(self, local: str) -> ox.NamedNode:
        if local.startswith("__"):
            raise AttributeError(local)
        return ox.NamedNode(self + local)

    def __getitem__(self, local: str) -> ox.NamedNode:
        return ox.NamedNode(self + local)


# Formal namespace for the codebase topography ontology
CODE = Namespace("http://acte.local/code#")
RDF = Namespace("http://www.w3.org/1999/02/22-rdf-syntax-ns#")
RDFS = Namespace("http://www.w3.org/2000/01/rdf-schema#")
_DEFAULT = ox.DefaultGraph()


# Terms of a map. Every module makes and reads terms through these, so the store behind them can change in one place.
def lit(value: str | int | bool) -> ox.Literal:
    """A literal typed by its Python type: xsd:boolean, xsd:integer or xsd:string."""
    return ox.Literal(value)


def iri(text: str) -> ox.NamedNode:
    """The node with the full IRI *text*."""
    return ox.NamedNode(text)


def text(term) -> str:
    """An IRI's text, or a literal's value as written in the map; a str as it is, and "None" for None."""
    return str(term) if term is None or isinstance(term, str) else term.value


def is_blank(term) -> bool:
    return isinstance(term, ox.BlankNode)


def nt(term) -> str:
    """*term* as N-Triples and SPARQL write it, e.g. ``<http://...>``."""
    return str(term)


# Lookups and edits. None matches anything; remove() takes away every fact matching the pattern.
def new_graph() -> Graph:
    return ox.Store()


def triples(graph: Graph, s=None, p=None, o=None):
    return ((q.subject, q.predicate, q.object) for q in graph.quads_for_pattern(s, p, o, _DEFAULT))


def subjects(graph: Graph, p=None, o=None):
    return (q.subject for q in graph.quads_for_pattern(None, p, o, _DEFAULT))


def objects(graph: Graph, s=None, p=None):
    return (q.object for q in graph.quads_for_pattern(s, p, None, _DEFAULT))


def value(graph: Graph, s, p):
    """One object of (s, p), or None."""
    return next(objects(graph, s, p), None)


def has(graph: Graph, s=None, p=None, o=None) -> bool:
    return next(graph.quads_for_pattern(s, p, o, _DEFAULT), None) is not None


def add(graph: Graph, s, p, o) -> None:
    graph.add(ox.Quad(s, p, o))


def remove(graph: Graph, s=None, p=None, o=None) -> None:
    for q in list(graph.quads_for_pattern(s, p, o, _DEFAULT)):
        graph.remove(q)


def load(path: str | Path) -> Graph:
    """The map at *path*: the database folder of a map folder written by ``acte build``, opened read-only, or a kept
    ``.nt.gz`` file or a Turtle file, read into memory."""
    path = Path(path)
    if path.is_dir():
        return ox.Store.read_only(str(path / (path / "current").read_text().strip() / "db"))
    graph = ox.Store()
    if path.name.endswith(".gz"):
        with gzip.open(path) as f:
            graph.bulk_load(f, format=ox.RdfFormat.N_TRIPLES)
    else:
        graph.bulk_load(path=str(path), format=ox.RdfFormat.TURTLE)
    return graph


MAP_FORMAT = 2  # code:mapFormat of the maps this ACTE builds; 2 = with Definition records


def load_map(path: str | Path) -> Graph:
    """The map *path* written by ``acte build``; a ValueError when an older ACTE built it."""
    return _same_format(load(path))


def _same_format(graph: Graph) -> Graph:
    if value(graph, CODE["build"], CODE.mapFormat) != lit(MAP_FORMAT):
        raise ValueError("built by an older ACTE. Run acte build again.")
    return graph


def stored_summary(graph: Graph) -> dict | None:
    """The summary ``acte build`` stored in *graph*, or None when there is no readable one."""
    stored = value(graph, CODE["build"], CODE.summary)
    try:
        summary = None if stored is None else json.loads(text(stored))
    except ValueError:
        return None
    return summary if isinstance(summary, dict) else None


def stored_count(graph: Graph) -> int | None:
    """The fact count ``acte build`` stored in *graph*'s summary, or None when there is no readable one."""
    return (stored_summary(graph) or {}).get("triples")


def subfolder(path: str | Path) -> Path | None:
    """The subfolder ``current`` names in the map folder *path*, or None when *path* is a single map file."""
    path = Path(path)
    return path / (path / "current").read_text().strip() if path.is_dir() else None


def open_checked(path: str | Path) -> tuple[Graph, int]:
    """The map at *path* and its fact count, counted once, for a server that will answer from it.

    A map folder's database is rebuilt from its kept file when it is missing, will not open, or does not hold the
    count its build stored. A ValueError when the map itself does not hold that count: it was changed after its
    build. A map with no stored count (from an older ACTE) opens as it is."""
    path = Path(path)
    if not path.is_dir():
        graph = load_map(path)
        size, stored = len(graph), stored_count(graph)
        if stored not in (None, size):
            raise ValueError(f"it holds {size} facts but its build stored {stored}: the map was changed after its build. "
                             "Run acte build again.")
        return graph, size
    sub = subfolder(path)
    try:
        graph = ox.Store.read_only(str(sub / "db"))
        size = len(graph)
    except Exception:  # noqa: BLE001 - any folder that will not open is rebuilt: a pyoxigraph 0.4 one raises RuntimeError
        graph = None
    if graph is not None and size and stored_count(graph) == size:
        return _same_format(graph), size
    graph = None  # closes it before the rebuild
    new = sub / f".db.{os.getpid()}.new"
    shutil.rmtree(new, ignore_errors=True)
    graph = ox.Store(str(new))
    try:
        with gzip.open(sub / "map.nt.gz") as f:
            graph.bulk_load(f, format=ox.RdfFormat.N_TRIPLES)
        graph.flush()
        size, stored = len(graph), stored_count(graph)
        del graph
        if stored is not None and stored != size:
            raise ValueError(f"its kept file holds {size} facts but its build stored {stored}: the map was changed "
                             "after its build. Run acte build again.")
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    # swap: a non-empty folder cannot be renamed over another, so the old one is moved aside first
    old = sub / f".db.{os.getpid()}.old"
    if (sub / "db").exists():
        os.rename(sub / "db", old)
    os.rename(new, sub / "db")
    shutil.rmtree(old, ignore_errors=True)
    return _same_format(ox.Store.read_only(str(sub / "db"))), size


def sorted_lines(graph: Graph) -> list[bytes]:
    """The lines of *graph*'s kept file: its facts as N-Triples, sorted as bytes, without newlines."""
    lines = graph.dump(format=ox.RdfFormat.N_TRIPLES, from_graph=_DEFAULT).split(b"\n")[:-1]
    lines.sort()
    return lines


def nt_line(s, p, o) -> bytes:
    """The fact (s, p, o) as its line in the kept file, without the newline."""
    return ox.serialize([ox.Triple(s, p, o)], format=ox.RdfFormat.N_TRIPLES).rstrip(b"\n")


def map_id(lines: list[bytes]) -> str:
    """The SHA-256 of *lines* as the kept file writes them: the id ``acte build`` stores in the summary, made from
    every line but the summary's own."""
    digest = hashlib.sha256(b"\n".join(lines))
    digest.update(b"\n" if lines else b"")
    return digest.hexdigest()


def save_json(data: dict, path: str | Path) -> None:
    """Write *data* to *path* as a side file of the map: JSON with sorted keys, gzip level 1 with no time or file
    name in the header, so the same map always gives the same bytes."""
    body = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0, filename="") as f:
        f.write(body)


def read_json(path: str | Path) -> dict:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def save(graph: Graph, path: str | Path, lines: list[bytes] | None = None) -> None:
    """Write *graph* to *path* as the kept file: N-Triples lines sorted as bytes, gzip level 1 with no time or
    file name in the header, so the same map always gives the same bytes. *lines*: its sorted_lines(), if known."""
    lines = sorted_lines(graph) if lines is None else lines
    with open(path, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0, filename="") as f:
        if lines:  # two writes: one more copy of the joined lines would be the size of the map
            f.write(b"\n".join(lines))
            f.write(b"\n")


def _write_new(path: Path, write) -> None:
    """Call write(tmp) on a temporary file beside *path*, then rename it to *path*: a failed write leaves no half file."""
    # A plain name, not tempfile's: tempfile creates mode 0600, which the rename would keep.
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def check_map_folder(folder: str | Path) -> None:
    """A ValueError if *folder* exists and is not a map folder: a file, or a folder with other things in it but no
    ``current``. A build never deletes what another program keeps there."""
    folder = Path(folder)
    if folder.exists() and not (folder / "current").is_file() and (not folder.is_dir() or any(folder.iterdir())):
        raise ValueError("it exists and is not a map folder")


def fill_db(graph: Graph, folder: Path) -> None:
    """Make the database folder *folder* holding the facts of *graph*."""
    db = ox.Store(str(folder))
    db.bulk_extend(graph)
    db.flush()
    del db  # closes it


def save_map(graph: Graph, folder: str | Path, lines: list[bytes] | None = None, index: dict | None = None,
             links: dict | None = None) -> None:
    """Write *graph* as the map folder *folder*: a new subfolder with the kept file ``map.nt.gz``, the database
    folder ``db`` and the side files ``index.json.gz`` holding *index* plus the kept file's SHA-256 and
    ``links.json.gz`` holding *links* (each when given), then the file ``current`` naming it. Old subfolders are deleted, but the one ``current`` named before stays: a running
    server may still read it. A ValueError if *folder* is not a map folder (check_map_folder). *lines*: as for save()."""
    folder = Path(folder)
    check_map_folder(folder)
    folder.mkdir(parents=True, exist_ok=True)
    current = folder / "current"
    before = current.read_text().strip() if current.exists() else None
    name = f"map-{time.time_ns()}-{os.getpid()}"  # never part of the map, so the same code gives the same kept file
    new = folder / name
    new.mkdir()
    try:
        _write_new(new / "map.nt.gz", lambda tmp: save(graph, tmp, lines))
        if index is not None:
            with open(new / "map.nt.gz", "rb") as f:
                index = {**index, "keptSha256": hashlib.file_digest(f, "sha256").hexdigest()}
            _write_new(new / "index.json.gz", lambda tmp: save_json(index, tmp))
        if links is not None:
            _write_new(new / "links.json.gz", lambda tmp: save_json(links, tmp))
        del lines, index, links  # each about the size of the map: freed now if the caller holds no other reference
        fill_db(graph, new / "db")
        _write_new(current, lambda tmp: tmp.write_text(name + "\n"))
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    for old in folder.glob("map-*"):
        if old.is_dir() and old.name not in (name, before):
            shutil.rmtree(old)


def query_rows(graph: Graph, query: str) -> list[dict[str, str]]:
    """The rows of a SELECT as {variable: text}, without the variables a row leaves unbound."""
    result = graph.query(query)
    names = [v.value for v in result.variables]
    return [{n: text(row[n]) for n in names if row[n] is not None} for row in result]

# An import's kind (parser EdgeInfo.extra["import_kind"]) -> its (project file, outside module) predicates
_IMPORT_PREDICATES = {
    "module":      (CODE.importsFrom, CODE.importsExternal),
    "type-only":   (CODE.importsTypeOnly, CODE.importsTypeOnlyExternal),
    "in-function": (CODE.importsInFunction, CODE.importsInFunctionExternal),
    "main-block":  (CODE.importsInMainBlock, CODE.importsInMainBlockExternal),
}


class KnowledgeGraphBuilder:
    """Constructs an RDF Knowledge Graph from extracted AST nodes and edges.

    Usage pattern — single file:
        builder = KnowledgeGraphBuilder()
        builder.build(nodes, edges)
        graph = builder.graph

    Usage pattern — whole repository (many files):
        builder = KnowledgeGraphBuilder()
        for path in repo_files:
            result = parser.parse_file(path)
            builder.build(result.nodes, result.edges)   # accumulates across calls
        save(builder.graph, "map.nt.gz")

    Call reset() between runs if you need a clean graph without creating a new
    instance (e.g. in tests or repeated ingestion jobs).
    """

    def __init__(self) -> None:
        self.graph = new_graph()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def build(self, nodes: list[NodeInfo], edges: list[EdgeInfo], resolution: Resolution | None = None) -> Graph:
        """Translate NodeInfo / EdgeInfo objects, and the CallSites of *resolution* if given, into RDF triples.

        Imports and inheritance need the index of *resolution*; without it they are left out.

        Triples are *accumulated* into self.graph across repeated calls so that
        an entire repository can be ingested file-by-file. Call reset() first
        if you want to start from a clean slate.
        """
        for node in nodes:
            self._add_node(node)
        for edge in edges:
            self._add_edge(edge, resolution.index if resolution else None)
        for cls, bases in sorted(resolution.index.bases.items()) if resolution else []:
            for base in bases:
                if base in resolution.index.by_class:  # project bases only; "external" and "unknown" draw no arrow
                    add(self.graph, self._make_uri(cls), CODE.inherits, self._make_uri(base))
        for call in resolution.calls if resolution else []:
            self._add_call(call)
        return self.graph

    def reset(self) -> None:
        """Clear all triples and start fresh."""
        self.graph = new_graph()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _node_id(self, node: NodeInfo) -> str:
        """Return the canonical string identifier for a node (see NodeInfo.qualified_id)."""
        return node.qualified_id

    def _make_uri(self, identifier: str) -> ox.NamedNode:
        """Encode an arbitrary code identifier into a valid RDF URI."""
        # "#" is encoded: the namespace's "#" must stay the only one in the IRI.
        safe = urllib.parse.quote(identifier, safe="/:.@")
        return CODE[safe]

    def _add_node(self, node: NodeInfo) -> None:
        uri = self._make_uri(self._node_id(node))

        # rdf:type
        type_map = {
            "Class":    CODE.Class,
            "Function": CODE.Function,
            "File":     CODE.File,
        }
        rdf_type = type_map.get(node.kind)
        if rdf_type:
            add(self.graph, uri, RDF.type, rdf_type)

        # Literal properties
        add(self.graph, uri, CODE.name,      lit(node.name))
        add(self.graph, uri, CODE.filePath,  lit(node.file_path))
        add(self.graph, uri, CODE.lineStart, lit(node.line_start))
        add(self.graph, uri, CODE.lineEnd,   lit(node.line_end))
        if node.decorator_line is not None:
            add(self.graph, uri, CODE.decoratorLine, lit(node.decorator_line))
        if node.kind in ("Function", "Class"):
            # One record per definition: an id defined twice (both branches of an if) keeps each one's lines together.
            # _make_uri always escapes "!", so no identifier gives this IRI.
            record = iri(f"{text(uri)}!{node.line_start}")
            add(self.graph, uri, CODE.definedAt, record)
            add(self.graph, record, RDF.type, CODE.Definition)
            add(self.graph, record, CODE.lineStart, lit(node.line_start))
            add(self.graph, record, CODE.lineEnd, lit(node.line_end))
            if node.decorator_line is not None:
                add(self.graph, record, CODE.decoratorLine, lit(node.decorator_line))

        if node.language:
            add(self.graph, uri, CODE.language, lit(node.language))

    def _add_edge(self, edge: EdgeInfo, index: SymbolIndex | None = None) -> None:
        source_uri = self._make_uri(edge.source)

        if edge.kind == "CONTAINS":
            add(self.graph, source_uri, CODE.contains, self._make_uri(edge.target))
        elif edge.kind == "IMPORTS_FROM" and index and edge.source.endswith(".py"):  # the index only knows Python modules
            files = index.module_files(edge.target, edge.source)
            project, external = _IMPORT_PREDICATES[edge.extra.get("import_kind", "module")]
            if len(files) == 1:
                add(self.graph, source_uri, project, self._make_uri(files[0]))
            elif not files and not edge.target.startswith("."):  # a relative import is inside the project, never external
                add(self.graph, source_uri, external, lit(edge.target))
            # several files: ambiguous, so no arrow

    def _add_call(self, r: CallResolution) -> None:
        """One code:CallSite record, plus the calls / instantiates arrow when resolved (spec §7, D9, R11)."""
        call = r.call
        uri = self._make_uri(f"{call.caller_id}@L{call.line}c{call.col}")
        caller = self._make_uri(call.caller_id)
        g = self.graph
        add(g, uri, RDF.type, CODE.CallSite)
        add(g, uri, CODE.inFunction, caller)
        add(g, uri, CODE.line, lit(call.line))
        add(g, uri, CODE.column, lit(call.col))
        add(g, uri, CODE.targetName, lit(call.target_name))
        if call.receiver is not None:
            add(g, uri, CODE.receiver, lit(call.receiver))
        add(g, uri, CODE.status, lit(r.status))
        if r.reason:
            add(g, uri, CODE.reason, lit(r.reason))
        if r.status == "resolved":
            target = self._make_uri(r.target)
            add(g, uri, CODE.tier, lit(r.tier))
            add(g, uri, CODE.resolvedTo, target)
            if r.init:
                add(g, uri, CODE.initTarget, self._make_uri(r.init))
            if r.init_unknown:
                add(g, uri, CODE.initUnknown, lit(True))
            if r.recursive:
                add(g, uri, CODE.recursive, lit(True))
            else:  # R11: a recursive call keeps its record but draws no self-edge
                add(g, caller, CODE[r.edge], target)
        elif r.status == "ambiguous":
            add(g, uri, CODE.candidateCount, lit(r.candidate_count))
            for c in r.candidates:
                add(g, uri, CODE.candidate, self._make_uri(c))


# below this many files a process pool costs more than it saves
MIN_PARALLEL_FILES = 50
_worker_parser: CodeParser | None = None


def _parse_in_worker(path: Path) -> ParseResult:
    global _worker_parser
    if _worker_parser is None:  # set already when the worker was forked from the parent
        _worker_parser = CodeParser()
    return _worker_parser.parse_file(path)


def build_graph(paths: list[Path], root: Path, use_jedi: bool = True, jobs: int = 1) -> Graph:
    """The whole pipeline over *paths*: parse, resolve calls across all of them, build one graph.
    With *jobs* > 1 and enough files, files are parsed in that many processes; the results keep the order of *paths*."""
    global _worker_parser
    parser = CodeParser()
    if jobs <= 1 or len(paths) < MIN_PARALLEL_FILES:
        results = [parser.parse_file(p) for p in paths]
    else:
        # Load every grammar here first, so workers never race to download or cache one in HOME.
        for language in {parser.detect_language(p) for p in paths} - {None}:
            parser._get_parser(language)
        _worker_parser = parser
        try:
            # relative paths resolve against the working directory, so each worker starts in this one, whatever the start method
            # The pool starts before any store opens: a database folder runs threads, and forking a process that runs threads can hang
            with ProcessPoolExecutor(jobs, initializer=os.chdir, initargs=(os.getcwd(),)) as pool:
                results = list(pool.map(_parse_in_worker, paths, chunksize=max(1, len(paths) // (jobs * 4))))
        finally:
            _worker_parser = None
    nodes = [n for r in results for n in r.nodes]
    edges = [e for r in results for e in r.edges]
    return KnowledgeGraphBuilder().build(nodes, edges, resolve(results, root, use_jedi))
