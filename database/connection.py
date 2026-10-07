import logging
import os
import time
from sqlalchemy import create_engine, text, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import declarative_base, sessionmaker
from config.settings import settings

logger = logging.getLogger(__name__)

Base = declarative_base()

# True bila Postgres tidak terjangkau saat boot dan bot berjalan di SQLite sementara.
DB_DEGRADED = False
CONNECT_ATTEMPTS = max(1, int(os.getenv("DB_CONNECT_ATTEMPTS", "5")))
RETRY_DELAY_SECONDS = float(os.getenv("DB_CONNECT_RETRY_DELAY", "3"))


def _redact_url(db_url: str):
    """URL tanpa password untuk log, beserta password-nya (untuk disensor dari pesan error)."""
    try:
        url = make_url(db_url)
        return url.render_as_string(hide_password=True), url.password or ""
    except Exception:
        return "<DATABASE_URL>", ""


def _redact_text(value, password: str) -> str:
    text_value = str(value)
    return text_value.replace(password, "***") if password else text_value

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

    # Coba koneksi ke PostgreSQL / external DB dengan pre-flight test. DNS internal
    # Railway sering belum siap beberapa detik setelah container start — coba ulang dulu.
    global DB_DEGRADED
    safe_url, password = _redact_url(db_url)
    last_exc = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        try:
            eng = create_engine(
                db_url,
                pool_pre_ping=True,
                pool_recycle=300,
            )
            with eng.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("✅ Berhasil terhubung ke database eksternal (PostgreSQL).")
            DB_DEGRADED = False
            return eng
        except Exception as exc:
            last_exc = exc
            logger.warning("[DATABASE] Percobaan %d/%d ke %s gagal: %s",
                           attempt, CONNECT_ATTEMPTS, safe_url, _redact_text(exc, password))
            if attempt < CONNECT_ATTEMPTS and RETRY_DELAY_SECONDS:
                time.sleep(RETRY_DELAY_SECONDS)

    # Bot tetap hidup (keputusan 261001), tetapi masuk mode darurat: transaksi user
    # ditolak (bot/utils/maintenance_guard.py) agar tidak ada order/saldo yang hanya
    # tercatat di SQLite sementara lalu hilang saat restart.
    DB_DEGRADED = True
    logger.error(
        "⚠️ [DATABASE] Gagal terhubung ke %s setelah %d percobaan: %s\n"
        "🔄 Fallback ke SQLite lokal dalam MODE DARURAT — transaksi user dinonaktifkan.",
        safe_url, CONNECT_ATTEMPTS, _redact_text(last_exc, password),
    )
    return _create_sqlite_engine("sqlite:///./p2p_bot.db")


# Engine dan SessionLocal aktif
engine = _build_database_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False, bind=engine)

