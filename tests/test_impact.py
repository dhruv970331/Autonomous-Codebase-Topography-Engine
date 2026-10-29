import json
import subprocess
from pathlib import Path

import pytest

from acte.cli import main
from acte.impact import ModuleChange, impact

# make() creates A; app.run calls make (through an import), app.main calls run
MODELS = "RATE = 2\n\n\nclass A:\n    def m(self):\n        return RATE\n\n\ndef make():\n    return A()\n"
PROJECT = {
    "shop/__init__.py": "",
    "shop/models.py": MODELS,
    "app.py": "from shop.models import make\n\n\ndef run():\n    x = make()\n    return x\n\n\ndef main():\n    return run()\n",
}


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


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    _write(root, PROJECT)
    _git(root, "init", "-q")
    _git(root, "checkout", "-q", "-b", "main")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def _impact(capsys, *args):
    code = main(["impact", *map(str, args)])
    out = capsys.readouterr()
    return code, out.out.splitlines(), out.err


def _edit(root, rel, old, new):
    text = (root / rel).read_text()
    assert old in text
    (root / rel).write_text(text.replace(old, new))


def test_no_changes(repo, capsys):
    assert _impact(capsys, repo) == (0, ["0 definitions changed in 0 files; 0 others affected (exact)"], "")


def test_modified_function(repo, capsys):
    _edit(repo, "shop/models.py", "return A()", "return A()  # edited")
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 2 others affected (exact)",
        "modified shop/models.py::make  shop/models.py:9: 2 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::run  app.py:5  (calls shop/models.py::make)",
        "  Distance 2:",
        "    app.py::main  app.py:10  (calls app.py::run)",
    ], "")


def test_deletion_inside_a_function(repo, capsys):
    _edit(repo, "app.py", "    x = make()\n    return x\n", "    return x\n")  # git: @@ -5 +4,0 @@, lines 4 and 5 are in run
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 1 other affected (exact)",
        "modified app.py::run  app.py:4: 1 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::main  app.py:9  (calls app.py::run)",
    ], "")


def test_deleting_a_whole_definition_is_a_module_level_change(repo, capsys):
    # git: @@ -4,5 +3,0 @@; line 3 is blank and line 4 is make's, so no definition holds both
    _edit(repo, "shop/models.py", "class A:\n    def m(self):\n        return RATE\n\n\n", "")
    assert _impact(capsys, repo) == (0, [
        "0 definitions changed in 0 files, 1 file changed at module level; 1 other affected (exact)",
        "module-level shop/models.py: imported by 1 project file",
        "    app.py",
    ], "")


def test_commenting_out_a_line_is_a_deletion(repo, capsys):
    _edit(repo, "app.py", "    x = make()\n", "    # x = make()\n")  # git: @@ -5 +5 @@, only a comment added
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 1 other affected (exact)",
        "modified app.py::run  app.py:4: 1 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::main  app.py:10  (calls app.py::run)",
    ], "")


def test_deleting_a_function_s_last_line(repo, capsys):
    _edit(repo, "app.py", "    return x\n", "")  # git: @@ -6 +5,0 @@; line 5 is run's, line 6 is blank
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 1 other affected (exact)",
        "modified app.py::run  app.py:4: 1 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::main  app.py:9  (calls app.py::run)",
    ], "")


def test_deleting_a_method_marks_its_class(repo, capsys):
    _write(repo, {"shop/k.py": "class K:\n    def a(self):\n        return 1\n\n    def b(self):\n        return 2\n\n"
                                "    def c(self):\n        return 3\n"})
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "k")
    _edit(repo, "shop/k.py", "    def b(self):\n        return 2\n\n", "")
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 0 others affected (exact)",
        "modified shop/k.py::K  shop/k.py:1: 0 affected, 0 possible (exact)",
    ], "")


def test_deleting_the_last_function_of_a_file(repo, capsys):
    # git: @@ -7,4 +6,0 @@; line 6 is A.m's, line 7 is past the end, so A.m is not marked
    _edit(repo, "shop/models.py", "\n\ndef make():\n    return A()\n", "")
    assert _impact(capsys, repo) == (0, [
        "0 definitions changed in 0 files, 1 file changed at module level; 1 other affected (exact)",
        "module-level shop/models.py: imported by 1 project file",
        "    app.py",
    ], "")


def test_deleting_only_blank_lines_is_ignored(repo, capsys):
    _edit(repo, "app.py", "    return x\n\n\n", "    return x\n\n")
    assert _impact(capsys, repo) == (0, ["0 definitions changed in 0 files; 0 others affected (exact)"], "")


def test_blank_and_comment_lines_touch_nothing(repo, capsys):
    _edit(repo, "app.py", "    x = make()\n", "    # make one\n\n    x = make()\n")
    _edit(repo, "shop/models.py", "RATE = 2\n", "RATE = 2\n# the rate\n\n")
    assert _impact(capsys, repo) == (0, ["0 definitions changed in 0 files; 0 others affected (exact)"], "")


def test_decorators_belong_to_their_definition(tmp_path, capsys):
    root = tmp_path / "repo"
    _write(root, {"deco.py": (
        "import functools\n\n\n"
        "@functools.cache\n"        # 4
        "def f():\n"
        "    return 1\n\n\n"
        "class K:\n"                # 9
        "    @property\n"
        "    def p(self):\n"
        "        return f()\n"
    )})
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _edit(root, "deco.py", "@functools.cache", "@functools.lru_cache")  # a top-level decorator
    _edit(root, "deco.py", "    @property\n", "    @staticmethod\n    @property\n")  # a new decorator stacked on a method
    assert _impact(capsys, root) == (0, [
        "2 definitions changed in 1 file; 0 others affected (exact)",  # K.p is affected but changed itself
        "modified deco.py::f  deco.py:5: 1 affected, 0 possible (exact)",
        "  Distance 1:",
        "    deco.py::K.p  deco.py:13  (calls deco.py::f)",
        "modified deco.py::K.p  deco.py:12: 0 affected, 0 possible (exact)",
    ], "")


def test_appended_function_alone_is_added(repo, capsys):
    (repo / "shop/models.py").write_text(MODELS + "\n\ndef helper():\n    return make()\n")  # the blank lines are not module-level
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file; 0 others affected (exact)",
        "added shop/models.py::helper  shop/models.py:13: 0 affected, 0 possible (exact)",
    ], "")


def test_no_jedi(repo, capsys):
    pytest.importorskip("jedi")
    _write(repo, {"use.py": "from shop.models import make\n\n\ndef use():\n    x = make()\n    x.m()\n"})  # x.m(): only jedi follows it
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "use")
    _edit(repo, "shop/models.py", "return RATE", "return RATE + 1")
    assert _impact(capsys, repo)[1][1:] == [
        "modified shop/models.py::A.m  shop/models.py:5: 1 affected, 0 possible (exact)",
        "  Distance 1:",
        "    use.py::use  use.py:6  (calls shop/models.py::A.m)",
    ]
    assert _impact(capsys, repo, "--no-jedi")[1][1:] == [
        "modified shop/models.py::A.m  shop/models.py:5: 0 affected, 0 possible (lower-bound: 1 call on objects of unknown type)",
    ]


def test_method_and_class_body(repo, capsys):
    # a line in a method is the method's; a class body line outside any method is the class's
    _edit(repo, "shop/models.py", "class A:\n    def m(self):\n        return RATE\n",
          "class A:\n    KIND = 1\n\n    def m(self):\n        return RATE + 1\n")
    assert _impact(capsys, repo) == (0, [
        "2 definitions changed in 1 file; 2 others affected (lower-bound)",
        "modified shop/models.py::A  shop/models.py:4: 2 affected, 0 possible (lower-bound: stopped at depth 2 while callers continue)",
        "  Distance 1:",
        "    shop/models.py::make  shop/models.py:12  (calls shop/models.py::A)",
        "  Distance 2:",
        "    app.py::run  app.py:5  (calls shop/models.py::make)",
        "modified shop/models.py::A.m  shop/models.py:7: 0 affected, 0 possible (exact)",
    ], "")


def test_added_function_and_module_level(repo, capsys):
    _edit(repo, "shop/models.py", "RATE = 2", "RATE = 3")
    (repo / "shop/models.py").write_text((repo / "shop/models.py").read_text() + "\n\ndef helper():\n    return make()\n")
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file, 1 file changed at module level; 1 other affected (exact)",
        "added shop/models.py::helper  shop/models.py:13: 0 affected, 0 possible (exact)",
        "module-level shop/models.py: imported by 1 project file",
        "    app.py",
    ], "")


def test_importers_are_labelled_with_their_kind(repo, capsys):
    _write(repo, {
        "both.py": "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import shop.models\n\n\ndef go():\n    import shop.models\n",
        "script.py": "if __name__ == '__main__':\n    import shop.models\n",
        "mixed.py": "import shop.models\n\n\ndef go():\n    import shop.models\n",  # module level too: no label
    })
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "importers")
    _edit(repo, "shop/models.py", "RATE = 2", "RATE = 3")
    assert _impact(capsys, repo) == (0, [
        "0 definitions changed in 0 files, 1 file changed at module level; 4 others affected (exact)",
        "module-level shop/models.py: imported by 4 project files",
        "    app.py",
        "    both.py (type only, in function)",
        "    mixed.py",
        "    script.py (main block)",
    ], "")
    code, out, err = _impact(capsys, repo, "--format", "json", "--limit", "2")
    assert (code, err) == (0, "")
    assert json.loads("\n".join(out))["modules"] == [{
        "file_path": "shop/models.py", "importers": ["app.py", "both.py"], "total": 4,
        "importer_kinds": {"both.py": ["type-only", "in-function"]},  # only the listed importers
    }]


def test_untracked_file_is_fully_added(repo, capsys):
    _write(repo, {"extra.py": "from app import main\n\n\ndef go():\n    return main()\n"})
    assert _impact(capsys, repo) == (0, [
        "1 definition changed in 1 file, 1 file changed at module level; 0 others affected (exact)",
        "added extra.py::go  extra.py:4: 0 affected, 0 possible (exact)",
        "module-level extra.py: imported by 0 project files",
    ], "")


def test_base_a_branch(repo, capsys):
    _git(repo, "checkout", "-q", "-b", "feature")
    _edit(repo, "app.py", "return run()", "return run() or 1")
    _git(repo, "commit", "-qam", "edit main")

    assert _impact(capsys, repo)[1] == ["0 definitions changed in 0 files; 0 others affected (exact)"]  # nothing since HEAD
    assert _impact(capsys, repo, "--base", "main") == (0, [
        "1 definition changed in 1 file; 0 others affected (exact)",
        "modified app.py::main  app.py:9: 0 affected, 0 possible (exact)",
    ], "")


def test_subfolder_path_uses_ids_relative_to_it(tmp_path, capsys):
    root = tmp_path / "repo"
    _write(root, {f"src/{k}": v for k, v in PROJECT.items()} | {"tools/t.py": "def t():\n    pass\n"})
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    _edit(root, "src/shop/models.py", "return A()", "return A()  # edited")
    _edit(root, "tools/t.py", "pass", "return 1")  # outside PATH: not part of this graph

    assert _impact(capsys, root / "src") == (0, [
        "1 definition changed in 1 file; 2 others affected (exact)",
        "modified shop/models.py::make  shop/models.py:9: 2 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::run  app.py:5  (calls shop/models.py::make)",
        "  Distance 2:",
        "    app.py::main  app.py:10  (calls app.py::run)",
    ], "")


def test_limit_cuts_and_states_the_total(repo, capsys):
    _edit(repo, "shop/models.py", "return A()", "return A()  # edited")
    assert _impact(capsys, repo, "--limit", "1") == (0, [
        "1 definition changed in 1 file; 2 others affected (exact)",  # the summary counts the uncut lists
        "modified shop/models.py::make  shop/models.py:9: 2 affected, 0 possible (exact)",
        "  Distance 1:",
        "    app.py::run  app.py:5  (calls shop/models.py::make)",
        "  Showing 1 of 2.",
    ], "")


def test_json_shape(repo, capsys):
    _edit(repo, "shop/models.py", "RATE = 2", "RATE = 3")
    _edit(repo, "shop/models.py", "return A()", "return A()  # edited")
    code, out, err = _impact(capsys, repo, "--format", "json", "--limit", "1", "--depth", "1")
    assert (code, err) == (0, "")
    assert json.loads("\n".join(out)) == {
        "base": "HEAD",
        "changed": [{
            "id": "shop/models.py::make", "kind": "Function", "file_path": "shop/models.py", "line": 9, "change": "modified",
            "blast": {
                "target": "shop/models.py::make",
                "affected": [{"id": "app.py::run", "name": "run", "file_path": "app.py", "distance": 1,
                              "via": "shop/models.py::make", "line": 5}],
                "possible": [], "total": 1, "truncated": False, "depth": 1,
                "completeness": "lower-bound", "reasons": {"depth-limit": 1},
            },
        }],
        "modules": [{"file_path": "shop/models.py", "importers": ["app.py"], "total": 1, "importer_kinds": {}}],
        "affected": 2,
        "completeness": "lower-bound",
    }


def test_graph_file_with_stale_commit(repo, tmp_path, capsys):
    assert main(["build", str(repo), "-o", str(tmp_path / "g.ttl"), "--no-jedi"]) == 0
    old = _git(repo, "rev-parse", "HEAD")
    _edit(repo, "app.py", "return run()", "return run() or 1")
    _git(repo, "commit", "-qam", "edit main")
    new = _git(repo, "rev-parse", "HEAD")
    capsys.readouterr()

    code, out, err = _impact(capsys, repo, "--graph", tmp_path / "g.ttl", "--base", "HEAD~1")
    assert (code, out[0]) == (0, "1 definition changed in 1 file; 0 others affected (exact)")
    assert err == f"Warning: this map was built at commit {old[:7]} but the project is now at {new[:7]}; answers may be out of date.\n"


def test_not_a_git_repo(tmp_path, capsys):
    _write(tmp_path / "plain", PROJECT)
    assert _impact(capsys, tmp_path / "plain") == (2, [], f"acte impact: {tmp_path / 'plain'} is not in a git work tree\n")


def test_bad_base(repo, capsys):
    code, out, err = _impact(capsys, repo, "--base", "no-such-branch")
    assert (code, out) == (2, [])
    assert err.startswith("acte impact: git failed: fatal:")


def test_unknown_format(repo, capsys):
    with pytest.raises(SystemExit) as exit_:
        main(["impact", str(repo), "--format", "xml"])
    assert exit_.value.code == 2


def test_each_definition_of_a_twice_defined_function_has_its_own_span(twice_defined_graphs):
    """alpha is defined at lines 9-10 and 15-16; Box.size at 21-22 and, decorated at 24, 25-26."""
    root = Path(__file__).parent / "data" / "twice_defined"
    app = f"{root}/app.py"

    def changed(graph, *lines):
        report = impact(root, graph, {app: set(lines)}, {}, [], "HEAD", depth=1)
        return [(c.id.split("::")[1], c.line) for c in report.changed], report.modules

    for graph in twice_defined_graphs:
        assert changed(graph, 14) == ([], [ModuleChange(app, [], 0, {})])  # the else: between them is in neither
        assert changed(graph, 16) == ([("alpha", 15)], [])
        assert changed(graph, 24) == ([("Box.size", 25)], [])  # the decorator belongs to the second size only
        assert changed(graph, 22) == ([("Box.size", 21)], [])
        assert changed(graph, 10, 16) == ([("alpha", 9)], [])  # both changed: one entry, at the first definition
