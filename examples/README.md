# Client examples

Build the map first, in the project folder: `acte build .`, which writes `acte-graph.ttl`.
Both examples start the server with `acte serve acte-graph.ttl`, so `acte` must be on your
PATH and the client must start in the project folder.

- `opencode.json`: copy to the project folder. It points opencode at an OpenAI-compatible
  server (here Ollama on `localhost:11434`) and at ACTE. It only allows ACTE's tools, so the
  model cannot run commands, edit files or browse the web. Change `baseURL` and the model name to match your server.
- `mcp.json`: for Claude Code, copy to `.mcp.json` in the project folder.

Tested here: opencode 1.18.25 with Qwen 3.6 35B-A3B, and Claude Code 2.1.
