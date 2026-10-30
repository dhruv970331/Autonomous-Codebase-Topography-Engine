import json
import os
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path

import pytest

from acte import cli
from acte.cli import main
from acte.rdf_builder import CODE, RDF, has, load, remove, save, subjects, text, value

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
    return load(out), capsys.readouterr().out.splitlines()


def _files(graph):
    return sorted(text(value(graph, f, CODE.filePath)) for f in subjects(graph, None, CODE.File))


def _call(graph, caller, name):
    """(status, resolvedTo id or None, tier or None) of the call to *name* in *caller*."""
    site = next(s for s in subjects(graph, CODE.inFunction, CODE[caller]) if text(value(graph, s, CODE.targetName)) == name)
    target = value(graph, site, CODE.resolvedTo)
    return (text(value(graph, site, CODE.status)), target and text(target)[len(CODE):],
            value(graph, site, CODE.tier) and text(value(graph, site, CODE.tier)))


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
    assert has(graph, meta, None, CODE.Build)
    assert text(value(graph, meta, CODE.root)) == "repo"  # the name only: no machine path in the graph
    assert text(value(graph, meta, CODE.commit)) == _git(repo, "rev-parse", "HEAD")
    assert value(graph, meta, CODE.acteVersion) is not None
    assert value(graph, meta, CODE.jediVersion) is not None
    assert out == [f"wrote {tmp_path / 'g.ttl'}", *SUMMARY, "coverage: 100.0%", "health: ok"]


def test_plain_dir_without_jedi(tmp_path, capsys):
    root = tmp_path / "proj"
    _write(root, PROJECT | {".venv/lib/dep.py": "def d():\n    pass\n", "node_modules/x/y.js": "function f() {}\n",
                            ".hidden/h.py": "def h():\n    pass\n", "build/b.py": "def b():\n    pass\n"})

    graph, out = _build(capsys, root, tmp_path / "g.ttl", "--no-jedi")

    assert _files(graph) == ["app.py", "shop/__init__.py", "shop/models.py"]  # skipped and hidden dirs absent
    assert _call(graph, "app.py::run", "m") == ("unresolved", None, None)
    meta = CODE["build"]
    assert text(value(graph, meta, CODE.root)) == "proj"
    assert value(graph, meta, CODE.commit) is None  # not a git work tree
    assert value(graph, meta, CODE.jediVersion) is None  # jedi did not run
    assert out[1:] == [*SUMMARY, "coverage: 66.7%", "health: ok"]


def test_calls_in_other_languages_are_counted_apart(tmp_path, capsys):
    root = tmp_path / "proj"
    _write(root, PROJECT | {"svc/main.go": "package main\n\nfunc main() {\n\tfmt.Println(run())\n}\n",
                            "web/app.tsx": "export const App = () => <div>{load()}</div>;\n"})

    _, out = _build(capsys, root, tmp_path / "g.ttl", "--no-jedi")

    # the Python numbers are those of PROJECT alone; the Go and TSX calls are counted apart
    assert out[1:] == ["files parsed: 5", "functions: 5", "classes: 1", "call sites: 6",
                       "call sites in languages without call resolution: 3", "coverage: 66.7%", "health: ok"]
    # and `stats` gives the breakdown of the same Python project built alone
    _write(tmp_path / "py", PROJECT)
    _build(capsys, tmp_path / "py", tmp_path / "py.ttl", "--no-jedi")
    stats = {}
    for name in ("g", "py"):
        assert main(["stats", str(tmp_path / f"{name}.ttl")]) == 0
        lines = capsys.readouterr().out.splitlines()
        stats[name] = lines[lines.index("coverage: 66.7%"):]
    assert stats["g"] == stats["py"]
    assert stats["g"][1].startswith("status: resolved")


def test_failing_health_check_exits_1(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "proj", PROJECT)
    monkeypatch.setattr("acte.cli.run_checks", lambda graph: {"self-call-arrows": [{"s": "x"}], "callsite-status": []})
    assert main(["build", str(tmp_path / "proj"), "-o", str(tmp_path / "g.ttl"), "--no-jedi"]) == 1
    assert capsys.readouterr().out.splitlines()[-1] == "health: self-call-arrows"
    assert (tmp_path / "g.ttl").exists()  # the graph is still written


def test_failed_write_keeps_the_old_output(tmp_path, monkeypatch):
    _write(tmp_path / "proj", PROJECT)
    out = tmp_path / "out" / "g.ttl"
    out.parent.mkdir()
    out.write_text("old map\n")

    def broken(graph, path):  # writes part of the map, then fails as a crash would
        Path(path).write_text("@prefix half")
        raise RuntimeError("disk full")

    monkeypatch.setattr(cli, "save", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        main(["build", str(tmp_path / "proj"), "-o", str(out), "--no-jedi"])
    assert out.read_text() == "old map\n"
    assert os.listdir(out.parent) == ["g.ttl"]  # no temporary file left behind


def test_parallel_parse_writes_the_same_map(tmp_path, monkeypatch, capsys):
    from acte import rdf_builder
    monkeypatch.setattr(rdf_builder, "MIN_PARALLEL_FILES", 2)  # this repository's src/ has fewer than 50 files
    src = Path(__file__).resolve().parents[1] / "src"
    main(["build", str(src), "-o", str(tmp_path / "one.ttl"), "--no-jedi", "--jobs", "1"])
    main(["build", str(src), "-o", str(tmp_path / "four.ttl"), "--no-jedi", "--jobs", "4"])
    assert (tmp_path / "one.ttl").read_bytes() == (tmp_path / "four.ttl").read_bytes()


def test_default_output_is_in_the_working_directory(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "proj", PROJECT)
    monkeypatch.chdir(tmp_path)
    assert main(["build", "proj", "--no-jedi"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "wrote acte-graph.ttl"
    assert text(value(load(tmp_path / "acte-graph.ttl"), CODE["build"], CODE.root)) == "proj"


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
    remove(graph, CODE["shop/models.py::A"], RDF.type, CODE.Class)  # make() now instantiates a node that is no Class
    save(graph, tmp_path / "broken.ttl")

    assert main(["stats", str(tmp_path / "broken.ttl")]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[out.index("health: failing"):] == [
        "health: failing",
        "  dangling-call-arrows: 1 rows",
        "    s=shop/models.py::make, p=instantiates, o=shop/models.py::A",
    ]


def test_stats_as_json(tmp_path, capsys):
    _write(tmp_path / "proj", PROJECT)
    graph, _ = _build(capsys, tmp_path / "proj", tmp_path / "g.ttl", "--no-jedi")

    assert main(["stats", str(tmp_path / "g.ttl"), "--format", "json"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats == {
        "build": {"root": "proj", "acteVersion": version("acte")},
        "files": 3, "functions": 3, "classes": 1, "call_sites": 3,
        "coverage": {"edge": {"calls": 1, "instantiates": 1}, "no_resolver": 0, "rate": 2 / 3, "reason": {"unknown-receiver": 1},
                     "status": {"resolved": 2, "unresolved": 1}, "tier": {"T2": 1, "T3": 1}, "total": 3},
        "health": "ok", "failing": {},
    }

    remove(graph, CODE["shop/models.py::A"], RDF.type, CODE.Class)
    save(graph, tmp_path / "broken.ttl")
    assert main(["stats", str(tmp_path / "broken.ttl"), "--format", "json"]) == 1
    stats = json.loads(capsys.readouterr().out)
    assert (stats["health"], stats["failing"]) == ("failing", {
        "dangling-call-arrows": [{"s": "shop/models.py::make", "p": "instantiates", "o": "shop/models.py::A"}]})


def test_stats_unknown_format(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["stats", str(tmp_path / "g.ttl"), "--format", "xml"])
    assert exit_.value.code == 2
    assert "invalid choice: 'xml'" in capsys.readouterr().err


def test_stats_on_a_missing_file(tmp_path, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["stats", str(tmp_path / "missing.ttl")])
    assert exit_.value.code == 2
    assert f"acte: error: cannot read {tmp_path / 'missing.ttl'}" in capsys.readouterr().err


# base() has three callers; core.py and util.py import each other; lone.py links to nothing.
# Link weights: core.py 6 (app.py 3, util.py 3), app.py 5, util.py 5, so core.py is the hub.
ANALYZE = {
    "pkg/core.py": "import pkg.util\n\n\ndef base():\n    pass\n",
    "pkg/util.py": "from pkg.core import base\n\n\ndef helper():\n    base()\n    base()\n",
    "app.py": "from pkg.core import base\nfrom pkg.util import helper\n\n\ndef main():\n    base()\n    helper()\n\n\nbase()\n",
    "lone.py": "def alone():\n    pass\n",
}


def _analyze(capsys, tmp_path, *flags):
    _write(tmp_path / "proj", ANALYZE)
    _build(capsys, tmp_path / "proj", tmp_path / "g.ttl", "--no-jedi")
    assert main(["analyze", str(tmp_path / "g.ttl"), *flags]) == 0
    return capsys.readouterr().out


def test_analyze_text(tmp_path, capsys):
    out = _analyze(capsys, tmp_path, "--top", "2").splitlines()
    assert out[0] == "most depended on: top 2 of 4 functions and classes, by PageRank"
    assert out[1].startswith("  1. pkg/core.py:4 base (score ") and out[1].endswith(", 3 direct callers)")  # app.py, main, helper
    assert out[2].startswith("  2. pkg/util.py:4 helper (score ") and out[2].endswith(", 1 direct callers)")
    assert out[3:5] == ["import cycles: 1", "  2 files: pkg/core.py, pkg/util.py"]
    assert out[5:] == ["subsystems: 1", "  pkg/core.py: 3 files in .: pkg/core.py, app.py, pkg/util.py",
                       "1 file with no links to other files"]


def test_analyze_json(tmp_path, capsys):
    data = json.loads(_analyze(capsys, tmp_path, "--format", "json"))
    assert list(data) == ["rank", "import_cycles", "subsystems"]
    assert data["rank"]["total"] == 4 and data["rank"]["converged"] is True
    assert list(data["rank"]["top"][0]) == ["id", "file_path", "line", "score", "callers"]
    assert data["rank"]["top"][0]["id"] == "pkg/core.py::base" and len(data["rank"]["top"]) == 4
    assert data["import_cycles"] == [{"files": ["pkg/core.py", "pkg/util.py"]}]
    assert data["subsystems"][-1] == {"hub": "lone.py", "folder": ".", "size": 1, "files": ["lone.py"]}  # listed, unlike text
    assert all(list(s) == ["hub", "folder", "size", "files"] for s in data["subsystems"])


def test_analyze_is_the_same_on_every_run(tmp_path, capsys):
    _analyze(capsys, tmp_path)
    run = "import sys; from acte.cli import main; sys.exit(main(sys.argv[1:]))"
    for fmt in ("text", "json"):
        # separate processes with different hash seeds, so set and dict order cannot leak into the output
        outs = [subprocess.run([sys.executable, "-c", run, "analyze", str(tmp_path / "g.ttl"), "--format", fmt],
                               check=True, capture_output=True, text=True, env=os.environ | {"PYTHONHASHSEED": seed}).stdout
                for seed in ("1", "2")]
        assert outs[0] == outs[1] and outs[0]


@pytest.mark.parametrize("flags, error", [(["--format", "dot"], "invalid choice: 'dot'"), (["--top", "0"], "--top must be at least 1")])
def test_analyze_bad_arguments(tmp_path, capsys, flags, error):
    with pytest.raises(SystemExit) as exit_:
        main(["analyze", str(tmp_path / "g.ttl"), *flags])
    assert exit_.value.code == 2
    assert error in capsys.readouterr().err
