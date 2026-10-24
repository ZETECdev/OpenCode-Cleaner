@echo off
chcp 65001 >nul
cd /d "%~dp0"
title OpenCode Cleaner

echo.
echo  Cleanup assistant for the OpenCode data folder.
echo  It asks what to clean and shows a summary before touching anything.
echo.
echo  IMPORTANT: close OpenCode completely before continuing
echo             (the desktop app and any 'bun' processes too).
echo.

python clean_opencode.py --interactive
