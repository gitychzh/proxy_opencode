@echo off
rem One-click launcher for the proxy_opencode LAN gateway.
rem Double-click this file. Stop with Ctrl+C or by closing the window.

chcp 65001 >nul
cd /d "%~dp0"

set HOST=0.0.0.0
set PORT=8791
set GATEWAY_API_KEYS=dev-local-key

echo ================================================
echo  proxy_opencode gateway
echo  Local : http://127.0.0.1:8791/v1
echo  LAN   : http://^<this-machine-IP^>:8791/v1
echo  APIkey: dev-local-key
echo  Model : ds41f_cus  (POST /v1/chat/completions | /v1/responses | /v1/messages)
echo ================================================
echo.

".venv\Scripts\python.exe" -m proxy_opencode

echo.
echo Gateway exited. Press any key to close.
pause >nul
