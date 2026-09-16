#!/bin/bash
# DENİZ — macOS Otomatik Başlatma KALDIR
# Kullanım: bash autostart_kaldir.sh

PLIST_DST="$HOME/Library/LaunchAgents/com.alp.jarvis.plist"

echo ""
echo "╔══════════════════════════════════════╗"
echo "║      DENİZ  Otomatik Başlatma        ║"
echo "║         Kaldırılıyor...              ║"
echo "╚══════════════════════════════════════╝"
echo ""

if [ ! -f "$PLIST_DST" ]; then
    echo "ℹ️  Kurulu değil, yapacak bir şey yok."
    exit 0
fi

launchctl unload "$PLIST_DST" 2>/dev/null && echo "✅ LaunchAgent durduruldu"
rm -f "$PLIST_DST" && echo "✅ Plist silindi"

echo ""
echo "✔️  DENİZ artık otomatik açılmayacak."
echo ""
