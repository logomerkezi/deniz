#!/usr/bin/env python3
"""
DENİZ Web — Bilgisayar Ajanı
─────────────────────────────
Backend sunucusuna bağlanır ve sistem araçlarını (uygulama açma, takvim,
anımsatıcı, shell, ekran analizi, WhatsApp, medya...) bu bilgisayarda
çalıştırır. macOS ve Windows'ta aynı şekilde çalışır.

Çalıştırma:
    python agent.py                           # localhost'taki sunucuya
    python agent.py --server ws://1.2.3.4:8765 --token <token>

Token verilmezse aynı klasördeki web_config.json'dan okunur.
"""

from __future__ import annotations

import sys

import asyncio
import argparse
import json
import ssl
import traceback
from pathlib import Path

import websockets

# ── Ana proje modüllerine erişim ─────────────────────────────────────────────
WEB_DIR  = Path(__file__).resolve().parent
BASE_DIR = WEB_DIR.parent
sys.path.insert(0, str(BASE_DIR))

from actions.open_app  import open_app
from actions.sys_info  import sys_info
from actions.calendar  import get_calendar_events, add_calendar_event, delete_calendar_event
from actions.reminders import get_reminders, add_reminder
from actions.browser   import browser_control
from actions.shell     import shell_run
from actions.whatsapp  import send_whatsapp_message, save_whatsapp_contact
from actions.media     import play_media, control_media
from actions.screen_vision import analyze_screen
from actions.youtube_stats import get_youtube_channel_report

RECONNECT_DELAY = 3.0


def execute_tool(name: str, args: dict) -> str:
    """Masaüstü main.py'deki araç dağıtımının ajan kopyası (UI'sız)."""
    try:
        if name == "open_app":
            return open_app(args.get("app_name", "")) or \
                   f"{args.get('app_name')} açıldı."

        if name == "sys_info":
            return sys_info(args.get("query", "all")) or "Bilgi alındı."

        if name == "get_calendar_events":
            return get_calendar_events(
                args.get("query", "today"),
                int(args.get("limit", 6) or 6),
            ) or "Takvim bilgisi alindi."

        if name == "add_calendar_event":
            return add_calendar_event(
                args.get("title", ""),
                args.get("start_iso", ""),
                args.get("end_iso", ""),
                args.get("notes", ""),
                args.get("location", ""),
                args.get("calendar_name", ""),
                bool(args.get("all_day", False)),
            ) or "Takvim etkinligi eklendi."

        if name == "delete_calendar_event":
            return delete_calendar_event(
                args.get("title", ""),
                args.get("start_iso", ""),
                args.get("calendar_name", ""),
                bool(args.get("delete_all_matches", False)),
            ) or "Takvim etkinligi silindi."

        if name == "get_reminders":
            return get_reminders(
                args.get("query", "upcoming"),
                int(args.get("limit", 8) or 8),
                args.get("list_name", ""),
            ) or "Animsatici bilgisi alindi."

        if name == "add_reminder":
            return add_reminder(
                args.get("title", ""),
                args.get("due_iso", ""),
                args.get("notes", ""),
                args.get("list_name", ""),
                args.get("priority", ""),
                bool(args.get("all_day", False)),
            ) or "Animsatici eklendi."

        if name == "browser_control":
            return browser_control(
                args.get("action"),
                args.get("url"),
                args.get("query"),
            ) or "Tamam."

        if name == "shell_run":
            return shell_run(args.get("command", "")) or "Komut çalıştırıldı."

        if name == "play_media":
            return play_media(
                args.get("query", ""),
                args.get("provider", "auto"),
                bool(args.get("autoplay", True)),
            ) or "Medya oynatma başlatıldı."

        if name == "control_media":
            return control_media(args.get("action", "pause")) or "Medya komutu gonderildi."

        if name == "get_youtube_channel_report":
            return get_youtube_channel_report(
                args.get("query", "overview"),
                args.get("handle", ""),
                int(args.get("video_limit", 6) or 6),
            ) or "YouTube kanal raporu alindi."

        if name == "analyze_screen":
            return analyze_screen(
                args.get("query", "Ekranda ne var?"),
                args.get("target", "active_window"),
            ) or "Ekran analizi tamamlandi."

        if name == "send_whatsapp_message":
            return send_whatsapp_message(
                args.get("message", ""),
                args.get("phone_number", ""),
                args.get("recipient_name", ""),
                bool(args.get("send_now", False)),
                args.get("app_target", "auto"),
            ) or "WhatsApp işlemi tamamlandı."

        if name == "save_whatsapp_contact":
            return save_whatsapp_contact(
                args.get("display_name", ""),
                args.get("phone_number", ""),
                args.get("aliases", ""),
            ) or "WhatsApp kişisi kaydedildi."

        return f"Bilinmeyen araç: {name}"

    except Exception as e:
        traceback.print_exc()
        return f"Hata: {e}"


async def handle_connection(ws):
    print("[Ajan] ✅ Sunucuya bağlandı — araç çağrıları bekleniyor.")
    loop = asyncio.get_event_loop()

    async def run_call(obj: dict):
        name = obj.get("name", "")
        args = obj.get("args", {}) or {}
        print(f"[Ajan] 🔧 {name} {args}")
        result = await loop.run_in_executor(None, lambda: execute_tool(name, args))
        print(f"[Ajan] 📤 {name} → {str(result)[:80]}")
        await ws.send(json.dumps(
            {"type": "tool_result", "id": obj.get("id", ""), "result": result},
            ensure_ascii=False,
        ))

    async for message in ws:
        try:
            obj = json.loads(message)
        except Exception:
            continue
        if obj.get("type") == "tool_call":
            # Eşzamanlı çağrılar birbirini bloklamasın
            asyncio.create_task(run_call(obj))


def load_token_from_config() -> str:
    # Baslatici token'i ortam degiskeniyle veriyor — dosya yazilamasa bile
    # ajan baglanabilsin diye ONCE buna bakiyoruz.
    import os

    env_token = str(os.environ.get("JARVIS_WEB_TOKEN", "") or "").strip()
    if env_token:
        return env_token

    # Sunucu token'i YAZILABILIR koke yaziyor. .exe'de WEB_DIR paketin
    # icidir (salt okunur) — oradan okursak token'i asla bulamayiz.
    candidates = []
    try:
        from app_paths import data_path

        candidates.append(data_path("jarvis_web", "web_config.json"))
    except Exception:
        pass
    candidates.append(WEB_DIR / "web_config.json")

    for path in candidates:
        try:
            cfg = json.loads(Path(path).read_text(encoding="utf-8"))
            token = str(cfg.get("token", "") or "")
            if token:
                return token
        except Exception:
            continue
    return ""


async def main():
    ap = argparse.ArgumentParser(description="DENİZ bilgisayar ajanı")
    ap.add_argument("--server", default="ws://127.0.0.1:8765",
                    help="Sunucu adresi, örn. wss://1.2.3.4:8765")
    ap.add_argument("--token", default="",
                    help="Erişim token'ı (boşsa web_config.json'dan okunur)")
    args = ap.parse_args()

    token = args.token or load_token_from_config()
    if not token:
        print("[Ajan] ❌ Token bulunamadı. --token verin veya önce sunucuyu çalıştırın.")
        return

    url = f"{args.server.rstrip('/')}/ws/agent?token={token}"

    ssl_ctx = None
    if url.startswith("wss://"):
        # Kendinden imzalı sertifika kabul edilir (v1)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

    print(f"[Ajan] 🔌 Bağlanılıyor: {args.server}")
    while True:
        try:
            async with websockets.connect(url, ssl=ssl_ctx,
                                          ping_interval=20) as ws:
                await handle_connection(ws)
        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"[Ajan] ⚠️  Bağlantı sorunu: {e} — {RECONNECT_DELAY:.0f}s sonra tekrar denenecek.")
        await asyncio.sleep(RECONNECT_DELAY)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[Ajan] 👋 Kapatıldı.")
