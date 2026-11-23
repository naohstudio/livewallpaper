@echo off
rem Mở web quản lý wallpaper: http://127.0.0.1:5000
cd /d "%~dp0..\.."
start "" http://127.0.0.1:5000
.venv\Scripts\python tools\admin\server.py %*
