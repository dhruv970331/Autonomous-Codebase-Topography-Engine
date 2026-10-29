import os
from pathlib import Path

import pytest

from acte.rdf_builder import add, build_graph, new_graph, triples


@pytest.fixture(autouse=True)
def _no_git_env(monkeypatch):
    # tests that run git must never reach the real repository, for example under `git rebase --exec`
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)


TWICE_DEFINED = Path(__file__).parent / "data" / "twice_defined"


@pytest.fixture
def twice_defined_graphs():
    """The real map of tests/data/twice_defined/app.py, and a copy filled in reverse fact order, so a reader that
    depends on the store's order gives two answers."""
    graph = build_graph([TWICE_DEFINED / "app.py"], TWICE_DEFINED, use_jedi=False)
    copy = new_graph()
    for triple in reversed(list(triples(graph))):
        add(copy, *triple)
    return graph, copy
