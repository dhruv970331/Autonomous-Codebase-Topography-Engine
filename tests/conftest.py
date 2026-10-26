import os

import pytest


@pytest.fixture(autouse=True)
def _no_git_env(monkeypatch):
    # tests that run git must never reach the real repository, for example under `git rebase --exec`
    for key in [k for k in os.environ if k.startswith("GIT_")]:
        monkeypatch.delenv(key)
