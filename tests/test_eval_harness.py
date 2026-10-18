"""The eval runner's grader and tool conversion (eval/run_eval.py); no network, no model."""

import importlib.util
from pathlib import Path

import pytest
from mcp.types import Tool

_spec = importlib.util.spec_from_file_location("run_eval", Path(__file__).parents[1] / "eval" / "run_eval.py")
run_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(run_eval)

IDS = ["acte/cli.py::build", "acte/cli.py::main", "acte/rdf_builder.py::KnowledgeGraphBuilder.build",
       "acte/rdf_builder.py::build_graph", "acte/resolver.py::resolve", "acte/sparql_engine.py::SPARQLEngine",
       "acte/sparql_engine.py::SPARQLEngine._id"]
TAILS = run_eval._tails(IDS)


@pytest.mark.parametrize(("item", "text", "expected"), [
    ("acte/cli.py::build", "It is called by `acte/cli.py::build` at line 63.", True),
    ("acte/cli.py::build", "src/acte/cli.py::build", True),
    ("acte/cli.py::build", "acte/cli.py::build_graph", False),
    ("acte/cli.py::build", "build", False),  # two definitions are named build
    ("acte/rdf_builder.py::KnowledgeGraphBuilder.build", "KnowledgeGraphBuilder.build()", True),
    ("acte/rdf_builder.py::build_graph", "the build_graph function", True),
    ("acte/resolver.py::resolve", "resolve()", True),
    ("acte/resolver.py::resolve", "index.root.resolve", False),
    ("acte/resolver.py::resolve", "_resolve_call and acte/resolver.py", False),
    ("acte/sparql_engine.py::SPARQLEngine", "SPARQLEngine._id", False),
    ("acte/sparql_engine.py::SPARQLEngine", "creates a SPARQLEngine.", True),
    ("acte/sparql_engine.py::SPARQLEngine._id", "SPARQLEngine._id", True),
    ("acte/resolver.py", "in acte/resolver.py:25", True),
    ("acte/resolver.py", "in resolver.py", False),
])
def test_mentions(item, text, expected):
    assert run_eval.mentions(item, text, TAILS) is expected


def test_grade():
    q = {"expected": ["acte/rdf_builder.py::build_graph"], "forbidden": ["acte/cli.py::main"]}
    assert run_eval.grade(q, "build_graph", TAILS) == {"pass": True, "missing": [], "forbidden_found": []}
    assert run_eval.grade(q, "build_graph, then main", TAILS) == {
        "pass": False, "missing": [], "forbidden_found": ["acte/cli.py::main"]}
    assert run_eval.grade(q, "acte/cli.py::build", TAILS) == {
        "pass": False, "missing": ["acte/rdf_builder.py::build_graph"], "forbidden_found": []}


def test_strip_thinking():
    assert run_eval.strip_thinking("<think>maybe main?</think>\nbuild_graph") == "build_graph"
    assert run_eval.strip_thinking("<think>cut off") == ""
    assert run_eval.strip_thinking(None) == ""


def test_openai_tools():
    schema = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
    tool = Tool(name="callers_of", description="List who calls a function.", input_schema=schema)
    assert run_eval.openai_tools([tool]) == [{"type": "function", "function": {
        "name": "callers_of", "description": "List who calls a function.", "parameters": schema}}]
