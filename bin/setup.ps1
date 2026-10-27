# Extras for Reel on Windows, run once after cloning it (the counterpart of setup.sh):
#   powershell -ExecutionPolicy Bypass -File bin\setup.ps1
# installs ffmpeg (Windows has no afconvert: ffmpeg decodes and encodes the audio),
# numpy (multi-mic recordings, cutting and splitting) and fetches DeepFilterNet into
# bin\ (denoised listening copies). Reel itself needs only Python 3: python transcribe.py
$ErrorActionPreference = "Continue"
Set-Location (Split-Path $PSScriptRoot)

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "python is missing: winget install Python.Python.3.13 (then open a new terminal)"
    exit 1
}
python -c "import sys; sys.exit(sys.version_info < (3, 9))"
if ($LASTEXITCODE -ne 0) { Write-Host "Reel needs Python 3.9 or newer; python is $(python --version)"; exit 1 }

Write-Host "== ffmpeg (audio decoding and encoding)"
if (Get-Command ffmpeg -ErrorAction SilentlyContinue) { Write-Host "   already there" }
else {
    winget install --id Gyan.FFmpeg -e --accept-source-agreements --accept-package-agreements
    Write-Host "   installed: open a new terminal before starting Reel, so it's on PATH"
}

Write-Host "== numpy (multi-mic recordings, cutting and splitting)"
python -c "import numpy" 2>$null
if ($LASTEXITCODE -eq 0) { Write-Host "   already there" }
else {
    python -m pip install --user numpy
    python -c "import numpy" 2>$null
    if ($LASTEXITCODE -ne 0) { Write-Host "!! numpy could not be installed - Reel runs, but multi-mic support is off until it is" }
}

Write-Host "== DeepFilterNet (denoised listening copies)"
if (Test-Path bin\deep-filter.exe) { Write-Host "   already there" }
else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File bin\fetch-deepfilter.ps1
    if ($LASTEXITCODE -ne 0) { Write-Host "   (not fetched - the denoised copies are optional)" }
}

Write-Host ""
Write-Host "Done. Start Reel with:  python transcribe.py"
Write-Host "Then paste your ElevenLabs API key into SETTINGS on the page that opens."
