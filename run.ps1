$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    python -m venv .venv
}

$Python = ".\.venv\Scripts\python.exe"
& $Python -c "import yt_dlp, imageio_ffmpeg" 2>$null
if ($LASTEXITCODE -ne 0) {
    & $Python -m pip install -r requirements.txt
}

& $Python app.py --open-browser
