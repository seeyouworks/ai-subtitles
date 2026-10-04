$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { & (Join-Path $PSScriptRoot 'install.ps1') }
# NVIDIA's Windows wheels stay inside this project's virtual environment.
& $pythonExe -m pip install --disable-pip-version-check 'nvidia-cublas-cu12==12.9.2.10' 'nvidia-cudnn-cu12==9.27.0.42'
if ($LASTEXITCODE -ne 0) { throw 'GPU runtime installation failed. CPU mode is still available.' }
Push-Location $PSScriptRoot
try {
    & $pythonExe -c "from subtitle_app.core import configure_cuda_paths; configure_cuda_paths(); import ctranslate2; print('CUDA devices:', ctranslate2.get_cuda_device_count())"
} finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'GPU runtime verification failed.' }
