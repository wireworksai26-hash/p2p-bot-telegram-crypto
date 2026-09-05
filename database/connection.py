from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import settings

# Determine DB URL
raw_db_url = str(settings.DATABASE_URL or "").strip().strip('"').strip("'")
if not raw_db_url or not (raw_db_url.startswith("sqlite") or raw_db_url.startswith("postgres") or raw_db_url.startswith("mysql")):
    db_url = "sqlite:///./p2p_bot.db"
else:
    db_url = raw_db_url

# Handle Railway/Heroku legacy postgres:// schema
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)

if db_url.startswith("sqlite"):
    # SQLite requires check_same_thread=False for multi-thread access.
    # WAL + busy_timeout: banyak handler nulis bersamaan tidak boleh
    # memicu "database is locked" (bottleneck utama saat traffic tinggi).
    engine = create_engine(
        db_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()
else:
    engine = create_engine(
        db_url,
        pool_pre_ping=True,
        pool_recycle=300,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()
