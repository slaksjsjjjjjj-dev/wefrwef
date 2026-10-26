@echo off
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
  echo Execute INSTALAR_E_TESTAR.bat primeiro.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
set /p RUN_DATE=Data UTC YYYYMMDD (vazio = automatico): 
set /p RUN_CYCLE=Ciclo UTC 00, 06, 12 ou 18 (vazio = automatico): 
set /p FORECAST_HOURS=Horas CSV (vazio = 0,6,...,168): 
python -m sideral_multimodels run --date "%RUN_DATE%" --cycle "%RUN_CYCLE%" --hours "%FORECAST_HOURS%" --raw-dir data/raw --output-dir output
if errorlevel 1 echo Falhou. Leia a mensagem e README.md.
pause
