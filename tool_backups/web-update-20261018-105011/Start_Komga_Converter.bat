@echo off
cd /d "%~dp0"
python -c "from PIL import Image; from Crypto.Cipher import AES; import tkinter" >nul 2>&1
if errorlevel 1 (
  echo Python, Pillow, pycryptodome or Tkinter is missing. See README_Komga.md.
  pause
  exit /b 1
)
python komga_gui.py
if errorlevel 1 pause
