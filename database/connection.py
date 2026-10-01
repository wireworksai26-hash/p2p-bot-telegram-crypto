import logging
from sqlalchemy import create_engine, text, event
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

def _create_sqlite_engine(sqlite_url: str = "sqlite:///./p2p_bot.db"):
    """Membuat SQLite engine dengan WAL mode dan timeout 30s."""
    eng = create_engine(
        sqlite_url,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(eng, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=30000")
        except Exception:
            pass
        finally:
            cursor.close()

    return eng


def _build_database_engine():
    """
    Inisialisasi database engine dengan graceful self-healing fallback:
    Jika PostgreSQL gagal dihubungi (misal: DNS error postgres.railway.internal di Railway),
    sistem otomatis fallback ke SQLite lokal agar bot tetap hidup dan dapat melayani /start.
    """
    raw_db_url = str(settings.DATABASE_URL or "").strip().strip('"').strip("'")
    if not raw_db_url or not (raw_db_url.startswith("sqlite") or raw_db_url.startswith("postgres") or raw_db_url.startswith("mysql")):
        db_url = "sqlite:///./p2p_bot.db"
    else:
        db_url = raw_db_url

    # Handle Railway/Heroku legacy postgres:// schema
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)

    # Kunci driver psycopg2
    if db_url.startswith("postgresql://"):
        db_url = db_url.replace("postgresql://", "postgresql+psycopg2://", 1)

    if db_url.startswith("sqlite"):
        logger.info(f"Menggunakan database SQLite: {db_url}")
        return _create_sqlite_engine(db_url)

    # Coba koneksi ke PostgreSQL / external DB dengan pre-flight test
    try:
        eng = create_engine(
            db_url,
            pool_pre_ping=True,
            pool_recycle=300,
        )
        with eng.connect() as conn:
            conn.execute(text("SELECT 1"))
        logger.info("✅ Berhasil terhubung ke database eksternal (PostgreSQL).")
        return eng
    except Exception as exc:
        logger.error(
            f"⚠️ [DATABASE] Gagal terhubung ke database '{db_url}': {exc}\n"
            "🔄 Mengaktifkan self-healing graceful fallback ke SQLite lokal (p2p_bot.db)..."
        )
        return _create_sqlite_engine("sqlite:///./p2p_bot.db")


# Engine dan SessionLocal aktif
engine = _build_database_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)

