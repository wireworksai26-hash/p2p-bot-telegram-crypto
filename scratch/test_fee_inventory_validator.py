"""Scratch fee, inventory, and address validator sanity checks."""

import os
import sys
from pathlib import Path
from datetime import datetime
from decimal import Decimal

os.environ["PYTHON_DOTENV_DISABLED"] = "1"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
testdeps = Path(__file__).resolve().parents[1] / ".testdeps"
if testdeps.exists():
    sys.path.insert(0, str(testdeps))

from database.connection import Base, SessionLocal, engine
from database.crud import (
    get_available_inventory,
    release_order_inventory,
    reserve_order_inventory,
)
from database.models import WalletBalance
from services.fee_service import calculate_fee_idr
from bot.utils.validator import validate_wallet_address


def main():
    assert calculate_fee_idr(1_010_000, "ALTCOIN") == 19_000
    try:
        calculate_fee_idr(1_010_001, "ALTCOIN")
        assert False, "Should raise ValueError for above max Altcoin"
    except ValueError:
        pass

    assert calculate_fee_idr(1_015_000, "USD") == 14_500
    try:
        calculate_fee_idr(1_015_001, "USD")
        assert False, "Should raise ValueError for above max USD"
    except ValueError:
        pass

    assert calculate_fee_idr(1_010_000, "CONVERT") == 18_000
    try:
        calculate_fee_idr(1_010_001, "CONVERT")
        assert False, "Should raise ValueError for above max Convert"
    except ValueError:
        pass
    print("[PASS] fee tiers, caps, and limit validation")

    assert validate_wallet_address("0x" + "a" * 40, "ROBINHOOD")
    assert validate_wallet_address("0x" + "a" * 64, "SUI")
    assert validate_wallet_address("0x1", "APTOS")
    assert validate_wallet_address("0:" + "a" * 64, "TON")
    assert not validate_wallet_address("not-a-ton-wallet", "TON")
    print("[PASS] EVM/TON/SUI/APTOS validators")

    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    db.add(WalletBalance(
        network="SOLANA", symbol="USDC", balance=6,
        reserved_balance=0, address="A" * 32, sync_status="OK",
        last_checked_at=datetime.utcnow(), last_success_at=datetime.utcnow(),
    ))
    db.commit()
    assert reserve_order_inventory(db, "ORD-1", "SOLANA", "USDC", Decimal("5"))
    assert get_available_inventory(db, "SOLANA", "USDC") == Decimal("1")
    assert reserve_order_inventory(db, "ORD-1", "SOLANA", "USDC", Decimal("5"))
    assert not reserve_order_inventory(db, "ORD-2", "SOLANA", "USDC", Decimal("2"))
    assert release_order_inventory(db, "ORD-1")
    assert get_available_inventory(db, "SOLANA", "USDC") == Decimal("6")
    print("[PASS] inventory reservation prevents oversell and releases cleanly")

    db.close()
    engine.dispose()

    print("ALL FEE/INVENTORY/VALIDATOR TESTS PASSED")


if __name__ == "__main__":
    main()
