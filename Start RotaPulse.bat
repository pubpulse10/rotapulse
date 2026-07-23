@echo off
cd /d "%~dp0"
echo Starting RotaPulse...
echo.
echo Once it says "Running on http://0.0.0.0:5053", open your browser to:
echo   http://localhost:5053/dev-login?pub_id=0
echo.
echo Leave this window open while you use the app. Close it to stop the server.
echo.
".venv\Scripts\python.exe" run.py
pause
