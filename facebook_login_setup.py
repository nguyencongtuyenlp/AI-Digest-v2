#!/usr/bin/env python3
"""
facebook_login_setup.py - One-time bootstrap for Facebook auto ingestion.

What it does:
- opens Google Chrome with a dedicated persistent profile
- navigates to facebook.com
- lets the user log in manually

After this, scheduled runs can reuse the saved session to read page/group feeds
for the Facebook News topic.
"""

from __future__ import annotations

import contextlib
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
env_path = PROJECT_ROOT / "config" / ".env"
if env_path.exists():
    load_dotenv(env_path)


def main() -> None:
    try:
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        raise SystemExit(f"Playwright chưa sẵn sàng: {exc}")

    chrome_executable = (
        __import__("os").getenv(
            "FACEBOOK_CHROME_EXECUTABLE",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ).strip()
        or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    )
    profile_dir = Path(
        __import__("os").getenv(
            "FACEBOOK_CHROME_PROFILE_DIR",
            str(PROJECT_ROOT / "config" / "facebook_chrome_profile"),
        ).strip()
        or str(PROJECT_ROOT / "config" / "facebook_chrome_profile")
    ).expanduser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    storage_state_path = Path(
        __import__("os").getenv(
            "FACEBOOK_STORAGE_STATE_FILE",
            str(PROJECT_ROOT / "config" / "facebook_storage_state.json"),
        ).strip()
        or str(PROJECT_ROOT / "config" / "facebook_storage_state.json")
    ).expanduser()
    storage_state_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Chrome executable: {chrome_executable}")
    print(f"Profile dir      : {profile_dir}")
    print(f"Storage state    : {storage_state_path}")
    print("Sắp mở Chrome profile riêng cho Facebook auto adapter.")
    print("Hãy đăng nhập Facebook trong cửa sổ đó, rồi quay lại terminal và nhấn Enter.")

    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            executable_path=chrome_executable,
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://www.facebook.com/", wait_until="domcontentloaded", timeout=60000)
        try:
            input("Nhấn Enter sau khi đã login Facebook xong... ")
        except KeyboardInterrupt:
            print("\nĐã huỷ bởi người dùng.", file=sys.stderr)
        finally:
            with contextlib.suppress(Exception):
                context.storage_state(path=str(storage_state_path))
            context.close()

    print("Đã lưu session. Từ giờ pipeline có thể thử đọc Facebook page/group bằng storage state này.")


if __name__ == "__main__":
    main()
