"""Pemroses update Telegram: paralel antar user, berurutan untuk user yang sama.

Bawaan python-telegram-bot memproses SATU update pada satu waktu untuk seluruh bot: satu handler yang
menunggu lama (cek RPC/gateway, kirim koin, retry jaringan) membuat /start semua user lain antre di
belakangnya, terasa seperti bot "delay beberapa menit". Di sini user berbeda diproses bersamaan, tetapi
update dari user yang sama tetap berurutan (klik ganda tidak berlomba, status percakapan tetap konsisten).
"""
import asyncio
from typing import Any, Awaitable

from telegram.ext import BaseUpdateProcessor


class PerUserUpdateProcessor(BaseUpdateProcessor):
    def __init__(self, max_concurrent_updates: int = 64):
        super().__init__(max_concurrent_updates)
        self._locks: dict = {}  # user_id -> [asyncio.Lock, jumlah pemakai]

    @staticmethod
    def _user_key(update: object):
        user = getattr(update, "effective_user", None)
        return getattr(user, "id", None)

    async def initialize(self) -> None:
        return None

    async def shutdown(self) -> None:
        self._locks.clear()

    async def do_process_update(self, update: object, coroutine: "Awaitable[Any]") -> None:
        key = self._user_key(update)
        if key is None:  # update tanpa user (channel_post dsb.): langsung jalan
            await coroutine
            return
        entry = self._locks.setdefault(key, [asyncio.Lock(), 0])
        entry[1] += 1
        try:
            async with entry[0]:
                await coroutine
        finally:
            entry[1] -= 1
            if entry[1] == 0:
                self._locks.pop(key, None)  # tidak ada yang antre: buang agar memori tidak tumbuh
