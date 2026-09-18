# ===================================================
# P2P Crypto Telegram Bot & GoPay Gateway (All-in-One)
# ===================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV NODE_ENV=production

# 1. Install Node.js 22 & PM2
RUN apt-get update && apt-get install -y \
    curl \
    gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && npm install -g pm2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 2. Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 3. Install Node.js dependencies
COPY gopay-gateway/package*.json ./gopay-gateway/
RUN cd gopay-gateway && npm install --production

# 4. Copy entire codebase, fix CRLF line-endings, and set execute permissions
COPY . .
RUN sed -i 's/\r$//' start.sh && chmod +x start.sh

EXPOSE 3005 8000

CMD ["bash", "start.sh"]
