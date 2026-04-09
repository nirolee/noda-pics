@echo off
title noda.pics Poller
cd /d "%~dp0"
echo Starting noda.pics Poller...
echo Make sure ComfyUI is running at http://127.0.0.1:8188
echo.
F:\Python3.12\python.exe poller.py
pause
