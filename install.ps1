$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        & py -3 -m venv (Join-Path $projectRoot '.venv')
    } elseif (Get-Command python -ErrorAction SilentlyContinue) {
        & python -m venv (Join-Path $projectRoot '.venv')
    } else {
        throw 'Python 3.10 or newer is required. Install Python with Tcl/Tk support.'
    }
    if ($LASTEXITCODE -ne 0) { throw 'Python environment creation failed.' }
}
& $pythonExe -m pip install --disable-pip-version-check -r (Join-Path $projectRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $pythonExe -c "import tkinter, faster_whisper, imageio_ffmpeg; print('Installation complete.')"
if ($LASTEXITCODE -ne 0) { throw 'Runtime verification failed.' }
