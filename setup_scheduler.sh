#!/bin/bash
# setup_scheduler.sh — Cài đặt lịch chạy tự động 8:00 AM hàng ngày cho Daily Digest Agent

set -e

PROJECT_DIR="/Users/quangdang/Projects/daily-digest-agent"
PLIST_SRC="$PROJECT_DIR/launchd.plist"
PLIST_DEST="$HOME/Library/LaunchAgents/com.quangdang.daily-digest-agent.plist"
LABEL="com.quangdang.daily-digest-agent"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🤖 Daily Digest Agent — Setup Scheduler"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Kiểm tra Python venv
echo ""
echo "✅ [1/4] Kiểm tra Python virtual environment..."
PYTHON="$PROJECT_DIR/.venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    echo "❌ KHÔNG TÌM THẤY: $PYTHON"
    echo "   Hãy chạy: cd $PROJECT_DIR && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi
echo "   Python: $($PYTHON --version)"

# 2. Kiểm tra main.py chạy được
echo ""
echo "✅ [2/4] Kiểm tra các package cần thiết..."
$PYTHON -c "import langchain_community, langgraph, notion_client, httpx, feedparser, trafilatura, ddgs" 2>/dev/null && \
    echo "   ✅ Tất cả package OK" || \
    echo "   ⚠️  Một số package thiếu — hãy chạy: .venv/bin/pip install -r requirements.txt"

# 3. Kiểm tra .env
echo ""
echo "✅ [3/4] Kiểm tra cấu hình .env..."
source "$PROJECT_DIR/config/.env" 2>/dev/null || true
if [ -z "$NOTION_TOKEN" ]; then
    echo "   ❌ NOTION_TOKEN chưa cấu hình trong config/.env"
    exit 1
fi
if [ -z "$TELEGRAM_BOT_TOKEN" ]; then
    echo "   ❌ TELEGRAM_BOT_TOKEN chưa cấu hình trong config/.env"
    exit 1
fi
echo "   ✅ NOTION_TOKEN: ${NOTION_TOKEN:0:15}..."
echo "   ✅ TELEGRAM_BOT_TOKEN: ${TELEGRAM_BOT_TOKEN:0:15}..."
echo "   ✅ MLX_MODEL: $MLX_MODEL"

# 4. Đăng ký launchd
echo ""
echo "✅ [4/4] Đăng ký lịch chạy 8:00 AM với launchd..."

# Gỡ bỏ cũ nếu có
launchctl unload "$PLIST_DEST" 2>/dev/null || true
rm -f "$PLIST_DEST"

# Copy plist vào LaunchAgents
cp "$PLIST_SRC" "$PLIST_DEST"
chmod 644 "$PLIST_DEST"

# Đăng ký với launchd
launchctl load "$PLIST_DEST"

# Kiểm tra đã được đăng ký chưa
if launchctl list | grep -q "$LABEL"; then
    echo "   ✅ Đã đăng ký thành công!"
else
    echo "   ❌ Đăng ký thất bại — kiểm tra lại plist"
    exit 1
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 HOÀN TẤT! Agent sẽ tự động chạy lúc 8:00 AM mỗi ngày."
echo ""
echo "📋 Các lệnh hữu ích:"
echo "   Xem log realtime:  tail -f $PROJECT_DIR/digest.log"
echo "   Chạy thủ công:     cd $PROJECT_DIR && .venv/bin/python main.py"
echo "   Tắt scheduler:     launchctl unload $PLIST_DEST"
echo "   Bật lại:           launchctl load $PLIST_DEST"
echo "   Kiểm tra trạng thái: launchctl list | grep daily-digest"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
