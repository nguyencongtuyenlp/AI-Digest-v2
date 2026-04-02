from __future__ import annotations

import os
import shutil
from pathlib import Path

import httpx


token = os.getenv("NOTION_TOKEN", "").strip()
db_id = os.getenv("NOTION_DATABASE_ID", "").strip()

if not token or not db_id:
    raise SystemExit("Set NOTION_TOKEN and NOTION_DATABASE_ID before running fix_notion.py")

headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
    "Notion-Version": "2022-06-28",
}

payload = {
    "properties": {
        "Mức độ phù hợp dự án": {
            "select": {
                "options": [
                    {"name": "High", "color": "green"},
                    {"name": "Medium", "color": "yellow"},
                    {"name": "Low", "color": "red"},
                ]
            }
        }
    }
}

r = httpx.patch(f"https://api.notion.com/v1/databases/{db_id}", headers=headers, json=payload)
print("Notion DB Patch Status:", r.status_code)
if r.status_code == 200:
    print("✅ Đã tạo thành công cột 'Mức độ phù hợp dự án' vào Notion!")
else:
    print("❌ Lỗi:", r.text)

db_path = Path.home() / ".daily-digest-agent"
if db_path.exists():
    shutil.rmtree(db_path)
    print("✅ Đã xoá sạch trí nhớ ảo (Local DB), sẵn sàng cắn lại dữ liệu cũ làm mới!")
