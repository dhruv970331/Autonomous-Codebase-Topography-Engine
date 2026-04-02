"""
Multi-language codebase parser using Tree-sitter.
Extracts structural nodes (classes, functions) and edges (calls,contains, inheritance).
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict, Set

import tree_sitter_language_pack as tslp

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models for extracted entities
# ---------------------------------------------------------------------------

@dataclass
class NodeInfo:
    kind: str  # 'File', 'Class', 'Function', 'Type'
    name: str
    file_path: str
    line_start: int
    line_end: int
    language: str = ""
    parent_name: Optional[str] = None
    params: Optional[str] = None
    return_type: Optional[str] = None
    is_test: bool = False
    extra: dict = field(default_factory=dict)

@dataclass
class EdgeInfo:
    kind: str  # 'CALLS', 'IMPORTS_FROM', 'INHERITS', 'CONTAINS'
    source: str
    target: str
    file_path: str
    line: int = 0
    extra: dict = field(default_factory=dict)
