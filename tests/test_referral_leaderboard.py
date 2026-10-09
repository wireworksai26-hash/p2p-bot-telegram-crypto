"""Leaderboard referral publik: nama disensor (Ox***un), tanpa '@', plus posisi pribadi."""
import os
import re
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123456:TEST_ONLY")
os.environ.setdefault("ADMIN_CHAT_IDS", "1")

import database.models  # noqa: F401,E402
from database import crud  # noqa: E402
from database.connection import Base, SessionLocal, engine  # noqa: E402
from database.models import Referral, User  # noqa: E402
from bot.utils.formatter import mask_public_name  # noqa: E402
from bot.handlers.referral import referral_leaderboard_handler  # noqa: E402


class MaskPublicName(unittest.TestCase):
    def test_contoh_dari_client(self):
        self.assertEqual(mask_public_name("@Oxhusnun"), "Ox***un")
        self.assertEqual(mask_public_name("@Nandaderak"), "Na***ak")
        self.assertEqual(mask_public_name("Oxhusnun"), "Ox***un")

    def test_tidak_pernah_ada_at(self):
        for name in ("@abc@def@ghi", "@@@Budi", "a@b@c@d@e@f", "@"):
            with self.subTest(name=name):
                self.assertNotIn("@", mask_public_name(name))

    def test_nama_pendek_disensor_lebih_ketat(self):
        self.assertEqual(mask_public_name("Budi"), "B***i")
        self.assertEqual(mask_public_name("Ana"), "A***a")
        self.assertEqual(mask_public_name("Ab"), "A***")
        self.assertEqual(mask_public_name("A"), "A***")
        self.assertEqual(mask_public_name("Budis"), "Bu***is")

    def test_kosong_atau_none(self):
        for empty in (None, "", "   ", "@", "\x00\x01"):
            with self.subTest(empty=empty):
                self.assertEqual(mask_public_name(empty), "User***")

    def test_nama_tidak_pernah_terbaca_utuh(self):
        for name in ("Budi", "Budis", "Nandaderak", "Ox", "abcdef"):
            masked = mask_public_name(name)
            self.assertNotEqual(masked, name)
            self.assertIn("***", masked)

    def test_spasi_dirapikan_dan_id_angka_disensor(self):
        self.assertEqual(mask_public_name("  Budi   Santoso "), "Bu***so")
        self.assertEqual(mask_public_name(6440006997), "64***97")

    def test_karakter_kontrol_dibuang(self):
        self.assertEqual(mask_public_name("Ox​husnun\n"), "Ox***un")  # zero-width & newline dibuang


def _user(uid, username=None, full_name=None):
    return User(telegram_id=uid, username=username, full_name=full_name, balance_idr=Decimal("0"))


def _ref(referrer, referee, reward, status="COMPLETED"):
    return Referral(referrer_id=referrer, referee_id=referee, status=status, reward_idr=reward)


class _Seeded(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Base.metadata.create_all(bind=engine)
        self.db = SessionLocal()

    def tearDown(self):
        self.db.close()
        Base.metadata.drop_all(bind=engine)

    def seed(self, referrers):
        """referrers: {id: (username, [reward, ...])} -> referee id unik per baris."""
        nxt = 100000
        for rid, (uname, rewards) in referrers.items():
            self.db.add(_user(rid, uname))
            for rw in rewards:
                nxt += 1
                self.db.add(_user(nxt))
                self.db.flush()
                self.db.add(_ref(rid, nxt, rw))
        self.db.commit()

    async def render(self, viewer_id):
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(),
                                message=SimpleNamespace(reply_text=AsyncMock()))
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=viewer_id))
        context = SimpleNamespace(bot=SimpleNamespace(get_me=AsyncMock(return_value=SimpleNamespace(username="TokoKoinID_bot"))))
        await referral_leaderboard_handler(update, context)
        kwargs = query.edit_message_text.await_args.kwargs
        return kwargs["text"], kwargs["reply_markup"]


class RankingOrder(_Seeded):
    def test_urutan_jumlah_lalu_reward_lalu_id(self):
        self.seed({
            10: ("aaaaaa", [1000, 1000]),          # 2 referral, 2000
            20: ("bbbbbb", [1500, 1500]),          # 2 referral, 3000 -> di atas 10
            30: ("cccccc", [500, 500]),            # 2 referral, 1000
            40: ("dddddd", [9000]),                # 1 referral
            15: ("eeeeee", [1000, 1000]),          # 2 referral, 2000 (sama dengan 10) -> id lebih besar, di bawah 10
        })
        order = [r.referrer_id for r in crud.get_top_referrers(self.db, limit=10)]
        self.assertEqual(order, [20, 10, 15, 30, 40])

    def test_rank_pribadi_cocok_dengan_daftar(self):
        self.seed({
            10: ("aaaaaa", [1000, 1000]), 20: ("bbbbbb", [1500, 1500]), 30: ("cccccc", [500, 500]),
            40: ("dddddd", [9000]), 15: ("eeeeee", [1000, 1000]),
        })
        top = [r.referrer_id for r in crud.get_top_referrers(self.db, limit=10)]
        for position, rid in enumerate(top, 1):
            with self.subTest(referrer=rid):
                self.assertEqual(crud.get_referrer_rank(self.db, rid)["rank"], position)

    def test_tanpa_referral_selesai_tidak_punya_peringkat(self):
        self.seed({10: ("aaaaaa", [1000])})
        self.db.add(_user(55, "pemalas"))
        self.db.commit()
        self.assertEqual(crud.get_referrer_rank(self.db, 55), {"rank": None, "total": 0, "total_reward": 0})

    def test_referral_pending_tidak_dihitung(self):
        self.db.add_all([_user(10, "aaaaaa"), _user(901), _user(902)])
        self.db.commit()
        self.db.add_all([_ref(10, 901, 0, status="PENDING"), _ref(10, 902, 1210)])
        self.db.commit()
        me = crud.get_referrer_rank(self.db, 10)
        self.assertEqual((me["rank"], me["total"], me["total_reward"]), (1, 1, 1210))


class LeaderboardScreen(_Seeded):
    async def test_nama_disensor_tanpa_at_dan_nominal_tampil(self):
        self.seed({10: ("Oxhusnun", [1210]), 20: ("Nandaderak", [1000, 1000])})
        text, _ = await self.render(viewer_id=999)
        self.assertIn("Na***ak", text)
        self.assertIn("Ox***un", text)
        self.assertNotIn("Oxhusnun", text)
        self.assertNotIn("Nandaderak", text)
        self.assertNotIn("@", text)  # tidak ada mention aktif sama sekali
        self.assertIn("<b>2</b> referral (Rp 2.000)", text)
        self.assertIn("<b>1</b> referral (Rp 1.210)", text)

    async def test_urutan_dan_medali(self):
        self.seed({10: ("aaaaaaaa", [1]), 20: ("bbbbbbbb", [1, 1]), 30: ("cccccccc", [1, 1, 1])})
        text, _ = await self.render(999)
        first, second, third = text.index("cc***cc"), text.index("bb***bb"), text.index("aa***aa")
        self.assertLess(first, second)
        self.assertLess(second, third)
        self.assertIn("🥇 cc***cc", text)
        self.assertIn("🥈 bb***bb", text)
        self.assertIn("🥉 aa***aa", text)

    async def test_pengguna_tanpa_username_pakai_nama_lalu_id_semuanya_disensor(self):
        self.db.add(_user(777001, None, "Budi Santoso"))
        self.db.add(_user(777002, None, None))
        self.db.add_all([_user(8001), _user(8002)])
        self.db.commit()
        self.db.add_all([_ref(777001, 8001, 1000), _ref(777002, 8002, 1000)])
        self.db.commit()
        text, _ = await self.render(1)
        self.assertIn("Bu***so", text)
        self.assertIn("77***02", text)
        self.assertNotIn("777002", text)
        self.assertNotIn("Budi Santoso", text)

    async def test_nama_berisi_html_di_escape_dan_tidak_merusak_pesan(self):
        self.seed({10: ("<b>x&y</b>", [1000])})
        text, _ = await self.render(999)
        self.assertNotIn("<b>x", text)
        self.assertEqual(text.count("<b>") , text.count("</b>"))

    async def test_posisi_pribadi_belum_masuk_top_10(self):
        self.seed({10: ("aaaaaaaa", [1000])})
        text, _ = await self.render(999)
        self.assertIn("━━━━━━━━━━━━━━━━━━━━━━━━", text)
        self.assertIn("Posisi Anda Saat Ini", text)
        self.assertIn("Peringkat: <b>Belum Masuk Top 10</b> | Total: <b>0</b> Referral (Rp 0)", text)
        self.assertIn("Bagikan link referral Anda untuk mendaki leaderboard", text)

    async def test_posisi_pribadi_di_dalam_top_10_ditandai(self):
        self.seed({10: ("aaaaaaaa", [1000, 1000]), 20: ("bbbbbbbb", [1500])})
        text, _ = await self.render(20)
        self.assertIn("Peringkat: <b>#2</b> | Total: <b>1</b> Referral (Rp 1.500)", text)
        self.assertIn("bb***bb", text)
        self.assertIn("Kamu", text)
        self.assertEqual(text.count("Kamu"), 1)

    async def test_pengguna_di_luar_top_10_melihat_peringkatnya(self):
        referrers = {1000 + i: (f"user{i:02d}xx", [1000] * (30 - i)) for i in range(12)}  # 12 referrer, jumlah menurun
        self.seed(referrers)
        text, _ = await self.render(1011)  # peringkat 12
        self.assertIn("Belum Masuk Top 10 (#12)", text)
        self.assertEqual(text.count("</b> referral ("), 10)  # tepat 10 baris daftar
        self.assertEqual(text.count("Total: <b>"), 1)        # + 1 baris posisi pribadi
        self.assertNotIn("Kamu", text)  # dia tidak ada di daftar 10 besar

    async def test_belum_ada_data(self):
        text, _ = await self.render(999)
        self.assertIn("Belum ada data referral", text)
        self.assertIn("Posisi Anda Saat Ini", text)

    async def test_tombol_bagikan_dan_navigasi(self):
        self.seed({10: ("aaaaaaaa", [1000])})
        _, markup = await self.render(999)
        buttons = [b for row in markup.inline_keyboard for b in row]
        share = [b for b in buttons if b.url]
        self.assertEqual(len(share), 1)
        self.assertIn("ref_999", share[0].url)  # link referral milik pengguna yang melihat
        self.assertEqual({b.callback_data for b in buttons if b.callback_data}, {"menu_referral", "menu_back"})

    async def test_gagal_get_me_tidak_merusak_leaderboard(self):
        self.seed({10: ("aaaaaaaa", [1000])})
        query = SimpleNamespace(answer=AsyncMock(), edit_message_text=AsyncMock(),
                                message=SimpleNamespace(reply_text=AsyncMock()))
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=999))
        context = SimpleNamespace(bot=SimpleNamespace(get_me=AsyncMock(side_effect=RuntimeError("down"))))
        await referral_leaderboard_handler(update, context)
        self.assertIn("aa***aa", query.edit_message_text.await_args.kwargs["text"])
        markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertFalse([b for row in markup.inline_keyboard for b in row if b.url])


if __name__ == "__main__":
    unittest.main()
