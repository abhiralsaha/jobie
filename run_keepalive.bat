@echo off
title Jobie Keep-Alive Agent
echo ============================================================
echo  Jobie Keep-Alive Agent
echo  Pinging https://jobie-gu7t.onrender.com/ every 14 minutes
echo  Close this window to stop the agent.
echo ============================================================
cd /d "%~dp0"
python keepalive_agent.py
pause
