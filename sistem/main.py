#!/usr/bin/env python3
"""
DENİZ macOS — Gercek zamanli sesli yardimci cekirdegi
macOS ortamina uyarlanmis calisma akisi
"""

import asyncio
import datetime
import threading
import traceback
import os
import subprocess
import sys
import time
import re
from pathlib import Path

from actions.platform_utils import (
    IS_WIN,
    acquire_single_instance,
    attach_parent_console,
    configure_console_output,
    focus_window,
)

# Konsol ciktisini pythonw (konsolsuz) ve Turkce kod sayfasi icin guvenli yap.
# Diger importlardan once calismali — bazi moduller import aninda yazdirir.
configure_console_output()

import pyaudio
import psutil
from google import genai
from google.genai import types

from app_config import get_app_config_value
from ui import JarvisUI
from memory.memory_manager import load_memory, update_memory, delete_memory, format_memory_for_prompt
from actions.open_app import open_app
from actions.sys_info  import sys_info
from actions.calendar import get_calendar_events, add_calendar_event, delete_calendar_event
from actions.reminders import get_reminders, add_reminder
from actions.browser   import browser_control
from actions.shell     import shell_run
from actions.whatsapp  import send_whatsapp_message, save_whatsapp_contact
from actions.media     import play_media, control_media
from actions.weather   import get_weather_summary
from actions.screen_vision import analyze_screen
from actions.youtube_stats import get_youtube_channel_report

# ── Paths ───────────────────────────────────────────────────────────────────
from app_paths import resource_path

BASE_DIR        = Path(__file__).resolve().parent
PROMPT_PATH     = resource_path("core", "prompt.txt")


# ── WebcamStreamer ──────────────────────────────────────────────────────────
class WebcamStreamer:
    """
    Webcam'dan sürekli kare çeker ve en güncel JPEG'i bellekte tutar.
    Queue yerine tek bir 'latest frame' yaklaşımı — eski kare birikimi olmaz.
    """

    JPEG_QUALITY = 72
    MAX_DIM      = 640
    WARMUP       = 6
    # Gecici okuma hatalari normaldir (USB kamera, guc tasarrufu, baska
    # uygulamanin kisa erisimi). Tek hatada akisi kapatmak yerine tolere et.
    MAX_READ_FAILURES = 30
    OPEN_ATTEMPTS = 3
    CAPTURE_INTERVAL = 0.05   # ~20 FPS yakalama (CPU dostu)

    def __init__(self):
        self._latest: bytes | None = None
        self._lock   = threading.Lock()
        self._active = False
        self._thread: threading.Thread | None = None

    @property
    def is_active(self) -> bool:
        return self._active

    def get_latest_frame(self) -> bytes | None:
        """Thread-safe, her zaman en güncel kareyi döner."""
        with self._lock:
            return self._latest

    def start(self) -> str:
        with self._lock:
            if self._active:
                return "already_active"

        # Onceki cekim thread'i kamerayi serbest birakana kadar BEKLE.
        # Beklemezsek yeni VideoCapture "cihaz mesgul" diye acilamiyordu ve
        # kamera kapatilip acildiktan sonra bir daha calismiyordu.
        previous = self._thread
        if previous and previous.is_alive():
            previous.join(timeout=3.0)

        with self._lock:
            self._active = True
            self._latest = None
        t = threading.Thread(target=self._run, daemon=True)
        self._thread = t
        t.start()
        return "ok"

    def stop(self):
        with self._lock:
            self._active = False
            self._latest = None

    def _open_capture(self, cv2):
        """
        Kamerayi acar. Windows'ta DirectShow varsayilandan belirgin hizli
        acilir (~1 sn). Kamera bir onceki oturumdan henuz serbest kalmamis
        olabilecegi icin birkac kez denenir.
        """
        backends = []
        if IS_WIN:
            backends.append(("DirectShow", lambda: cv2.VideoCapture(0, cv2.CAP_DSHOW)))
        backends.append(("varsayilan", lambda: cv2.VideoCapture(0)))

        for attempt in range(1, self.OPEN_ATTEMPTS + 1):
            for label, factory in backends:
                try:
                    cap = factory()
                except Exception:
                    continue
                if cap is not None and cap.isOpened():
                    return cap
                try:
                    if cap is not None:
                        cap.release()
                except Exception:
                    pass
            if attempt < self.OPEN_ATTEMPTS:
                # Kamerayi baska bir surec/onceki thread hala tutuyor olabilir
                time.sleep(0.6)
        return None

    def _run(self):
        try:
            import cv2
        except ImportError:
            print("[Webcam] opencv-python yüklü değil.")
            with self._lock:
                self._active = False
            return

        cap = self._open_capture(cv2)
        if cap is None:
            print("[Webcam] Kamera açılamadı.")
            with self._lock:
                self._active = False
            return

        # Isınma — sensörün otomatik pozlaması oturuncaya kadar bekle
        for _ in range(self.WARMUP):
            cap.read()

        enc_params = [cv2.IMWRITE_JPEG_QUALITY, self.JPEG_QUALITY]
        read_failures = 0

        try:
            while True:
                with self._lock:
                    if not self._active:
                        break

                ret, frame = cap.read()
                if not ret or frame is None:
                    # Tek basarisiz okuma akisi oldurmesin — bir sure dene
                    read_failures += 1
                    if read_failures >= self.MAX_READ_FAILURES:
                        print("[Webcam] Kameradan kare alinamiyor, akis durduruldu.")
                        break
                    time.sleep(0.05)
                    continue
                read_failures = 0

                h, w = frame.shape[:2]
                if max(h, w) > self.MAX_DIM:
                    s = self.MAX_DIM / max(h, w)
                    frame = cv2.resize(frame, (int(w * s), int(h * s)))

                frame = cv2.flip(frame, 1)  # yatay ayna — hem UI hem AI tutarlı
                ok, buf = cv2.imencode(".jpg", frame, enc_params)
                if ok:
                    with self._lock:
                        self._latest = buf.tobytes()

                # ~20 FPS yakala. Onceden 33 FPS'ti ama her kare JPEG'e
                # kodlandigi icin bosuna CPU yiyordu; UI onizlemesi icin
                # 20 FPS gozle ayirt edilemeyecek kadar akici.
                time.sleep(self.CAPTURE_INTERVAL)
        finally:
            cap.release()
            with self._lock:
                self._active = False
                self._latest = None
            print("[Webcam] Kamera serbest bırakıldı.")


CONTROL_TOKEN_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# ── Model ───────────────────────────────────────────────────────────────────
LIVE_MODEL = "models/gemini-2.5-flash-native-audio-latest"

# Webcam acikken modele kac saniyede bir kare gonderilecek.
# 0.5 sn = akici "canli goruyor" hissi. Bilgisayar zorlanirsa (eski makine,
# yuksek CPU) bu degeri 1.0-1.5'e cikarmak yeterlidir.
WEBCAM_SEND_INTERVAL = 0.5

# ── Audio ───────────────────────────────────────────────────────────────────
FORMAT           = pyaudio.paInt16
CHANNELS         = 1
SEND_SAMPLE_RATE = 16000
RECV_SAMPLE_RATE = 24000
CHUNK_SIZE       = 1024
pya              = pyaudio.PyAudio()

# ── Tool tanımları — paylaşılan modülden ────────────────────────────────────
from tool_defs import TOOL_DECLARATIONS


def get_api_key() -> str:
    return str(get_app_config_value("gemini_api_key", "") or "")


def load_system_prompt() -> str:
    # Prompt tek dosyada tutulur; Windows'a uyarlamayi prompt_loader yapar.
    from prompt_loader import load_system_prompt as _load

    return _load()


class JarvisLive:
    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self._music_proc    = None
        self._webcam_streamer = WebcamStreamer()

        self.ui.on_text_command  = self._on_text_command
        self.ui.on_pause_toggle  = self._on_pause_toggle
        self.ui.on_effects_state_change = self._on_effects_state_change
        self.ui.on_webcam_toggle = self._on_webcam_toggle_ui
        self._paused             = False

    def _on_pause_toggle(self, paused: bool):
        self._paused = paused
        if paused:
            self._stop_music()

    def _on_effects_state_change(self, enabled: bool):
        if not enabled:
            self._stop_music()

    def _on_webcam_toggle_ui(self, activate: bool):
        if activate:
            status = self._webcam_streamer.start()
            self.ui.set_webcam_active(status == "ok" or status == "already_active")
        else:
            self._webcam_streamer.stop()
            self.ui.set_webcam_active(False)

    def _focus_ui_section_for_tool(self, tool_name: str, args: dict):
        if tool_name == "sys_info":
            query = str(args.get("query", "")).strip().lower()
            if query in {"time", "saat", "zaman", "date", "tarih"}:
                self.ui.focus_panel("time", duration_ms=5200)
            else:
                self.ui.focus_panel("system", duration_ms=5200)
        elif tool_name == "get_weather":
            self.ui.focus_panel("weather", duration_ms=5600)

    def _on_text_command(self, text: str):
        if self._paused:
            return
        self.ui.write_log(f"Siz: {text}")
        if not self._loop or not self.session:
            self.ui.write_log("ERR: DENİZ bağlantısı henüz hazır değil.")
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    async def _interrupt_audio(self):
        try:
            if self.audio_in_queue:
                while not self.audio_in_queue.empty():
                    try:
                        self.audio_in_queue.get_nowait()
                    except Exception:
                        break
            if self.session:
                await self.session.send_realtime_input(audio_stream_end=True)
            self.set_speaking(False)
        except Exception:
            pass

    def _stop_music(self):
        proc = self._music_proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        self._music_proc = None

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        else:
            self.ui.set_state("LISTENING")

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.ui.write_debug(f"{tool_name}: {short}", level="ERROR")
        self.ui.set_state("ERROR")

    @staticmethod
    def _result_looks_like_error(result) -> bool:
        text = str(result or "").strip().lower()
        if not text:
            return False
        error_markers = (
            "hata",
            "error",
            "alinamadi",
            "alınamadı",
            "bulunamadi",
            "bulunamadı",
            "acilamadi",
            "açılamadı",
            "tamamlanamadi",
            "tamamlanamadı",
            "gecersiz",
            "geçersiz",
            "izin gerekiyor",
            "izin gerekli",
            "baglanti",
            "bağlantı",
            "gerekli.",
        )
        return any(marker in text for marker in error_markers)

    @staticmethod
    def _should_play_success_sfx(tool_name: str, args: dict, result) -> bool:
        action_tools = {
            "open_app",
            "add_calendar_event",
            "add_reminder",
            "delete_calendar_event",
            "remove_calendar_event",
        }
        if tool_name in action_tools:
            return True

        if tool_name == "send_whatsapp_message":
            text = str(result or "").lower()
            if bool(args.get("send_now", False)):
                return "gönderildi" in text or "gonderildi" in text
            return False

        return False

    @staticmethod
    def _clean_transcript_text(text: str) -> tuple[str, bool]:
        raw = str(text or "")
        had_noise = False
        if CONTROL_TOKEN_RE.search(raw):
            had_noise = True
            raw = CONTROL_TOKEN_RE.sub(" ", raw)
        cleaned = []
        for ch in raw:
            if ch in "\n\r\t" or ord(ch) >= 32:
                cleaned.append(ch)
            else:
                had_noise = True
        normalized = " ".join("".join(cleaned).split())
        return normalized.strip(), had_noise

    def _build_config(self) -> types.LiveConnectConfig:
        import datetime
        memory  = load_memory()
        mem_str = format_memory_for_prompt(memory)
        sys_p   = load_system_prompt()
        now = datetime.datetime.now()
        time_ctx = f"[ŞU ANKİ ZAMAN]\n{now.strftime('%A, %d %B %Y — %H:%M')}\n\n"

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str + "\n\n")  # <--- Bu satır TAB ile içeride olmalı
        parts.append(sys_p)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=str(get_app_config_value("voice", "Charon") or "Charon")
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})
        print(f"[DENİZ] 🔧 {name} {args}")
        self.ui.set_state("THINKING")

        loop   = asyncio.get_event_loop()
        result = "Tamam."
        had_exception = False

        try:
            if name == "save_memory":
                cat = str(args.get("category", "") or "notes").strip() or "notes"
                key = str(args.get("key", "") or "").strip()
                val = str(args.get("value", "") or "").strip()
                if not key or not val:
                    # Eskiden bu durumda da "ok" donuyordu; model kaydettigini
                    # saniyor ama hicbir sey yazilmiyordu. Artik durumu bildiriyoruz.
                    result = (
                        "Hafizaya yazilamadi: key ve value dolu olmali. "
                        "Ornek: category='identity', key='name', value='Alp'"
                    )
                else:
                    update_memory({cat: {key: {"value": val}}})
                    print(f"[Memory] 💾 {cat}/{key} = {val}")
                    result = f"Hafizaya kaydedildi: {cat}/{key} = {val}"

            elif name == "delete_memory":
                result = delete_memory(
                    args.get("category", ""),
                    args.get("key", ""),
                    args.get("match_text", ""),
                )

            elif name == "open_app":
                r = await loop.run_in_executor(
                    None, lambda: open_app(args.get("app_name", "")))
                result = r or f"{args.get('app_name')} açıldı."

            elif name == "sys_info":
                self._focus_ui_section_for_tool(name, args)
                r = await loop.run_in_executor(
                    None, lambda: sys_info(args.get("query", "all")))
                result = r or "Bilgi alındı."

            elif name == "get_weather":
                self._focus_ui_section_for_tool(name, args)
                r = await loop.run_in_executor(
                    None, lambda: get_weather_summary(args.get("location") or None))
                result = r or "Hava durumu bilgisi alindi."

            elif name == "get_calendar_events":
                r = await loop.run_in_executor(
                    None,
                    lambda: get_calendar_events(
                        args.get("query", "today"),
                        int(args.get("limit", 6) or 6),
                    ),
                )
                result = r or "Takvim bilgisi alindi."

            elif name == "add_calendar_event":
                r = await loop.run_in_executor(
                    None,
                    lambda: add_calendar_event(
                        args.get("title", ""),
                        args.get("start_iso", ""),
                        args.get("end_iso", ""),
                        args.get("notes", ""),
                        args.get("location", ""),
                        args.get("calendar_name", ""),
                        bool(args.get("all_day", False)),
                    ),
                )
                result = r or "Takvim etkinligi eklendi."

            elif name == "delete_calendar_event":
                r = await loop.run_in_executor(
                    None,
                    lambda: delete_calendar_event(
                        args.get("title", ""),
                        args.get("start_iso", ""),
                        args.get("calendar_name", ""),
                        bool(args.get("delete_all_matches", False)),
                    ),
                )
                result = r or "Takvim etkinligi silindi."

            elif name == "get_reminders":
                r = await loop.run_in_executor(
                    None,
                    lambda: get_reminders(
                        args.get("query", "upcoming"),
                        int(args.get("limit", 8) or 8),
                        args.get("list_name", ""),
                    ),
                )
                result = r or "Animsatici bilgisi alindi."

            elif name == "add_reminder":
                r = await loop.run_in_executor(
                    None,
                    lambda: add_reminder(
                        args.get("title", ""),
                        args.get("due_iso", ""),
                        args.get("notes", ""),
                        args.get("list_name", ""),
                        args.get("priority", ""),
                        bool(args.get("all_day", False)),
                    ),
                )
                result = r or "Animsatici eklendi."

            elif name == "browser_control":
                r = await loop.run_in_executor(
                    None, lambda: browser_control(
                        args.get("action"),
                        args.get("url"),
                        args.get("query")
                    ))
                result = r or "Tamam."

            elif name == "shell_run":
                r = await loop.run_in_executor(
                    None, lambda: shell_run(args.get("command", "")))
                result = r or "Komut çalıştırıldı."

            elif name == "toggle_webcam":
                action = str(args.get("action", "start")).strip().lower()
                if action == "start":
                    status = self._webcam_streamer.start()
                    if status == "ok":
                        self.ui.set_webcam_active(True)
                        result = (
                            "Webcam akışı başlatıldı. "
                            "Artık kameranı görüyorum — dilediğin zaman soru sorabilirsin."
                        )
                    elif status == "already_active":
                        result = "Webcam zaten açık, görüntü alıyorum."
                    else:
                        result = "Webcam başlatılamadı: opencv-python yüklü değil."
                else:
                    self._webcam_streamer.stop()
                    self.ui.set_webcam_active(False)
                    result = "Webcam akışı durduruldu."

            elif name == "play_media":
                r = await loop.run_in_executor(
                    None,
                    lambda: play_media(
                        args.get("query", ""),
                        args.get("provider", "auto"),
                        bool(args.get("autoplay", True)),
                    ),
                )
                result = r or "Medya oynatma başlatıldı."

            elif name == "control_media":
                r = await loop.run_in_executor(
                    None, lambda: control_media(args.get("action", "pause")))
                result = r or "Medya komutu gonderildi."

            elif name == "get_youtube_channel_report":
                r = await loop.run_in_executor(
                    None,
                    lambda: get_youtube_channel_report(
                        args.get("query", "overview"),
                        args.get("handle", ""),
                        int(args.get("video_limit", 6) or 6),
                    ),
                )
                result = r or "YouTube kanal raporu alindi."

            elif name == "analyze_screen":
                r = await loop.run_in_executor(
                    None,
                    lambda: analyze_screen(
                        args.get("query", "Ekranda ne var?"),
                        args.get("target", "active_window"),
                    ),
                )
                result = r or "Ekran analizi tamamlandi."

            elif name == "send_whatsapp_message":
                r = await loop.run_in_executor(
                    None,
                    lambda: send_whatsapp_message(
                        args.get("message", ""),
                        args.get("phone_number", ""),
                        args.get("recipient_name", ""),
                        bool(args.get("send_now", False)),
                        args.get("app_target", "auto"),
                    ),
                )
                result = r or "WhatsApp işlemi tamamlandı."

            elif name == "save_whatsapp_contact":
                r = await loop.run_in_executor(
                    None,
                    lambda: save_whatsapp_contact(
                        args.get("display_name", ""),
                        args.get("phone_number", ""),
                        args.get("aliases", ""),
                    ),
                )
                result = r or "WhatsApp kişisi kaydedildi."

            else:
                result = f"Bilinmeyen araç: {name}"

        except Exception as e:
            result = f"Hata: {e}"
            had_exception = True
            traceback.print_exc()
            self.speak_error(name, e)

        tool_failed = self._result_looks_like_error(result)
        if tool_failed:
            if not had_exception:
                self.ui.set_state("ERROR")
        elif self._should_play_success_sfx(name, args, result):
            self.ui.play_success_sfx()

        if not tool_failed and not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[DENİZ] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _stream_webcam_frames(self):
        """
        Webcam aktifken her 1.5s'de EN GÜNCEL kareyi session'a gönderir.
        Queue'suz 'latest frame' yaklaşımı: model hep şimdiki görüntüyü görür.
        """
        _last_sent: bytes | None = None
        while True:
            if not self._webcam_streamer.is_active:
                await asyncio.sleep(0.2)
                continue

            jpeg = self._webcam_streamer.get_latest_frame()
            if jpeg is None or jpeg is _last_sent:
                await asyncio.sleep(0.2)
                continue

            _last_sent = jpeg
            try:
                await self.session.send_realtime_input(
                    media={"data": jpeg, "mime_type": "image/jpeg"}
                )
            except Exception as e:
                print(f"[Webcam] Frame gönderilemedi: {e}")

            await asyncio.sleep(WEBCAM_SEND_INTERVAL)

    async def _update_ui_webcam_preview(self):
        """UI önizlemesini ~24 FPS günceller. AI akışından bağımsız."""
        frame_interval = 1.0 / 24.0   # ~0.0417 sn → 24 FPS
        while True:
            if self._webcam_streamer.is_active:
                jpeg = self._webcam_streamer.get_latest_frame()
                if jpeg:
                    self.ui.update_webcam_preview(jpeg)
            await asyncio.sleep(frame_interval)

    async def _listen_audio(self):
        print("[DENİZ] 🎤 Mikrofon başladı")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT, channels=CHANNELS,
            rate=SEND_SAMPLE_RATE, input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
        try:
            while True:
                data = await asyncio.to_thread(
                    stream.read, CHUNK_SIZE, exception_on_overflow=False)
                with self._speaking_lock:
                    jarvis_speaking = self._is_speaking
                if not jarvis_speaking and not self.ui.muted and not self._paused:
                    await self.out_queue.put({"data": data, "mime_type": "audio/pcm"})
        except Exception as e:
            print(f"[DENİZ] ❌ Mikrofon: {e}")
            raise
        finally:
            stream.close()

    async def _receive_audio(self):
        print("[DENİZ] 👂 Alım başladı")
        out_buf, in_buf = [], []
        output_noise = False
        output_noise_samples = []
        try:
            while True:
                async for response in self.session.receive():
                    if response.data:
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            self.set_speaking(True)
                            raw_txt = sc.output_transcription.text.strip()
                            if raw_txt:
                                txt, had_noise = self._clean_transcript_text(raw_txt)
                                if had_noise:
                                    output_noise = True
                                    if len(output_noise_samples) < 4:
                                        output_noise_samples.append(raw_txt)
                                if txt:
                                    out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = sc.input_transcription.text.strip()
                            if txt:
                                in_buf.append(txt)
                                self.ui.mark_user_activity(True)

                        if sc.turn_complete:
                            self.set_speaking(False)

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"Siz: {full_in}")
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"DENİZ: {full_out}")
                                if output_noise_samples:
                                    self.ui.write_debug(
                                        "Kısmen filtrelenen ses transcripti: " + " | ".join(output_noise_samples),
                                        level="WARN",
                                    )
                            elif output_noise:
                                self.ui.write_log("ERR: DENİZ sesli yanıtını çözümlerken bir hata oluştu.")
                                if output_noise_samples:
                                    self.ui.write_debug(
                                        "Filtrelenen ham transcript: " + " | ".join(output_noise_samples),
                                        level="WARN",
                                    )
                                self.ui.set_state("ERROR")
                            out_buf = []
                            output_noise = False
                            output_noise_samples = []

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[DENİZ] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses)

        except Exception as e:
            print(f"[DENİZ] ❌ Alım: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[DENİZ] 🔊 Ses çalma başladı")
        stream = await asyncio.to_thread(
            pya.open,
            format=FORMAT, channels=CHANNELS,
            rate=RECV_SAMPLE_RATE, output=True,
        )
        try:
            while True:
                chunk = await self.audio_in_queue.get()
                self.set_speaking(True)
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[DENİZ] ❌ Ses: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.close()

    async def run(self):
        connect_attempts = 0
        while True:
            # Duraklatılmışsa bağlanma, bekle
            if self._paused:
                await asyncio.sleep(1)
                continue

            try:
                # Client'ı her bağlanışta yeniden oluştur ve anahtarı tazeden oku.
                # Böylece yeni girilen API anahtarı anında geçerli olur; ilk
                # deneme başarısız olsa bile otomatik tekrar (3sn) kendini onarır.
                client = genai.Client(
                    api_key=get_api_key(),
                    http_options={"api_version": "v1alpha"}
                )
                print("[DENİZ] 🔌 Bağlanıyor...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)

                    print("[DENİZ] ✅ Bağlandı.")
                    connect_attempts = 0          # başarılı bağlantı → sayaç sıfırla
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: DENİZ hazır. Dinliyorum...")

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._stream_webcam_frames())
                    tg.create_task(self._update_ui_webcam_preview())

            except Exception as e:
                print(f"[DENİZ] ⚠️ {e}")
                traceback.print_exc()
                self.set_speaking(False)
                # Webcam akışını durdur — yeni session'da yeniden başlayacak
                if self._webcam_streamer.is_active:
                    self._webcam_streamer.stop()
                    self.ui.set_webcam_active(False)

                connect_attempts += 1
                # İlk birkaç deneme sessiz: yeni girilen API anahtarı Google
                # tarafında saniyeler içinde aktifleşebilir. Kullanıcıya hemen
                # "hatalı anahtar" göstermeyip kısa aralıkla otomatik tekrar dene.
                if connect_attempts <= 3:
                    self.ui.set_state("INITIALISING")
                    print(f"[DENİZ] 🔄 Bağlanmayı tekrar deniyor ({connect_attempts}/3)...")
                    await asyncio.sleep(2)
                else:
                    self.ui.write_log(
                        f"ERR: DENİZ baglanamiyor — API anahtarini ve internet "
                        f"baglantisini kontrol et. ({e})"
                    )
                    self.ui.set_state("ERROR")
                    print("[DENİZ] 🔄 5 saniyede yeniden bağlanıyor...")
                    await asyncio.sleep(5)


def run_selftest() -> int:
    """
    Kurulum tanisi: `DENİZ.exe --selftest`

    Arayuzu acmadan yollari, kritik modulleri ve temel araclari dener.
    Destek isterken kullaniciya "bunu calistirip ciktiyi gonder" demek icin.
    """
    import app_paths

    # Penceresiz .exe'de konsol yoktur → ciktiyi dosyaya da yaz.
    lines: list[str] = []

    def out(text=""):
        lines.append(str(text))
        print(text)

    out("DENİZ — kurulum tanisi\n")
    out(app_paths.describe())

    ok = bad = 0

    def check(label, fn):
        nonlocal ok, bad
        try:
            detail = fn()
            out(f"  [OK]   {label}" + (f" — {detail}" if detail else ""))
            ok += 1
        except Exception as exc:
            out(f"  [HATA] {label} — {type(exc).__name__}: {exc}")
            bad += 1

    out("\nKritik moduller:")
    for mod in ("win32com.client", "pythoncom", "win32gui", "cv2",
                "pyaudio", "mss", "psutil", "PIL.Image", "google.genai"):
        check(mod, lambda m=mod: __import__(m) and "")

    out("\nGomulu kaynaklar:")
    for rel in (("core", "prompt.txt"), ("Fonts", "Grift-Regular.ttf"), ("SFX", "HUD.mp3")):
        check("/".join(rel), lambda r=rel: "bulundu" if app_paths.resource_path(*r).exists()
              else (_ for _ in ()).throw(FileNotFoundError(app_paths.resource_path(*r))))

    out("\nYazma izni:")

    def _write_probe():
        p = app_paths.data_path("config", ".probe")
        p.write_text("ok", encoding="utf-8")
        p.unlink()
        return str(p.parent)

    check("veri klasorune yazilabiliyor", _write_probe)

    out("\nAyarlar:")
    check("API anahtari girilmis mi",
          lambda: "evet" if get_app_config_value("gemini_api_key", "") else "HAYIR (uygulamadan gir)")

    out("\nAraclar:")
    check("sys_info", lambda: sys_info("battery"))
    check("takvim", lambda: get_calendar_events("today"))
    check("PowerShell", lambda: shell_run("Write-Output tamam"))
    check("COM (kisayol/Outlook icin)", lambda: _com_probe())

    out(f"\nSONUC: {ok} basarili, {bad} hatali")

    try:
        log = app_paths.data_path("jarvis_tani.log")
        log.write_text("\n".join(lines), encoding="utf-8")
        print(f"\nRapor kaydedildi: {log}")
    except Exception:
        pass

    return 1 if bad else 0


def _com_probe() -> str:
    from actions.platform_utils import com_context

    with com_context():
        import win32com.client

        shell = win32com.client.Dispatch("WScript.Shell")
        return "WScript.Shell olusturuldu"


def _dispatch_web_modes():
    """
    Telefon/web modlari. .exe icinde "python server.py" calistirilamadigi icin
    DENİZ.exe kendini bu bayraklarla yeniden cagirir.
    """
    argv = sys.argv[1:]

    if "--web" in argv:
        # Penceresiz .exe'nin ciktisi hicbir yere gitmiyordu; TELEFON.bat'in
        # konsoluna baglan ki adres ve QR kodu gorunsun.
        attach_parent_console()
        from jarvis_web.launcher import run_orchestrator

        raise SystemExit(run_orchestrator())

    if "--web-server" in argv:
        # server.py kendi argparse'ini kullaniyor — kendi bayragimizi cikar
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--web-server"]
        from jarvis_web.server import main as server_main

        raise SystemExit(server_main() or 0)

    if "--web-agent" in argv:
        sys.argv = [sys.argv[0]] + [a for a in argv if a != "--web-agent"]
        from jarvis_web.agent import main as agent_main

        try:
            asyncio.run(agent_main())
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)


def main():
    if "--selftest" in sys.argv:
        raise SystemExit(run_selftest())

    _dispatch_web_modes()

    if os.environ.get("TERM_PROGRAM") == "vscode":
        print("[DENİZ] VS Code icinden baslatildi.")

    # Zaten calisan bir DENİZ varsa yenisini acma, mevcut pencereyi one getir.
    if not acquire_single_instance():
        print("[DENİZ] Zaten calisiyor — mevcut pencere one getiriliyor.")
        focus_window("DENİZ")
        return

    ui = JarvisUI()

    def runner():
        ui.wait_for_api_key()
        jarvis = JarvisLive(ui)
        try:
            asyncio.run(jarvis.run())
        except KeyboardInterrupt:
            print("\n🔴 Kapatılıyor...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()


if __name__ == "__main__":
    main()
