# 启动「听间」本地播客书架（Windows PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File start.ps1
#   powershell -ExecutionPolicy Bypass -File start.ps1 -Port 9000 -NoBrowser
#
# 只用 Python 标准库，不需要 pip install。
param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

$python = $env:PODCAST_PYTHON
if (-not $python) {
    $candidates = @('python', 'python3', 'py')
    foreach ($candidate in $candidates) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if ($found) { $python = $found.Source; break }
    }
}
if (-not $python) {
    throw '没有找到 Python。请安装 Python 3.10 或更新版本，或设置环境变量 PODCAST_PYTHON 指向 python.exe。'
}

$arguments = @((Join-Path $root 'run.py'), '--port', $Port)
if ($NoBrowser) { $arguments += '--no-browser' }

Write-Output "启动中，稍后会自动打开 http://127.0.0.1:$Port"
& $python @arguments
