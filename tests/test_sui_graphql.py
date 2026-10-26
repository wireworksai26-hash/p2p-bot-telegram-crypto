"""Penerjemah JSON-RPC Sui -> GraphQL (services/sui_graphql.py).

JSON-RPC publik Sui sudah dimatikan; sender/verifier/monitor memakai hasil berbentuk JSON-RPC,
jadi bentuk hasil terjemahan dan sikap fail-closed (balasan rusak bukan "saldo nol") dikunci di sini.
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if (ROOT / ".testdeps").exists():
    sys.path.insert(0, str(ROOT / ".testdeps"))

os.environ.update({
    "PYTHON_DOTENV_DISABLED": "1",
    "DATABASE_URL": "sqlite:///:memory:",
    "TELEGRAM_BOT_TOKEN": "123456:TEST_ONLY",
    "ADMIN_CHAT_IDS": "123456",
    "EVM_WALLET_ADDRESS": "0x" + "1" * 40,
    "EVM_PRIVATE_KEY": "",
})

from services import sui_graphql as g  # noqa: E402

URL = "https://graphql.example/graphql"
WALLET = "0x" + "ab" * 32


def run(coro):
    return asyncio.run(coro)


def gql(data):
    return patch.object(g, "_gql", new=AsyncMock(return_value=data))


class TestUrl(unittest.TestCase):
    def test_is_graphql_url(self):
        self.assertTrue(g.is_graphql_url("https://graphql.mainnet.sui.io/graphql"))
        self.assertTrue(g.is_graphql_url("https://x.io/GraphQL/"))
        self.assertFalse(g.is_graphql_url("https://mainnet.suiet.app"))
        self.assertFalse(g.is_graphql_url(""))
        self.assertFalse(g.is_graphql_url(None))


class TestTranslate(unittest.TestCase):
    def test_checkpoint(self):
        with gql({"checkpoint": {"sequenceNumber": 338320796}}):
            self.assertEqual(run(g.call(URL, "sui_getLatestCheckpointSequenceNumber", [])), "338320796")

    def test_balance(self):
        with gql({"address": {"balance": {"totalBalance": "47782541998"}}}):
            res = run(g.call(URL, "suix_getBalance", [WALLET, "0x2::sui::SUI"]))
        self.assertEqual(res["totalBalance"], "47782541998")

    def test_alamat_tanpa_koin_itu_saldo_nol_sungguhan(self):
        with gql({"address": {"balance": None}}):
            res = run(g.call(URL, "suix_getBalance", [WALLET, "0x2::sui::SUI"]))
        self.assertEqual(res["totalBalance"], "0")

    def test_coins_bentuk_json_rpc(self):
        node = {"address": "0x" + "c" * 64, "version": 7, "digest": "D1", "contents": {"json": {"balance": "42"}}}
        mock = AsyncMock(return_value={"address": {"objects": {"nodes": [node], "pageInfo": {"hasNextPage": False}}}})
        with patch.object(g, "_gql", new=mock):
            res = run(g.call(URL, "suix_getCoins", [WALLET, "0x2::sui::SUI", None, 500]))
        self.assertEqual(res["data"], [{"coinObjectId": node["address"], "version": "7", "digest": "D1", "balance": "42"}])
        variables = mock.await_args.args[2]
        self.assertEqual(variables["t"], "0x2::coin::Coin<0x2::sui::SUI>")
        self.assertEqual(variables["n"], g.PAGE_MAX)  # server menolak halaman > 50

    def test_gas_price(self):
        with gql({"epoch": {"referenceGasPrice": "100"}}):
            self.assertEqual(run(g.call(URL, "suix_getReferenceGasPrice", [])), "100")

    def test_dry_run_sukses_dan_gagal(self):
        ok = {"simulateTransaction": {"effects": {"status": "SUCCESS", "executionError": None, "gasEffects": {
            "gasSummary": {"computationCost": 100000, "storageCost": 1976000, "storageRebate": 987901}}}}}
        with gql(ok):
            res = run(g.call(URL, "sui_dryRunTransactionBlock", ["QUJD"]))
        self.assertEqual(res["effects"]["status"], {"status": "success"})
        self.assertEqual(res["effects"]["gasUsed"]["storageCost"], "1976000")
        bad = {"simulateTransaction": {"effects": {"status": "FAILURE", "executionError": {"message": "InsufficientGas"}}}}
        mock = AsyncMock(return_value=bad)
        with patch.object(g, "_gql", new=mock):
            res = run(g.call(URL, "sui_dryRunTransactionBlock", ["QUJD"]))
        self.assertEqual(res["effects"]["status"], {"status": "failure", "error": "InsufficientGas"})
        self.assertEqual(mock.await_args.args[2], {"t": {"bcs": {"value": "QUJD"}}})

    def test_execute(self):
        data = {"executeTransaction": {"effects": {"digest": "DIG", "status": "SUCCESS", "executionError": None}}}
        mock = AsyncMock(return_value=data)
        with patch.object(g, "_gql", new=mock):
            res = run(g.call(URL, "sui_executeTransactionBlock", ["QUJD", ["c2ln"], {"showEffects": True}, "WaitForLocalExecution"]))
        self.assertEqual(res, {"digest": "DIG", "effects": {"status": {"status": "success"}}})
        self.assertEqual(mock.await_args.args[2], {"d": "QUJD", "s": ["c2ln"]})

    def test_execute_tanpa_effects_bukan_sukses(self):
        with gql({"executeTransaction": {"effects": None}}):
            with self.assertRaises(RuntimeError):
                run(g.call(URL, "sui_executeTransactionBlock", ["QUJD", ["c2ln"], {}, "x"]))

    def test_get_transaction_dipetakan_seperti_json_rpc(self):
        data = {"transaction": {"digest": "DIG", "sender": {"address": "0xS"}, "effects": {
            "status": "SUCCESS", "timestamp": "2026-10-26T12:16:30.650Z", "checkpoint": {"sequenceNumber": 99},
            "balanceChanges": {"nodes": [{"owner": {"address": WALLET}, "coinType": {"repr": "0x2::sui::SUI"},
                                          "amount": "1500000000"}]}}}}
        with gql(data):
            res = run(g.call(URL, "sui_getTransactionBlock", ["DIG", {}]))
        self.assertEqual(res["effects"]["status"]["status"], "success")
        self.assertEqual(res["checkpoint"], "99")
        self.assertEqual(res["timestampMs"], "1793016990650")
        self.assertEqual(res["transaction"]["data"]["sender"], "0xS")
        self.assertEqual(res["balanceChanges"], [{"owner": {"AddressOwner": WALLET}, "coinType": "0x2::sui::SUI",
                                                  "amount": "1500000000"}])

    def test_transaksi_belum_ada_raise(self):
        with gql({"transaction": None}):
            with self.assertRaises(RuntimeError):
                run(g.call(URL, "sui_getTransactionBlock", ["DIG", {}]))

    def test_transaksi_belum_checkpoint_dilaporkan_tanpa_checkpoint(self):
        data = {"transaction": {"digest": "D", "sender": None, "effects": {
            "status": "SUCCESS", "timestamp": "2026-10-26T12:16:30Z", "checkpoint": None,
            "balanceChanges": {"nodes": []}}}}
        with gql(data):
            res = run(g.call(URL, "sui_getTransactionBlock", ["D", {}]))
        self.assertIsNone(res["checkpoint"])  # verifier menolak: "belum sukses/final"

    def test_transaksi_masuk_hanya_yang_menambah_saldo_wallet_terbaru_dulu(self):
        def tx(digest, owner, amount):
            return {"digest": digest, "effects": {"balanceChanges": {"nodes": [{"owner": {"address": owner}, "amount": amount}]}}}
        nodes = [tx("LAMA", WALLET, "5"), tx("KELUAR", WALLET, "-9"), tx("ORANG", "0x" + "ee" * 32, "7"), tx("BARU", WALLET, "3")]
        with gql({"transactions": {"nodes": nodes}}):
            res = run(g.call(URL, "suix_queryTransactionBlocks", [{"filter": {"ToAddress": WALLET.upper().replace("0X", "0x")}}, None, 100, True]))
        self.assertEqual([r["digest"] for r in res["data"]], ["BARU", "LAMA"])

    def test_metode_tak_dikenal_raise(self):
        with self.assertRaises(RuntimeError):
            run(g.call(URL, "sui_tidakAda", []))


class TestFailClosed(unittest.TestCase):
    def _http(self, body, status=200):
        class Resp:
            def raise_for_status(self_inner):
                if status >= 400:
                    raise RuntimeError(f"HTTP {status}")

            def json(self_inner):
                return body

        class Client:
            async def __aenter__(self_inner):
                return self_inner

            async def __aexit__(self_inner, *a):
                return False

            async def post(self_inner, url, json=None):
                return Resp()

        return patch("httpx.AsyncClient", return_value=Client())

    def test_balasan_error_json_rpc_bukan_saldo_nol(self):
        with self._http({"error": {"code": -1}}):
            with self.assertRaises(RuntimeError):
                run(g.call(URL, "suix_getBalance", [WALLET, "0x2::sui::SUI"]))

    def test_errors_graphql_diangkat(self):
        with self._http({"data": None, "errors": [{"message": "Page size is too large"}]}):
            with self.assertRaises(RuntimeError) as ctx:
                run(g.call(URL, "suix_getBalance", [WALLET, "0x2::sui::SUI"]))
        self.assertIn("Page size", str(ctx.exception))

    def test_alamat_null_bukan_saldo_nol(self):
        with self._http({"data": {"address": None}}):
            with self.assertRaises(RuntimeError):
                run(g.call(URL, "suix_getBalance", [WALLET, "0x2::sui::SUI"]))

    def test_http_error_diangkat(self):
        with self._http({}, status=503):
            with self.assertRaises(RuntimeError):
                run(g.call(URL, "sui_getLatestCheckpointSequenceNumber", []))


class TestVerifierMemakaiGraphql(unittest.TestCase):
    def test_verify_sui_lewat_graphql_dan_urutan_host(self):
        from services import tx_verifier
        self.assertTrue(g.is_graphql_url(tx_verifier.SUI_RPCS[0]))
        data = {"transaction": {"digest": "DIG", "sender": {"address": "0x" + "5" * 64}, "effects": {
            "status": "SUCCESS", "timestamp": "2026-10-26T12:16:30.650Z", "checkpoint": {"sequenceNumber": 99},
            "balanceChanges": {"nodes": [{"owner": {"address": WALLET},
                                          "coinType": {"repr": "0x" + "0" * 63 + "2::sui::SUI"}, "amount": "2500000000"}]}}}}
        with patch.object(tx_verifier, "SUI_RPCS", [URL]), gql(data):
            res = run(tx_verifier._verify_sui("DIG", WALLET))
        self.assertTrue(res["verified"])
        self.assertAlmostEqual(res["amount"], 2.5)
        self.assertEqual(res["from_address"], "0x" + "5" * 64)

    def test_verify_sui_belum_checkpoint_ditolak(self):
        from services import tx_verifier
        data = {"transaction": {"digest": "DIG", "sender": None, "effects": {
            "status": "SUCCESS", "timestamp": "2026-10-26T12:16:30Z", "checkpoint": None,
            "balanceChanges": {"nodes": []}}}}
        with patch.object(tx_verifier, "SUI_RPCS", [URL]), gql(data):
            res = run(tx_verifier._verify_sui("DIG", WALLET))
        self.assertFalse(res["verified"])


if __name__ == "__main__":
    unittest.main()
