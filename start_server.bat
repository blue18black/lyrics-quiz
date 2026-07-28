@echo off
cd /d "%~dp0"
echo Starting Flask server...
echo Open http://127.0.0.1:5050 in your browser.
echo Press Ctrl+C in this window to stop the server.
python app.py
pause
