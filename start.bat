@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 音乐工具台

echo.
echo   音乐工具台  ·  Music Desk
echo   -----------------------------
echo   正在启动本地服务...
echo.

start "" http://127.0.0.1:8899/
python server.py --port 8899

echo.
echo   服务已退出。按任意键关闭窗口。
pause >nul