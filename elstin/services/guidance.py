"""A person's messages to an agent that is already working.

Until now the only controls on a running story were Cancel, and Retry afterwards:
a person who watched dev head the wrong way could stop it, not say "use the
existing helper in utils/dates.py". A message sent to the story
(POST /api/stories/{key}/guidance) waits in `stories.pending_guidance`; the loop
reads it at the start of the agent's next turn (elstin/agents/loop.py's
`pending_messages`) and it goes into the conversation as an ordinary user message —
kept in the history, so it is still there if the run is resumed.

It is advice, not authority. Dev can still write only the files its plan names, and
the message says so, so a request outside them is answered in words instead of an
attempt that the path guard would refuse. Whatever has not been read when the run
ends is cleared and reported (elstin/daemon/main.py), never carried into a later pass
where it would be stale.
"""
from __future__ import annotations

import logging

from elstin.services.run_activity import append_activity
from elstin.state.manager import StateManager

logger = logging.getLogger(__name__)

MAX_GUIDANCE_CHARS = 4000

_BOUNDS = {
    "dev": (
        "It does not widen what you may change: you can still write only the files in the approved plan, "
        "so if it asks for something outside them, say so in your reply instead of trying."
    ),
    "planner": "It does not change what you may do: you are only reading the code and writing the plan.",
}


def render_guidance(text: str, *, phase: str) -> str:
    """The user message the agent is shown for one of a person's messages."""
    return (
        "A person following this run sent you a message. Take it into account from here on. "
        f"{_BOUNDS.get(phase, _BOUNDS['dev'])}\n\n{text.strip()}"
    )


def take_guidance(run_id: str, *, phase: str) -> list[str]:
    """Every message waiting for `run_id`'s agent, rendered for the conversation
    and removed from the queue, with a line in the activity feed saying they were
    read. Empty when none are waiting. Never raises: a failure to read the queue
    must not cost the run a turn."""
    try:
        taken = StateManager().take_story_guidance(run_id)
        if not taken:
            return []
        count = len(taken)
        append_activity(
            run_id, type="status", phase=phase,
            title=f"The agent read {count} message{'s' if count != 1 else ''} from a person",
        )
        return [render_guidance(item["text"], phase=phase) for item in taken]
    except Exception:  # noqa: BLE001 - see docstring
        logger.warning("could not read the messages waiting for %s", run_id, exc_info=True)
        return []
