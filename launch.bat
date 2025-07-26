@echo off
chcp 65001 > nul
cd /d "%~dp0"
start "WiFi-Share Flask Server" cmd /k "python app.py flask"
start "WiFi-Share WebSocket Server" cmd /k "python app.py websocket"
exit