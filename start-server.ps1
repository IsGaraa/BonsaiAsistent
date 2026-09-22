$ErrorActionPreference = 'Stop'
$exe = Join-Path $env:LOCALAPPDATA "Programs\prism-llama\llama-server.exe"
$model = Join-Path $PSScriptRoot 'Ternary-Bonsai-2-27B-PQ2_0.gguf'
$logOut = Join-Path $PSScriptRoot 'bonsai-server.out.log'
$logErr = Join-Path $PSScriptRoot 'bonsai-server.err.log'

if (-not (Test-Path -LiteralPath $exe)) {
    Write-Host "llama-server not found at: $exe"
    exit 1
}
if (-not (Test-Path -LiteralPath $model)) {
    Write-Host "Model weights not found at: $model"
    exit 1
}

$args = @(
    '-m', $model,
    '--mmproj', (Join-Path $PSScriptRoot 'Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf'),
    '--alias', 'bonsai2',
    '--port', '8080',
    '--ctx-size', '32768',
    '-ngl', '99',
    '--flash-attn', 'on',
    '--temp', '1.0',
    '--top-p', '0.95',
    '--top-k', '20'
)

Write-Host 'Starting Bonsai 2 server...'
Start-Process -FilePath $exe -ArgumentList $args -WindowStyle Hidden `
    -RedirectStandardOutput $logOut -RedirectStandardError $logErr

$ready = $false
for ($i = 0; $i -lt 60; $i++) {
    Start-Sleep -Seconds 2
    try {
        $h = Invoke-RestMethod 'http://127.0.0.1:8080/health' -TimeoutSec 3
        if ($h.status -eq 'ok') { $ready = $true; break }
    } catch { }
}

if ($ready) {
    Write-Host ''
    Write-Host 'Bonsai 2 is READY.'
    Write-Host '  OpenAI-compatible API : http://127.0.0.1:8080/v1'
    Write-Host '  Model id               : bonsai2'
    Write-Host '  Run in opencode        : opencode --model llama.cpp/bonsai2'
    exit 0
} else {
    Write-Host 'Bonsai failed to become ready - check bonsai-server.err.log'
    exit 1
}