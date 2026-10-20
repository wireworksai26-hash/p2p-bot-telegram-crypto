"""
services/instance_lock.py — Pastikan HANYA SATU salinan bot yang aktif (polling + penjadwal).
===========================================================================================
Dua salinan bot dengan token yang sama (replika Railway, deployment lama yang belum berhenti, atau bot
yang dijalankan juga di komputer lain) membuat Telegram membagi update ke keduanya. Akibatnya:
  - "telegram.error.Conflict: terminated by other getUpdates request",
  - status percakapan (memori tiap salinan) terpisah -> tombol "Sesi tombol sudah berakhir",
  - tiap job terjadwal (cek deposit, payout, referral) berjalan dobel.

Kunci dipegang lewat advisory lock PostgreSQL pada satu koneksi yang dibiarkan terbuka selama bot hidup.
Salinan kedua menunggu sampai yang pertama berhenti (koneksi putus = kunci terlepas otomatis), jadi
deployment bergulir tetap mulus. SQLite/MySQL atau database yang tidak terjangkau: tidak ada kunci, bot
berjalan seperti biasa (tidak boleh membuat bot gagal start karena pengaman ini).
"""
import asyncio
import logging
import os
import socket

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Konstanta tetap (int64) — jangan diubah, semua salinan harus memakai angka yang sama.
LOCK_KEY = 7_210_202_610_001


def _who() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class SingleInstanceLock:
    def __init__(self, engine, key: int = LOCK_KEY):
        self.engine = engine
        self.key = int(key)
        self._conn = None
        self.enabled = getattr(getattr(engine, "dialect", None), "name", "") == "postgresql"

    # ---- inti -------------------------------------------------------------
    def try_acquire(self):
        """True = kunci dipegang (atau pengaman tidak berlaku); False = salinan lain memegangnya;
        None = database tidak bisa dihubungi (jangan memblokir bot)."""
        if not self.enabled:
            return True
        try:
            if self._conn is None:
                conn = self.engine.connect()
                try:
                    conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                except Exception:
                    pass
                self._conn = conn
            got = self._conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": self.key}).scalar()
            if got:
                return True
            self.release()
            return False
        except Exception as exc:
            logger.warning("Pengaman satu-salinan: database tidak terjangkau (%s); bot tetap berjalan.", exc)
            self.release()
            return None

    def release(self):
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()  # menutup sesi = kunci advisory otomatis terlepas
            except Exception:
                pass

    def still_held(self):
        """Pantau kunci: True aman; False = kunci hilang dan sudah diambil salinan lain; None = tak pasti."""
        if not self.enabled:
            return True
        try:
            if self._conn is not None:
                self._conn.execute(text("SELECT 1"))
                return True
        except Exception as exc:
            logger.warning("Koneksi kunci satu-salinan putus (%s); mencoba mengambil ulang.", exc)
            self.release()
        return self.try_acquire()

    # ---- pemakaian di main() ------------------------------------------------
    async def acquire_blocking(self, poll_seconds: float = 5.0, log_every: float = 60.0) -> None:
        """Tunggu sampai kunci didapat. Selesai juga bila database tak terjangkau (bot tetap hidup)."""
        waited = 0.0
        announced = False
        while True:
            result = await asyncio.to_thread(self.try_acquire)
            if result is not False:
                if announced:
                    logger.info("Salinan bot sebelumnya sudah berhenti — instance %s mengambil alih.", _who())
                else:
                    logger.info("Pengaman satu-salinan: instance %s memegang kunci.", _who())
                return
            if not announced or waited % log_every < poll_seconds:
                logger.warning(
                    "Salinan bot lain sedang aktif (kunci database dipegang). Instance %s menunggu supaya "
                    "tidak terjadi Conflict getUpdates / sesi tombol berakhir...", _who())
                announced = True
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds

    async def watch(self, on_lost, interval: float = 30.0) -> None:
        """Pantau terus; bila kunci hilang ke salinan lain, panggil on_lost() agar bot berhenti rapi."""
        while True:
            await asyncio.sleep(interval)
            if await asyncio.to_thread(self.still_held) is False:
                logger.error("Kunci satu-salinan diambil salinan lain — instance %s berhenti agar tidak dobel.", _who())
                on_lost()
                return
