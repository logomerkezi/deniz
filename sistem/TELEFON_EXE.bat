@echo off
chcp 65001 >nul
title DENİZ - TELEFONDAN BAGLAN
rem ╔══════════════════════════════════════════════════════════╗
rem ║   DENİZ — TELEFONDAN BAGLAN (EXE surumu)                 ║
rem ║   Cift tikla → telefon icin adres + QR kod cikar.        ║
rem ╚══════════════════════════════════════════════════════════╝
rem
rem DENİZ.exe kendi icinde sunucuyu ve ajani baslatir; Python gerekmez.

if not exist "%~dp0JARVIS.exe" (
    echo.
    echo DENİZ.exe bulunamadi. Bu dosya DENİZ.exe ile ayni klasorde olmali.
    echo.
    pause
    exit /b 1
)

"%~dp0JARVIS.exe" --web

echo.
echo DENİZ Web durdu.
pause
