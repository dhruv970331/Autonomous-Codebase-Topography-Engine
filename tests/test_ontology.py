import subprocess
from importlib.resources import files
from pathlib import Path

from rdflib import Graph
from rdflib.namespace import RDF, RDFS

from acte.cli import main
from acte.rdf_builder import CODE, build_graph

SRC = Path(__file__).parent.parent / "src" / "acte"

# One file per CallSite shape: resolved, ambiguous with candidates, external with
# reason, recursive, instantiates (an __init__ found, and one behind an outside base), and a
# receiver; and a class inheriting a project class. kinds.py has the import kinds ACTE's own src lacks.
PROJECT = {
    "kinds.py": "if TYPE_CHECKING:\n    import app\n    import json\n\ndef load():\n    import csv\n\n"
                "if __name__ == '__main__':\n    import app\n    import pprint\n",
    "x/util.py": "def build():\n    pass\n",
    "y/util.py": "def build():\n    pass\n",
    "app.py": (
        "import os\n"
        "from x.util import *\n"
        "from y.util import *\n"
        "\n"
        "class Db:\n"
        "    pass\n"
        "\n"
        "class Pg(Db):\n"
        "    pass\n"
        "\n"
        "class Env(dict):\n"
        "    pass\n"
        "\n"
        "def run(n):\n"
        "    Db()\n"
        "    Env()\n"
        "    build()\n"
        "    os.getcwd()\n"
        "    run(n - 1)\n"
    ),
}


def _ontology():
    return Graph().parse(files("acte") / "ontology" / "code.ttl", format="turtle")


def _graph(tmp_path):
    for rel, code in PROJECT.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(code)
    for args in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "init"]):  # a commit, so the Build node has one
        subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", *args], check=True)
    graph = build_graph(sorted(SRC.glob("*.py")), SRC.parent.parent, use_jedi=False)
    # the real `acte build` (with jedi, so the Build node has jediVersion; PROJECT has no call jedi is asked about),
    # so code:summary is covered too
    main(["build", str(tmp_path), "-o", str(tmp_path / "g.ttl")])
    return graph + Graph().parse(tmp_path / "g.ttl", format="turtle")


def test_ontology_and_graph_agree(tmp_path):
    onto = _ontology()
    declared = set(onto.subjects(RDF.type, RDFS.Class)) | set(onto.subjects(RDF.type, RDF.Property))
    graph = _graph(tmp_path)
    used = set(graph.objects(None, RDF.type)) | {p for p in graph.predicates() if p.startswith(CODE)}
    assert used - declared == set(), "emitted but not declared in code.ttl"
    assert declared - used == set(), "declared in code.ttl but never emitted"
    assert all(onto.value(t, RDFS.comment) for t in declared)
