<#
构建 MoeCardWatch.exe 和可分发 zip。

示例：
    .\build.ps1
    .\build.ps1 -Python "C:\Python312\python.exe"
    .\build.ps1 -Python "C:\...\python.exe" -PyInstallerSite "C:\...\site-packages"
#>
param(
    [string]$Python = "",
    [string]$PyInstallerSite = "",
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

function Find-Python {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "找不到 Python：$Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    $candidates = @(
        (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
        "C:\Python312\python.exe",
        "C:\Python311\python.exe",
        "D:\Anaconda3\python.exe"
    ) | Where-Object { $_ -and (Test-Path $_) }
    foreach ($candidate in $candidates) {
        return $candidate
    }
    throw "找不到 Python。请用 -Python 指定 python.exe。"
}

function Find-PyInstallerSite {
    param([string]$PythonExe, [string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path (Join-Path $Explicit "PyInstaller"))) {
            throw "指定目录里没有 PyInstaller：$Explicit"
        }
        return (Resolve-Path $Explicit).Path
    }
    $known = @(
        "C:\Users\$env:USERNAME\.workbuddy\binaries\python\envs\default\Lib\site-packages",
        "C:\Users\$env:USERNAME\.cache\pyinstaller-site"
    ) | Where-Object { (Test-Path (Join-Path $_ "PyInstaller")) }
    $first = @($known) | Select-Object -First 1
    if ($first) { return [string]$first }
    $oldError = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $probe = & $PythonExe -c "import PyInstaller, os; print(os.path.dirname(os.path.dirname(PyInstaller.__file__)))" 2>$null
    $probeCode = $LASTEXITCODE
    $ErrorActionPreference = $oldError
    if ($probeCode -eq 0 -and $probe) { return $probe.Trim() }
    throw "PyInstaller 不可用。请先安装 PyInstaller，或用 -PyInstallerSite 指定包含 PyInstaller 的 site-packages。"
}

$PythonExe = Find-Python -Explicit $Python
Write-Host "Python: $PythonExe"
$PyInstallerSite = Find-PyInstallerSite -PythonExe $PythonExe -Explicit $PyInstallerSite
Write-Host "PyInstaller site-packages: $PyInstallerSite"

if (-not $SkipTests) {
    Write-Host "运行单元测试..."
    & $PythonExe -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw "单元测试失败，停止打包。" }
}

Get-ChildItem $Root -Filter "__pycache__" -Recurse -Directory -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
if (Test-Path "$Root\build") { Remove-Item "$Root\build" -Recurse -Force }
if (Test-Path "$Root\dist") { Remove-Item "$Root\dist" -Recurse -Force }
New-Item -ItemType Directory -Path "$Root\dist" | Out-Null

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $PyInstallerSite
try {
    Write-Host "PyInstaller 打包中..."
    & $PythonExe -m PyInstaller --noconfirm --clean "$Root\MoeCardWatch.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}

$appDir = Join-Path $Root "dist\MoeCardWatch"
$exe = Join-Path $appDir "MoeCardWatch.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

# 生成一个“便携版”目录：数据写在 exe 旁，解压即用。
$release = Join-Path $Root "dist\MoeCardWatch-win64"
if (Test-Path $release) { Remove-Item $release -Recurse -Force }
New-Item -ItemType Directory -Path $release | Out-Null
Copy-Item (Join-Path $appDir "*") $release -Recurse
Copy-Item "$Root\README.md" $release
Copy-Item "$Root\NOTICE.md" $release
New-Item -ItemType Directory -Path (Join-Path $release "assets") -Force | Out-Null
Copy-Item "$Root\assets\preview.png" (Join-Path $release "assets\preview.png")
New-Item -ItemType File -Path (Join-Path $release "portable.flag") | Out-Null

$zip = Join-Path $Root "dist\MoeCardWatch-win64.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path (Join-Path $release "*") -DestinationPath $zip -CompressionLevel Optimal

Write-Host ""
Write-Host "完成："
Write-Host "  exe: $exe"
Write-Host "  zip: $zip"
Write-Host "  dir: $release"
Write-Host ""
Write-Host "校验：运行 exe --smoke-test（窗口自动关闭即正常）。"
