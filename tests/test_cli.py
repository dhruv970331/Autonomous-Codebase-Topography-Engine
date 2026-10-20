import subprocess
from importlib.metadata import version

import pytest
from rdflib import Graph
from rdflib.namespace import RDF

from acte.cli import main
from acte.rdf_builder import CODE

# run() calls x.m() on the result of make(), which only jedi can follow (tier TJ)
PROJECT = {
    "shop/__init__.py": "",
    "shop/models.py": "class A:\n    def m(self):\n        pass\n\n\ndef make():\n    return A()\n",
    "app.py": "from shop.models import make\n\n\ndef run():\n    x = make()\n    x.m()\n",
}
SUMMARY = ["files parsed: 3", "functions: 3", "classes: 1", "call sites: 3"]


@pytest.fixture(autouse=True)
def _no_outer_repo(tmp_path, monkeypatch):
    # a git work tree that happens to enclose tmp_path must not be found
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))


def _write(root, files):
    for rel, code in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(code)


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


def _build(capsys, root, out, *flags):
    assert main(["build", str(root), "-o", str(out), *flags]) == 0
    return Graph().parse(out, format="turtle"), capsys.readouterr().out.splitlines()


def _files(graph):
    return sorted(str(graph.value(f, CODE.filePath)) for f in graph.subjects(None, CODE.File))


def _call(graph, caller, name):
    """(status, resolvedTo id or None, tier or None) of the call to *name* in *caller*."""
    site = next(s for s in graph.subjects(CODE.inFunction, CODE[caller]) if str(graph.value(s, CODE.targetName)) == name)
    target = graph.value(site, CODE.resolvedTo)
    return (str(graph.value(site, CODE.status)), target and str(target)[len(CODE):],
            graph.value(site, CODE.tier) and str(graph.value(site, CODE.tier)))


def test_git_repo(tmp_path, capsys):
    repo = tmp_path / "repo"
    _write(repo, PROJECT | {".gitignore": "generated/\n", "generated/gen.py": "def g():\n    pass\n", "notes.txt": "text\n",
                            ".ipynb_checkpoints/app-checkpoint.py": "def c():\n    pass\n"})
    _git(repo, "init", "-q")
    _git(repo, "add", "shop", ".gitignore")
    _git(repo, "commit", "-q", "-m", "init")  # app.py stays untracked and is still parsed

    graph, out = _build(capsys, repo, tmp_path / "g.ttl")

    assert _files(graph) == ["app.py", "shop/__init__.py", "shop/models.py"]  # ignored, non-code and checkpoint files absent
    assert _call(graph, "app.py::run", "make") == ("resolved", "shop/models.py::make", "T3")
    assert _call(graph, "app.py::run", "m") == ("resolved", "shop/models.py::A.m", "TJ")  # jedi reads the relative paths
    meta = CODE["build"]
    assert (meta, None, CODE.Build) in graph
    assert str(graph.value(meta, CODE.root)) == "repo"  # the name only: no machine path in the graph
    assert str(graph.value(meta, CODE.commit)) == _git(repo, "rev-parse", "HEAD")
    assert graph.value(meta, CODE.acteVersion) is not None
    assert graph.value(meta, CODE.jediVersion) is not None
    assert out == [f"wrote {tmp_path / 'g.ttl'}", *SUMMARY, "coverage: 100.0%", "health: ok"]


def test_plain_dir_without_jedi(tmp_path, capsys):
    root = tmp_path / "proj"
    _write(root, PROJECT | {".venv/lib/dep.py": "def d():\n    pass\n", "node_modules/x/y.js": "function f() {}\n",
                            ".hidden/h.py": "def h():\n    pass\n", "build/b.py": "def b():\n    pass\n"})

    graph, out = _build(capsys, root, tmp_path / "g.ttl", "--no-jedi")

    assert _files(graph) == ["app.py", "shop/__init__.py", "shop/models.py"]  # skipped and hidden dirs absent
    assert _call(graph, "app.py::run", "m") == ("unresolved", None, None)
    meta = CODE["build"]
    assert str(graph.value(meta, CODE.root)) == "proj"
    assert graph.value(meta, CODE.commit) is None  # not a git work tree
    assert graph.value(meta, CODE.jediVersion) is None  # jedi did not run
    assert out[1:] == [*SUMMARY, "coverage: 66.7%", "health: ok"]


def test_failing_health_check_exits_1(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "proj", PROJECT)
    monkeypatch.setattr("acte.cli.run_checks", lambda graph: {"self-call-arrows": [{"s": "x"}], "callsite-status": []})
    assert main(["build", str(tmp_path / "proj"), "-o", str(tmp_path / "g.ttl"), "--no-jedi"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "health: self-call-arrows"
    assert (tmp_path / "g.ttl").exists()  # the graph is still written


def test_default_output_is_in_the_working_directory(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "proj", PROJECT)
    monkeypatch.chdir(tmp_path)
    assert main(["build", "proj", "--no-jedi"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "wrote acte-graph.ttl"
    assert str(Graph().parse(tmp_path / "acte-graph.ttl", format="turtle").value(CODE["build"], CODE.root)) == "proj"


def test_stats_on_a_built_graph(tmp_path, capsys):
    jedi = pytest.importorskip("jedi")
    repo = tmp_path / "repo"
    _write(repo, PROJECT)
    _git(repo, "init", "-q")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    _build(capsys, repo, tmp_path / "g.ttl")

    assert main(["stats", str(tmp_path / "g.ttl")]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "root: repo", f"commit: {_git(repo, 'rev-parse', 'HEAD')}", f"acteVersion: {version('acte')}",
        f"jediVersion: {jedi.__version__}", *SUMMARY, "coverage: 100.0%",
        "status: resolved 3", "tier: T2 1, T3 1, TJ 1", "reason: none", "health: ok",
    ]


def test_stats_on_a_broken_graph(tmp_path, capsys):
    _write(tmp_path / "proj", PROJECT)
    graph, _ = _build(capsys, tmp_path / "proj", tmp_path / "g.ttl", "--no-jedi")
    graph.remove((CODE["shop/models.py::A"], RDF.type, CODE.Class))  # make() now instantiates a node that is no Class
    graph.serialize(tmp_path / "broken.ttl", format="turtle")

    assert main(["stats", str(tmp_path / "broken.ttl")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("health: failing"):] == [
        "health: failing",
        "  dangling-call-arrows: 1 rows",
        "    s=shop/models.py::make, p=instantiates, o=shop/models.py::A",
    ]


def test_stats_on_a_missing_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["stats", str(tmp_path / "missing.ttl")])
    assert exit_.value.code == 2
    assert f"acte: error: cannot read {tmp_path / 'missing.ttl'}" in capsys.readouterr().err
