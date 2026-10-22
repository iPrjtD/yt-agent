"""Print the best scored topics with their format (dev helper)."""

from sqlalchemy import select

from ytagent.db import session_scope
from ytagent.models import Topic

with session_scope() as s:
    for t in s.scalars(select(Topic).where(Topic.status == "scored").order_by(Topic.score.desc()).limit(15)):
        print(f"{t.id:4} {t.score:5.1f} {t.llm.get('content_format') or '?':10} {t.title[:80]}")
