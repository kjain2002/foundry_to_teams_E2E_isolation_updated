# restart.ps1 — cleanly restart the Streamlit publisher on port 8501.
# Run this EVERY time after changing publisher.py / config.py / .env etc.
# It kills whatever holds 8501 (stale instances) so the browser always
# reconnects to the freshly-loaded code.
param([int]$Port = 8501)

Write-Host "Stopping anything on port $Port ..." -ForegroundColor Yellow
Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty OwningProcess -Unique |
    ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }

# Also clear any stray streamlit launcher processes.
Get-Process streamlit -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

$still = Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue
if ($still) { Write-Host "WARNING: port $Port still held by PID $($still.OwningProcess)" -ForegroundColor Red }
else { Write-Host "Port $Port is free." -ForegroundColor Green }

Set-Location $PSScriptRoot
Write-Host "Launching fresh Streamlit on $Port ..." -ForegroundColor Cyan
conda run -n <your-python-env> --no-capture-output python -m streamlit run app.py --server.headless true --server.port $Port
