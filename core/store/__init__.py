"""PostgreSQL-backed platform store.

The store is optional at import time: legacy file mode continues to work when a
runtime database is not configured. Alembic owns schema creation in deployed
mode; tests may call `models.Base.metadata.create_all()` against SQLite.
"""

from .db import database_url, engine_from_url, session_scope
from .models import Base

__all__ = ["Base", "database_url", "engine_from_url", "session_scope"]
