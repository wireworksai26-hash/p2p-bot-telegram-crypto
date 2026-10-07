"""Nilai settings minimum untuk tes E2E, tidak bergantung urutan impor modul tes.

`config.settings` dibaca sekali per proses: bila modul tes lain mengimpornya lebih dulu
tanpa ADMIN_CHAT_IDS/EVM_WALLET_ADDRESS, `os.environ.setdefault` di modul berikutnya
tidak berpengaruh. Pakai `pinned_settings()` di setUp lalu hentikan di tearDown.
"""
from unittest.mock import patch

from config.settings import settings

ADMIN_ID = 1
HOT_WALLET = "0x" + "1" * 40


def pinned_settings():
    patches = [patch.object(settings, "ADMIN_CHAT_IDS", [ADMIN_ID])]
    if not settings.EVM_WALLET_ADDRESS:
        patches.append(patch.object(settings, "EVM_WALLET_ADDRESS", HOT_WALLET))
    for p in patches:
        p.start()
    return patches


def unpin(patches):
    for p in reversed(patches):
        p.stop()


async def e2e_setup(test):
    """asyncSetUp untuk kelas yang meminjam driver BotFlowE2E."""
    from tests import test_e2e_bot_flows as _e2e
    test._pins = pinned_settings()
    await _e2e.BotFlowE2E.asyncSetUp(test)


async def e2e_teardown(test):
    from tests import test_e2e_bot_flows as _e2e
    try:
        await _e2e.BotFlowE2E.asyncTearDown(test)
    finally:
        unpin(test._pins)
