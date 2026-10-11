@echo off
setlocal
rem Stops BOTH listeners, because the app is two processes.
rem   8080 - the model server (llama-server), which is what the old comment
rem           and every document that describes this script said it stopped
rem   8081 - the web UI (bonsai_web.py), which is all it actually used to kill
rem It only killed 8081, so on Windows the model server kept holding the VRAM
rem and the weights stayed resident after a "stop".
powershell -NoProfile -ExecutionPolicy Bypass -Command "$found = $false; foreach ($port in 8080, 8081) { $c = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue; if ($c) { $found = $true; $c | ForEach-Object { $name = (Get-Process -Id $_.OwningProcess -ErrorAction SilentlyContinue).ProcessName; Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue; Write-Host ('Stopped ' + $name + ' on port ' + $port + ' (PID ' + $_.OwningProcess + ').') } } }; if (-not $found) { Write-Host 'Bonsai is not running.' }"