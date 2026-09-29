from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

# Supabase (and several other Postgres hosts) sometimes hand out connection
# strings starting with the old "postgres://" scheme. SQLAlchemy 2.x's
# default driver only recognizes "postgresql://" -- normalize here so
# whichever form gets pasted into DATABASE_URL just works, instead of
# failing at startup with a cryptic "can't load plugin" error.
_database_url = settings.database_url
if _database_url.startswith("postgres://"):
    _database_url = _database_url.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if _database_url.startswith("sqlite") else {}
engine = create_engine(_database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
