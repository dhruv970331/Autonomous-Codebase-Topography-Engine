"""
Small-model evaluation of the ACTE MCP server.

Starts `acte serve GRAPH --profile PROFILE` over stdio, offers its tools to a model behind an
OpenAI-style /v1/chat/completions endpoint, asks each question in questions.json, and grades
the final answers against the expected ids.

    poetry run acte build src -o acte.ttl
    poetry run python eval/run_eval.py --graph acte.ttl --base-url http://host:11500/v1 --model qwen3.6:35b-a3b

The expected sets are for ACTE's own `src/` at the commit the questions were written against.
OPENAI_API_KEY, when set, is sent as a bearer token.
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

import anyio
from mcp import Client, StdioServerParameters

from acte.sparql_engine import SPARQLEngine

MAX_TURNS = 8
TIMEOUT = 180  # seconds per request
SYSTEM = (
    "You answer questions about a Python codebase. Use the tools to look things up in a map of the "
    "code; do not guess. Names in questions may be partial or misspelled. In your final answer, name "
    "every function, class or file you mean by the exact id or path the tools return, such as "
    "pkg/module.py::Class.method or pkg/module.py."
)
_THINK = re.compile(r"<think>.*?(</think>|$)", re.DOTALL)  # an unclosed block runs to the end
_CALL_IN_TEXT = re.compile(r"<tool_call>|<function=|\"arguments\"\s*:|^\s*\{\s*\"name\"\s*:", re.MULTILINE)


def openai_tools(tools) -> list[dict]:
    """MCP tools as OpenAI function tools."""
    return [{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.input_schema}}
            for t in tools]


def strip_thinking(text: str | None) -> str:
    return _THINK.sub("", text or "").strip()


def _tails(ids) -> dict[str, set[str]]:
    """qualname and bare name -> the ids that end in it."""
    tails = defaultdict(set)
    for i in ids:
        qual = i.split("::", 1)[-1]
        tails[qual].add(i)
        tails[qual.rsplit(".", 1)[-1]].add(i)
    return tails


def mentions(item: str, text: str, tails: dict[str, set[str]]) -> bool:
    """Whether *text* names *item*: its full id or path, or its qualname or bare name when only it has that name."""
    forms = [item] + ([t for t in (item.split("::", 1)[-1], item.rsplit(".", 1)[-1]) if tails.get(t) == {item}]
                      if "::" in item else [])
    # not after a word char or dot ("index.root.resolve" is not resolve), nor before a word char or ".x"
    return any(re.search(rf"(?<![\w.]){re.escape(f)}(?!\w|\.\w)", text) for f in forms)


def grade(question: dict, answer: str, tails: dict[str, set[str]]) -> dict:
    missing = [e for e in question["expected"] if not mentions(e, answer, tails)]
    found = [f for f in question.get("forbidden", []) if mentions(f, answer, tails)]
    return {"pass": not missing and not found, "missing": missing, "forbidden_found": found}


def chat(base_url: str, body: dict) -> dict:
    """One /chat/completions request; one retry on a connection error."""
    headers = {"Content-Type": "application/json"}
    if key := os.environ.get("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", json.dumps(body).encode(), headers)

    def post() -> dict:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.load(r)
    try:
        return post()
    except urllib.error.HTTPError as exc:  # the server answered: retrying will not help
        raise RuntimeError(f"HTTP {exc.code}: {exc.read().decode(errors='replace')[:500]}") from exc
    except (urllib.error.URLError, ConnectionError):
        return post()


async def ask(client: Client, tools: list[dict], names: set[str], args, question: dict) -> dict:
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": question["question"]}]
    calls, errors, answer, turns, failure = [], [], "", 0, None
    start = time.monotonic()
    while turns < MAX_TURNS:
        turns += 1
        try:
            reply = await anyio.to_thread.run_sync(chat, args.base_url, {"model": args.model, "messages": messages, "tools": tools})
            msg = reply["choices"][0]["message"]
        except Exception as exc:  # noqa: BLE001 -- one question failing must not stop the run; it is recorded
            failure = f"{type(exc).__name__}: {exc}"
            break
        content = strip_thinking(msg.get("content"))
        if not msg.get("tool_calls"):
            answer = content
            if _CALL_IN_TEXT.search(content):
                errors.append("tool call written in the answer text")
            break
        messages.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": msg["tool_calls"]})
        for tc in msg["tool_calls"]:
            fn = tc.get("function", {})
            name, raw = fn.get("name"), fn.get("arguments") or "{}"
            record = {"name": name, "arguments": raw}
            try:
                arguments = raw if isinstance(raw, dict) else json.loads(raw)
                if not isinstance(arguments, dict):
                    raise TypeError("arguments are not a JSON object")
            except (ValueError, TypeError) as exc:
                record["error"] = f"bad arguments: {exc}"
            else:
                if name not in names:
                    record["error"] = f"unknown tool {name}"
                else:
                    result = await client.call_tool(name, arguments)
                    text = "\n".join(c.text for c in result.content if hasattr(c, "text"))
                    if result.is_error:
                        record["error"] = f"tool error: {text}"
                    record["result"] = text
            if "error" in record:
                errors.append(record["error"])
            calls.append(record)
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "content": record.get("result") or f"Error: {record['error']}"})
    return {"id": question["id"], "type": question["type"], "question": question["question"], "answer": answer,
            "turns": turns, "tool_calls": calls, "tool_errors": errors, "failure": failure,
            "latency_s": round(time.monotonic() - start, 1), "messages": messages[1:]}


def table(results: list[dict]) -> str:
    groups = defaultdict(list)
    for r in results:
        groups[r["type"]].append(r)
    groups["overall"] = results
    lines = [f"{'type':<11}{'n':>4}{'pass':>6}{'rate':>7}{'turns':>7}{'calls':>7}{'tool err':>10}{'failed':>8}{'s/q':>7}"]
    for kind, rs in groups.items():
        n = len(rs)
        passed = sum(r["pass"] for r in rs)
        lines.append(f"{kind:<11}{n:>4}{passed:>6}{passed / n:>7.0%}{sum(r['turns'] for r in rs) / n:>7.1f}"
                     f"{sum(len(r['tool_calls']) for r in rs):>7}{sum(len(r['tool_errors']) for r in rs):>10}"
                     f"{sum(bool(r['failure']) for r in rs):>8}{sum(r['latency_s'] for r in rs) / n:>7.1f}")
    return "\n".join(lines)


async def run(args) -> list[dict]:
    questions = json.loads(Path(args.questions).read_text())[: args.limit]
    ids = [s[0] for s in SPARQLEngine.from_turtle(args.graph).symbols()]
    tails = _tails(ids)
    stale = [e for q in questions for e in q["expected"] if "::" in e and e not in ids]
    if stale:
        print(f"warning: expected ids not in the graph (key out of date?): {', '.join(stale)}", file=sys.stderr)
    server = StdioServerParameters(command="acte", args=["serve", str(args.graph), "--profile", args.profile])
    results = []
    async with Client(server) as client:
        listed = (await client.list_tools()).tools
        tools, names = openai_tools(listed), {t.name for t in listed}
        for q in questions:
            r = await ask(client, tools, names, args, q)
            r |= grade(q, r["answer"], tails)
            results.append(r)
            print(f"{q['id']} {'PASS' if r['pass'] else 'FAIL'} turns={r['turns']} calls={len(r['tool_calls'])} "
                  f"errors={len(r['tool_errors'])} {r['latency_s']}s" + (f" {r['failure']}" if r["failure"] else ""), flush=True)
    return results


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--graph", required=True, help="Turtle graph from `acte build src`")
    p.add_argument("--base-url", required=True, help="OpenAI-style API root, such as http://host:port/v1")
    p.add_argument("--model", required=True)
    p.add_argument("--profile", default="small", choices=["small", "full"])
    p.add_argument("--limit", type=int, help="ask only the first N questions")
    p.add_argument("--questions", default=str(Path(__file__).with_name("questions.json")))
    p.add_argument("--out", help="write the results as JSON here")
    args = p.parse_args()
    results = anyio.run(run, args)
    print(table(results))
    if args.out:
        summary = {"model": args.model, "base_url": args.base_url, "profile": args.profile, "graph": str(args.graph),
                   "passed": sum(r["pass"] for r in results), "total": len(results)}
        Path(args.out).write_text(json.dumps({"summary": summary, "results": results}, indent=2) + "\n")


if __name__ == "__main__":
    main()
