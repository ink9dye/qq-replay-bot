<#
构建 MoeCardWatch.exe，输出到 release\v1.0（唯一产物，不带重复目录和 zip）。

示例：
    .\build.ps1
    .\build.ps1 -Python "C:\Python312\python.exe"
    .\build.ps1 -Python "C:\...\python.exe" -PyInstallerSite "C:\...\site-packages"
#>
param(
    [string]$Python = "",
    [string]$PyInstallerSite = "",
    [string]$ReleaseName = "v1.0",
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

# 清掉所有历史产物：重复的 dist/build、PyInstaller 残留解包目录、旧 release。
Write-Host "清理历史产物..."
Get-ChildItem $Root -Filter "__pycache__" -Recurse -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
foreach ($name in @("build", "dist", "release", "hello", "temp-run", "temp-run2")) {
    $path = Join-Path $Root $name
    if (Test-Path $path) { Remove-Item $path -Recurse -Force }
}
Get-ChildItem $Root -Directory -Filter "temp-run*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Root -Directory -Filter "_MEI*" -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem -Path $Root -Filter "*.zip" -File -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "MoeCardWatch*" } |
    Remove-Item -Force -ErrorAction SilentlyContinue

$StageDir = Join-Path $Root "build\dist"
$WorkDir = Join-Path $Root "build\work"
New-Item -ItemType Directory -Path $StageDir -Force | Out-Null

$oldPythonPath = $env:PYTHONPATH
$env:PYTHONPATH = $PyInstallerSite
try {
    Write-Host "PyInstaller 打包中..."
    & $PythonExe -m PyInstaller --noconfirm --clean --distpath $StageDir --workpath $WorkDir "$Root\MoeCardWatch.spec"
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 打包失败。" }
}
finally {
    $env:PYTHONPATH = $oldPythonPath
}

$appDir = Join-Path $StageDir "MoeCardWatch"
$exe = Join-Path $appDir "MoeCardWatch.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }

# 唯一的发布目录：release\<版本>\，数据目录 data 运行时才生成，不会被打进去。
$releaseDir = Join-Path (Join-Path $Root "release") $ReleaseName
New-Item -ItemType Directory -Path $releaseDir -Force | Out-Null
Copy-Item (Join-Path $appDir "*") $releaseDir -Recurse
Copy-Item "$Root\README.md" $releaseDir
Copy-Item "$Root\NOTICE.md" $releaseDir
$assetsDir = Join-Path $releaseDir "assets"
New-Item -ItemType Directory -Path $assetsDir -Force | Out-Null
Copy-Item "$Root\assets\preview.png" (Join-Path $assetsDir "preview.png")

# 打包中间产物不再保留，仓库里只留 release 下的一个目录。
Remove-Item (Join-Path $Root "build") -Recurse -Force -ErrorAction SilentlyContinue
Get-ChildItem $Root -Filter "__pycache__" -Recurse -Directory -ErrorAction SilentlyContinue |
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue

$sizeMb = [math]::Round(((Get-ChildItem $releaseDir -Recurse -File |
    Measure-Object -Property Length -Sum).Sum / 1MB), 1)

Write-Host ""
Write-Host "完成：$releaseDir（$sizeMb MB）"
Write-Host "自检：& '$releaseDir\MoeCardWatch.exe' --smoke-test"
