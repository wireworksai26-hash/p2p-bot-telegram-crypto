import os
from dotenv import load_dotenv

# Load .env file only if exists without overriding container environment
if os.path.exists(".env"):
    load_dotenv(".env", override=False)
elif os.path.exists("../.env"):
    load_dotenv("../.env", override=False)

class Settings:
    # Telegram settings
    TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    _admin_env = os.getenv("ADMIN_CHAT_IDS") or os.getenv("ADMIN_CHAT_ID") or ""
    ADMIN_CHAT_IDS = [int(x.strip()) for x in _admin_env.split(",") if x.strip()]
    OWNER_USERNAME = (os.getenv("OWNER_USERNAME", "TimRobbyPR") or "").strip()

    # Server settings
    DATABASE_URL = (os.getenv("DATABASE_URL", "sqlite:///./p2p_bot.db") or "").strip()
    PORT = int(os.getenv("PORT", 8000))

    # GoPay / Gopiz API Gateway
    GOPAY_GATEWAY_URL = (os.getenv("GOPAY_GATEWAY_URL", "http://127.0.0.1:3005") or "").strip()
    GOPAY_API_KEY = (os.getenv("GOPAY_API_KEY", "RAHASIA") or "").strip()
    GOPAY_MERCHANT_ID = (os.getenv("GOPAY_MERCHANT_ID") or "").strip()
    QRIS_STATIC = (os.getenv("QRIS_STATIC") or "00020101021126610014COM.GO-JEK.WWW01189360091432922297020210G2922297020303UMI51440014ID.CO.QRIS.WWW0215ID10265038922870303UMI5204899953033605802ID5925Toko digital HSN, Digital6008SIDOARJO61056126162070703A016304A581").strip()

    # Wallets & RPC Endpoints
    EVM_PRIVATE_KEY = (os.getenv("EVM_PRIVATE_KEY") or "").strip()
    EVM_WALLET_ADDRESS = (os.getenv("EVM_WALLET_ADDRESS") or "").strip()
    
    # EVM RPC Endpoints
    BSC_RPC = (os.getenv("BSC_RPC", "https://bsc-rpc.publicnode.com") or "").strip()
    ETH_RPC = (os.getenv("ETH_RPC", "https://ethereum-rpc.publicnode.com") or "").strip()
    AVAX_RPC = (os.getenv("AVAX_RPC", "https://avalanche-c-chain-rpc.publicnode.com") or "").strip()
    POLYGON_RPC = (os.getenv("POLYGON_RPC", "https://polygon-bor-rpc.publicnode.com") or "").strip()
    BASE_RPC = (os.getenv("BASE_RPC", "https://base-rpc.publicnode.com") or "").strip()
    ARB_RPC = (os.getenv("ARB_RPC", "https://arbitrum-one-rpc.publicnode.com") or "").strip()
    OPTIMISM_RPC = (os.getenv("OPTIMISM_RPC", "https://optimism-rpc.publicnode.com") or "").strip()
    ROBINHOOD_RPC = (os.getenv("ROBINHOOD_RPC", "https://rpc.robinhood.com") or "").strip()
    KAIA_RPC = (os.getenv("KAIA_RPC", "https://klaytn.drpc.org") or "").strip()
    BERA_RPC = (os.getenv("BERA_RPC", "https://berachain.drpc.org") or "").strip()
    HYPEREVM_RPC = (os.getenv("HYPEREVM_RPC", "https://rpc.hyperliquid.xyz/evm") or "").strip()
    GRAVITY_RPC = (os.getenv("GRAVITY_RPC", "https://rpc.gravity.xyz") or "").strip()

    # Non-EVM RPCs & Wallets
    SOL_RPC = (os.getenv("SOL_RPC", "https://api.mainnet-beta.solana.com") or "").strip()
    SOL_PRIVATE_KEY = (os.getenv("SOL_PRIVATE_KEY") or "").strip()
    SOL_WALLET_ADDRESS = (os.getenv("SOL_WALLET_ADDRESS") or "").strip()

    TRX_RPC = (os.getenv("TRX_RPC", "https://api.trongrid.io") or "").strip()
    TRX_PRIVATE_KEY = (os.getenv("TRX_PRIVATE_KEY") or "").strip()
    TRX_WALLET_ADDRESS = (os.getenv("TRX_WALLET_ADDRESS") or "").strip()

    SUI_RPC = (os.getenv("SUI_RPC", "https://fullnode.mainnet.sui.io:443") or "").strip()
    SUI_PRIVATE_KEY = (os.getenv("SUI_PRIVATE_KEY") or "").strip()
    SUI_WALLET_ADDRESS = (os.getenv("SUI_WALLET_ADDRESS") or "").strip()

    TON_RPC = (os.getenv("TON_RPC", "https://toncenter.com/api/v2/jsonRPC") or "").strip()
    TON_API_KEY = (os.getenv("TON_API_KEY") or "").strip()
    TON_PRIVATE_KEY = (os.getenv("TON_PRIVATE_KEY") or "").strip()
    TON_WALLET_ADDRESS = (os.getenv("TON_WALLET_ADDRESS") or "").strip()

    APTOS_RPC = (os.getenv("APTOS_RPC", "https://fullnode.mainnet.aptoslabs.com/v1") or "").strip()
    APTOS_PRIVATE_KEY = (os.getenv("APTOS_PRIVATE_KEY") or "").strip()
    APTOS_WALLET_ADDRESS = (os.getenv("APTOS_WALLET_ADDRESS") or "").strip()
    
    _admin_group = (os.getenv("ADMIN_GROUP_ID") or "").strip()
    ADMIN_GROUP_ID = int(_admin_group) if _admin_group and _admin_group.lstrip("-").isdigit() else None

    # App logic configurations
    ORDER_EXPIRE_MINUTES = int(os.getenv("ORDER_EXPIRE_MINUTES", 30))
    DEFAULT_SPREAD_PCT = float(os.getenv("DEFAULT_SPREAD_PCT", 1.5))
    ENABLE_LOW_BALANCE_ALERT = os.getenv("ENABLE_LOW_BALANCE_ALERT", "false").lower() in ("true", "1", "yes")
    LOW_BALANCE_ALERT_HOURS = int(os.getenv("LOW_BALANCE_ALERT_HOURS", 6))

settings = Settings()

print(f"[CONFIG] Token Bot Terdeteksi: {bool(settings.TELEGRAM_BOT_TOKEN)} (Panjang: {len(settings.TELEGRAM_BOT_TOKEN)})")

