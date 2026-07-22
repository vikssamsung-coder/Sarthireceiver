@echo off
REM ============================================================
REM  Push the Sarthi Dump Processor app to GitHub.
REM  Run this from the app folder (double-click, or from a prompt).
REM  Repo: https://github.com/vikssamsung-coder/Sarthireceiver.git
REM ============================================================
setlocal
cd /d "%~dp0"

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed. Get it from https://git-scm.com/download/win
  pause & exit /b 1
)

REM keep caches and local data out of the repo
if not exist .gitignore (
  > .gitignore echo __pycache__/
  >>.gitignore echo *.pyc
  >>.gitignore echo *.sqlite3
  >>.gitignore echo .streamlit/secrets.toml
)

if not exist ".git" (
  git init
  git branch -M main
)

REM point origin at the repo (reset it so re-runs are safe)
git remote remove origin 2>nul
git remote add origin https://github.com/vikssamsung-coder/Sarthireceiver.git

git add -A
git commit -m "Update %date% %time%"
if errorlevel 1 echo (nothing new to commit)

git push -u origin main
echo.
echo ============================================================
echo Done. If it asks for a password, paste a GitHub Personal
echo Access Token (Settings - Developer settings - Tokens) — not
echo your account password.
echo ============================================================
pause
