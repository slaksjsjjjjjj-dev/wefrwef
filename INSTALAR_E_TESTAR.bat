@echo off
setlocal
cd /d "%~dp0"
py -3.11 -m venv .venv
if errorlevel 1 goto erro
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
if errorlevel 1 goto erro
python -m pip install -e ".[test]"
if errorlevel 1 goto erro
python -m sideral_multimodels doctor
python -m pytest -q
if errorlevel 1 goto erro
echo.
echo Instalacao/testes concluidos. Revise README.md antes da primeira rodada real.
pause
exit /b 0
:erro
echo Ocorreu um erro. Consulte a mensagem acima e README.md.
pause
exit /b 1
