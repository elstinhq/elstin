"""Comment watermarks are comment IDs, not timestamps — enables exactly-once
processing of JIRA comments the daemon reacts to."""
from __future__ import annotations

from forge.db.engine import session_scope
from forge.db.models import JiraWatermark


class _WatermarkMixin:
    def get_comment_watermark(self, jira_key: str) -> str | None:
        with session_scope() as session:
            row = session.get(JiraWatermark, jira_key)
            return row.last_comment_id if row is not None else None

    def set_comment_watermark(self, jira_key: str, comment_id: str) -> None:
        with session_scope() as session:
            row = session.get(JiraWatermark, jira_key)
            if row is None:
                session.add(JiraWatermark(jira_key=jira_key, last_comment_id=comment_id))
            else:
                row.last_comment_id = comment_id
