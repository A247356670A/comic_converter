@echo off
cd /d "%~dp0"
python -c "from PIL import Image; import tkinter" >nul 2>&1
if errorlevel 1 (
  echo Python, Pillow or Tkinter is missing. See README_Komga.md.
  pause
  exit /b 1
)
python komga_gui.py
if errorlevel 1 pause
