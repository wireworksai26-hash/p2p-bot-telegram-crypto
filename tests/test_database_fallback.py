import os
import sys
import unittest

sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"d:\Project_Test\p2p-crypto-telegram-bot")

class TestDatabaseFallback(unittest.TestCase):
    def test_postgres_dns_error_graceful_fallback(self):
        """Tes jika PostgreSQL DNS error (misal postgres.railway.internal), sistem otomatis fallback ke SQLite."""
        from config.settings import settings
        # Simulasikan setting DATABASE_URL ke host yang tidak ada
        settings.DATABASE_URL = "postgresql://postgres:secret@postgres.railway.internal:5432/railway"
        
        from database.connection import _build_database_engine
        engine = _build_database_engine()
        
        # Engine harus fallback ke sqlite
        self.assertEqual(engine.url.drivername, "sqlite")
        print("✅ Engine driver name:", engine.url.drivername)
        
        # Inisialisasi database
        from main import init_database
        init_database()
        print("✅ init_database() executed without crashing!")
        
        # Test query database
        from database.connection import SessionLocal
        from database.models import User
        db = SessionLocal()
        try:
            user_count = db.query(User).count()
            self.assertIsInstance(user_count, int)
            print(f"✅ User count query on SQLite fallback: {user_count}")
        finally:
            db.close()

if __name__ == "__main__":
    unittest.main()
