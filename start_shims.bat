@echo off
REM ISDO Lab C2 - start both mock APIs in their own windows (run from C:\Zensar_ISDO1)
cd /d "%~dp0"
set PY=labenv\Scripts\python.exe
if not exist %PY% set PY=python
start "ServiceNow Mock :5001" cmd /k %PY% mcp_server\snow_shim.py
start "Jira Mock :5002" cmd /k %PY% mcp_server\jira_shim.py
echo Both shims starting. Health: http://localhost:5001/health  and  http://localhost:5002/health
