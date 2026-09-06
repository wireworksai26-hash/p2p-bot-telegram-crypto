#!/bin/bash
set -e

echo "🚀 [STARTUP] Menyiapkan environment dual-service..."

# 1. Pulihkan sesi login GoPay dari secrets atau Database PostgreSQL
if [ -n "$GOPAY_SESSION_JSON" ]; then
    mkdir -p /app/gopay-gateway
    echo "$GOPAY_SESSION_JSON" > /app/gopay-gateway/.GOPAY_SESI_JANGAN_DIHAPUS.json
    echo "🔑 [GOPAY] Sesi GoBiz berhasil dimuat dari environment secrets."
fi

# Pulihkan sesi GoPay dari PostgreSQL sebelum servis dijalankan
python3 -c "
import sys
try:
    from database.crud import sync_gopay_session_file
    sync_gopay_session_file()
except Exception as e:
    print('ℹ️ [GOPAY] Info sync DB startup:', e)
" 2>/dev/null || true

# 2. Buat file gopay-gateway/.env jika belum ada atau update DATABASE_URL
cat <<EOF > /app/gopay-gateway/.env
PORT=3005
API_KEY=RAHASIA
DATABASE_URL=${DATABASE_URL}
QRIS_STATIC=${QRIS_STATIC:-00020101021126610014COM.GO-JEK.WWW01189360091432922297020210G2922297020303UMI51440014ID.CO.QRIS.WWW0215ID10265038922870303UMI5204899953033605802ID5925Toko digital HSN, Digital6008SIDOARJO61056126162070703A016304A581}
GOPAY_MERCHANT_ID=${GOPAY_MERCHANT_ID:-G292229702}
EOF


# 3. Jalankan semua servis via PM2
echo "🟢 [PM2] Menjalankan gopay-gateway dan p2p-telegram-bot..."
exec pm2-runtime start ecosystem.config.js
