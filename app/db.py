"""
db.py – Database connection for BIMPruef

``init_db()`` creates all tables defined in ``app.models``.  It is called
once at application startup (e.g. in a FastAPI lifespan handler) and also
defensively at the top of auth.py and project_storage.py so that the
application remains functional when those modules are imported in isolation
(e.g. during tests or one-off scripts).

``DATABASE_URL`` must be set as an environment variable – no default value is
provided so that a missing configuration fails loudly rather than silently
using a local SQLite file.
"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. "
        "Set DATABASE_URL in the Render (or local) environment variables."
    )

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autocommit=False,
    autoflush=False,
    future=True,
)

Base = declarative_base()


def init_db() -> None:
    """Create all tables that are not yet present in the database."""
    import app.models  # noqa: F401 – registers ORM models against Base

    Base.metadata.create_all(bind=engine)
