# ACTE: Autonomous Codebase Topography Engine

ACTE is an AI agent architecture using MCP that translates complex Python repositories into deterministic RDF Knowledge Graphs. It bridges the gap between unstructured LLM reasoning and strict architectural realities.

Instead of relying on hallucination prone vector search (RAG) to understand codebase dependencies, ACTE parses Abstract Syntax Trees (AST) into a semantic ontology, exposes it via the Model Context Protocol (MCP), and forces the LLM to query architectural relationships using multi-hop SPARQL executions.