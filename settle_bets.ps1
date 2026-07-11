$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PythonPath = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.14-64\python.exe"

Set-Location $ProjectRoot
& $PythonPath ".\update_sporttery_results.py"
& $PythonPath ".\generate_betting_plan.py" --settle-only
& $PythonPath ".\build_site.py"
