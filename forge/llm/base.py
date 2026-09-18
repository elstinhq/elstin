"""The provider port. forge/agents/loop.py and every role module depend on
this Protocol and forge/llm/types.py ONLY — never on a concrete adapter or
a provider SDK. forge/llm/registry.py is the sole place that decides which
adapter to construct.

Adding a second provider later means writing one more module next to
forge/llm/openai_compat.py and teaching forge/llm/registry.py its config
key — nothing in forge/agents/ or forge/services/generation.py changes.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from forge.llm.types import LlmRequest, LlmResponse


@dataclass(frozen=True)
class Capabilities:
    """What a provider can do, so callers branch on capability rather than
    on a provider name string.

    Deliberately minimal: a flag lives here only once something actually
    reads it. Anything else a second adapter turns out to need (explicit
    cache-breakpoint placement, a per-provider effort-level whitelist,
    parallel-tool-call support) gets added *with* its consumer, not
    speculatively ahead of one — a flag nothing branches on is just a
    comment that looks like behavior."""

    # Native json_schema-constrained output (forge/llm/types.py's
    # ResponseFormat). False means the caller must fall back to a
    # prompt-enforced "return JSON" instruction + tolerant parsing —
    # see forge/services/generation.py, the one reader today.
    supports_structured_output: bool = True

    # Server-side history compaction/summarization. False (every wired
    # adapter today) means forge/agents/loop.py runs its own
    # summarize-and-splice compaction instead; True suppresses that, so a
    # future adapter whose provider compacts server-side doesn't get
    # both.
    supports_server_compaction: bool = False


class LlmProvider(Protocol):
    """One provider adapter. `name` is used as the usage/pricing key
    alongside the model id (forge/db/models/story_usage.py stores both) so
    two providers serving a same-named model never collide."""

    name: str
    capabilities: Capabilities

    async def complete(self, request: LlmRequest) -> LlmResponse: ...
