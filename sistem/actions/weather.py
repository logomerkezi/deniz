"""
Basit hava durumu özeti — uzaktaki bir servis üzerinden çalışır.
Alp Ünlü tarafından yapılmıştır — @alppunlu
"""

from __future__ import annotations

import os
import requests

# wttr.in servisinden en sık dönen İngilizce durum metinlerinin Türkçe karşılıkları
WEATHER_TRANSLATIONS = {
    "sunny": "GÜNEŞLİ",
    "clear": "AÇIK",
    "partly cloudy": "PARÇALI BULUTLU",
    "cloudy": "BULUTLU",
    "overcast": "ÇOK BULUTLU (KAPALI)",
    "mist": "PUSLU",
    "patchy rain nearby": "YER YER YAĞMURLU",
    "patchy light rain": "YER YER HAFİF YAĞMURLU",
    "light rain": "HAFİF YAĞMURLU",
    "moderate rain": "ORTA ŞİDDETTE YAĞMURLU",
    "heavy rain": "ŞİDDETLİ YAĞMURLU",
    "patchy snow nearby": "YER YER KARLI",
    "light snow": "HAFİF KARLI",
    "heavy snow": "YOĞUN KARLI",
    "thundery outbreaks possible": "GÖK GÜRÜLTÜLÜ SAĞNAK İHTİMALİ",
    "fog": "SİSLİ",
    "freezing fog": "DONDURUCU SİSLİ",
}


def get_weather_summary(location: str | None = None) -> str:
    target = (location or os.environ.get("JARVIS_WEATHER_LOCATION") or "Istanbul").strip()
    try:
        response = requests.get(
            f"https://wttr.in/{target}",
            params={"format": "j1"},
            timeout=10,
            headers={"User-Agent": "JARVIS macOS"},
        )
        response.raise_for_status()
        payload = response.json()
        current = (payload.get("current_condition") or [{}])[0]
        temp_c = current.get("temp_C")
        feels_like = current.get("FeelsLikeC")
        
        # İngilizce metni alıp küçük harfe çeviriyoruz
        raw_desc = ((current.get("weatherDesc") or [{}])[0]).get("value", "").strip().lower()
        # Sözlükte varsa Türkçe karşılığını, yoksa orijinal metni kullanıyoruz
        weather_desc = WEATHER_TRANSLATIONS.get(raw_desc, raw_desc)

        humidity = current.get("humidity")

        parts = []
        if temp_c:
            parts.append(f"{temp_c} DERECE")
        if weather_desc:
            parts.append(weather_desc)
        if feels_like and feels_like != temp_c:
            parts.append(f"HİSSEDİLEN {feels_like} DERECE")
        if humidity:
            parts.append(f"NEM YÜZDE {humidity}")

        if not parts:
            return "HAVA DURUMU BİLGİSİ ALINAMADI."

        return f"{target} İÇİN HAVA DURUMU: " + ", ".join(parts) + "."
    except Exception:
        return "HAVA DURUMU ŞUAN BİLGİSİ ALINAMADI."