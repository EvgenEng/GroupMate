@echo off
chcp 65001 >nul
rem Запуск на Windows без Docker: двойной клик по файлу.
cd /d "%~dp0"
if not exist .venv (
    echo Создаю виртуальное окружение...
    python -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
)
if not exist .env (
    copy .env.example .env
    echo Заполните файл .env и запустите run.bat ещё раз.
    notepad .env
    exit /b
)
.venv\Scripts\python main.py
pause
