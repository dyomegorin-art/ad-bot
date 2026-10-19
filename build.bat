@echo off
rem Сборка AdBot.exe (нужен установленный Python 3.10+ с tkinter)
python -m venv .venv || exit /b 1
call .venv\Scripts\activate.bat
pip install -r requirements.txt pyinstaller || exit /b 1
pyinstaller --noconfirm --onefile --windowed --name AdBot app.py || exit /b 1
echo.
echo Готово: dist\AdBot.exe
