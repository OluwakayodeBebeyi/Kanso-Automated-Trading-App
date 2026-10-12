$ErrorActionPreference = 'Stop'
$python = Join-Path $env:LOCALAPPDATA 'StockPaperTrader\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) {
    throw 'Project virtual environment is missing. Create it with: $venv = Join-Path $env:LOCALAPPDATA "StockPaperTrader\.venv"; py -3 -m venv $venv; & "$venv\Scripts\python.exe" -m pip install -e ".[dev]"'
}
$env:EXECUTION_MODE = 'PAPER'
& $python -m streamlit run (Join-Path $PSScriptRoot 'app\dashboard.py') --server.address 127.0.0.1 --server.port 8501