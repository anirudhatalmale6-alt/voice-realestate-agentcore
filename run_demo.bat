@echo off
REM Double click this file on Windows. Nothing else needed.
REM No AWS account, no API key, no internet connection.

cd /d "%~dp0"

echo.
echo  Voice real estate agent, demo call
echo  ----------------------------------
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo  Python is not installed, or it is not on your PATH.
  echo.
  echo  Install it from https://www.python.org/downloads/
  echo  On the first screen of the installer, tick "Add python.exe to PATH".
  echo.
  pause
  exit /b 1
)

echo  Installing the two libraries this needs...
python -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 (
  echo.
  echo  The install failed. Copy everything above and send it to me.
  pause
  exit /b 1
)

echo  Done.
echo.
echo  === Checking everything works (57 checks) ===
echo.
python -m pytest tests -q
echo.
echo  === A phone call, start to finish ===
echo.
python demo_call.py

echo.
echo  Press any key to close.
pause >nul
