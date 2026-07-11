# ACTE: Autonomous Codebase Topography Engine
## Project Vision and Architectural Blueprint

### 1. Core Objective
ACTE is a deterministic evaluation pipeline designed for **AI-Based Quality Evaluation of AI-Generated Code Changes**. Its primary function is to eradicate probabilistic hallucination from LLM-driven code reviews. 

Current LLM tooling relies on Vector Databases and Retrieval-Augmented Generation (RAG). Vector spaces are mathematically incapable of executing multi-hop, deterministic relational logic. They guess. ACTE does not guess. It parses code into strict Abstract Syntax Trees (AST), constructs a W3C-standard Resource Description Framework (RDF) Knowledge Graph, and queries relationships using SPARQL. 

The output is absolute structural truth. The LLM is restricted to synthesizing human-readable reports from pre-validated mathematical proofs.

### 2. Architectural Topography
The system operates sequentially across four isolated layers:

*   **Layer 1: Extraction (AST Parsing)**
    *   **Mechanism:** `tree-sitter` dynamically parses polyglot codebases (Python, TypeScript, Go, Java) via `tree-sitter-language-pack`.
    *   **Output:** Extracts formal `NodeInfo` (Files, Classes, Functions) and `EdgeInfo` (CALLS, CONTAINS, IMPORTS_FROM, INHERITS) using fallback-tolerant API reflection heuristics to survive version drift.
*   **Layer 2: Structuring (RDF Builder)**
    *   **Mechanism:** `rdflib` consumes raw AST dataclasses and binds them to a custom Semantic Web namespace (`http://acte.local/code#`).
    *   **Output:** Serializes the codebase into an interoperable `.ttl` (Turtle) file. Identifiers are strictly qualified (e.g., `file.py::Class.method`) to guarantee unbroken graph traversal.
*   **Layer 3: Retrieval (SPARQL Engine)**
    *   **Mechanism:** A purely deterministic execution engine executing hardcoded graph queries.
    *   **Output:** Calculates exact dependency chains, dead code (orphans), and multi-hop downstream impact (Blast Radius) via property paths (`code:calls+`). 
*   **Layer 4: Orchestration (FastMCP Server)**
    *   **Mechanism:** Wraps the SPARQL engine in the Model Context Protocol (MCP).
    *   **Output:** Exposes parametrized, hallucination-proof tools (`get_blast_radius`, `get_callers`) to an LLM evaluator (e.g., Claude, Llama-3).

### 3. Feature Specifications

#### 3.1. Deterministic Blast Radius Calculation
When an AI generates a code change, ACTE calculates the exact downstream impact. By executing a transitive SPARQL query (`code:calls+`), the engine maps every function, class, and file that relies on the modified node, up to *N* hops, preventing isolated localized testing of interconnected systems.

#### 3.2. Polyglot Evaluation
The architecture is inherently language-agnostic. The `tree-sitter` abstraction standardizes disparate language syntaxes into a unified ontology. A Python function calling a C-extension or an API endpoint can be mathematically linked in the same `.ttl` artifact.

#### 3.3. Autonomous AI Quality Gates
ACTE functions as a continuous integration (CI) quality gate for AI-generated code.
1.  AI proposes a PR.
2.  ACTE generates the RDF graph of the PR delta against `main`.
3.  ACTE executes SPARQL impact queries.
4.  The MCP-connected LLM Judge reviews the localized logic *combined* with the absolute blast radius metrics to approve, reject, or request structural modifications.

#### 3.4. Dead-Code Detection (Orphan Analysis)
Identifies functions or classes with zero incoming `CALLS` or `IMPORTS_FROM` edges. Forces AI code generators to clean up deprecated pathways when refactoring.

### 4. Operational Superiority (The "Why")
*   **Zero Infrastructure:** No vector database provisioning, no latent space tuning, no Docker clusters. The entire graph runs in-memory and serializes to a flat text file.
*   **Provable Accuracy:** A 3-hop dependency chain in an RDF graph is a mathematical certainty. The same chain in a Vector DB is a statistical probability that degrades with context window noise.
*   **Interoperability:** RDF is a global standard. The generated `.ttl` files can be ingested by any enterprise graph database (Neptune, Stardog) if scaling is required.