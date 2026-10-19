#!/usr/bin/env bash
# Сборка для Linux (нужен python с tkinter: на Arch `sudo pacman -S tk`)
set -e
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pyinstaller
.venv/bin/pyinstaller --noconfirm --onefile --windowed --name AdBot app.py
echo "Готово: dist/AdBot"
