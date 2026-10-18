param(
    [string]$Version = "1.0.0",
    [string]$MinGW = "C:\MinGW",
    [switch]$SkipValidate,
    [switch]$SkipZip,
    [switch]$Portable
)

$ErrorActionPreference = "Stop"
$JudgeDir = $PSScriptRoot
$DistRoot = Join-Path $JudgeDir "dist"
$BuildRoot = Join-Path $JudgeDir "build"
$AppDir = Join-Path $DistRoot "CJudge"
$ZipPath = Join-Path $DistRoot ("CJudge-v{0}-win64.zip" -f $Version)
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

Write-Host "== CJudge portable build =="
Write-Host "Judge dir: $JudgeDir"
Write-Host "MinGW: $MinGW"
Write-Host "Version: $Version"

if (-not (Test-Path -LiteralPath (Join-Path $JudgeDir "data\catalog.json"))) {
    throw "data\catalog.json not found. Run fetch_data.py and seed_tests.py first."
}
if (-not (Test-Path -LiteralPath (Join-Path $JudgeDir "data\tests"))) {
    throw "data\tests not found."
}
if (-not (Test-Path -LiteralPath (Join-Path $MinGW "bin\gcc.exe"))) {
    throw "gcc.exe not found under $MinGW\bin."
}

if (-not $SkipValidate) {
    Write-Host "== Validating reference solutions =="
    Push-Location (Join-Path $JudgeDir "tools")
    try {
        python validate_tests.py
        if ($LASTEXITCODE -ne 0) {
            throw "validate_tests.py failed."
        }
    }
    finally {
        Pop-Location
    }
}

if (Test-Path -LiteralPath $AppDir) {
    Remove-Item -LiteralPath $AppDir -Recurse -Force
}
if (Test-Path -LiteralPath $BuildRoot) {
    Remove-Item -LiteralPath $BuildRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $DistRoot | Out-Null
New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null

Write-Host "== Running PyInstaller =="
Push-Location $JudgeDir
try {
    python -m PyInstaller `
        --noconfirm `
        --clean `
        --windowed `
        --onedir `
        --noupx `
        --name CJudge `
        --paths $JudgeDir `
        --distpath $DistRoot `
        --workpath (Join-Path $BuildRoot "pyinstaller") `
        --specpath $BuildRoot `
        app.py
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed."
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path -LiteralPath (Join-Path $AppDir "CJudge.exe"))) {
    throw "CJudge.exe was not produced."
}

Write-Host "== Copying data =="
$DataDest = Join-Path $AppDir "data"
Copy-Item -LiteralPath (Join-Path $JudgeDir "data") -Destination $DataDest -Recurse -Force
Remove-Item -LiteralPath (Join-Path $DataDest "reference") -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath (Join-Path $DataDest "progress.json") -Force -ErrorAction SilentlyContinue
Get-ChildItem -LiteralPath (Join-Path $DataDest "tests") -Recurse -Filter "meta.json" | ForEach-Object {
    try {
        $meta = Get-Content -LiteralPath $_.FullName -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($meta.PSObject.Properties.Name -contains "reference_sids") {
            $meta.PSObject.Properties.Remove("reference_sids")
            [System.IO.File]::WriteAllText($_.FullName, ($meta | ConvertTo-Json -Depth 8), $Utf8NoBom)
        }
    }
    catch {
        Write-Warning "Could not sanitize $($_.FullName)"
    }
}
try {
    $catalog = Get-Content -LiteralPath (Join-Path $DataDest "catalog.json") -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($property in $catalog.problems.PSObject.Properties) {
        if ($property.Value.PSObject.Properties.Name -contains "reference_sids") {
            $property.Value.PSObject.Properties.Remove("reference_sids")
        }
    }
    [System.IO.File]::WriteAllText((Join-Path $DataDest "catalog.json"), ($catalog | ConvertTo-Json -Depth 12), $Utf8NoBom)
}
catch {
    Write-Warning "Could not sanitize catalog.json"
}

Write-Host "== Copying toolchain =="
$ToolDest = Join-Path $AppDir "toolchain\mingw32"
New-Item -ItemType Directory -Force -Path $ToolDest | Out-Null
robocopy $MinGW $ToolDest /E /XD (Join-Path $MinGW "var") /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) {
    throw "robocopy failed with exit code $LASTEXITCODE"
}
if (-not (Test-Path -LiteralPath (Join-Path $ToolDest "bin\gcc.exe"))) {
    throw "Bundled gcc.exe not found."
}

Write-Host "== Copying notices =="
Copy-Item -LiteralPath (Join-Path $JudgeDir "README.txt") -Destination (Join-Path $AppDir "README.txt") -Force
Copy-Item -LiteralPath (Join-Path $JudgeDir "THIRD_PARTY_NOTICES.txt") -Destination (Join-Path $AppDir "THIRD_PARTY_NOTICES.txt") -Force
Copy-Item -LiteralPath (Join-Path $JudgeDir "licenses") -Destination (Join-Path $AppDir "licenses") -Recurse -Force
if ($Portable) {
    New-Item -ItemType File -Path (Join-Path $AppDir "portable.flag") -Force | Out-Null
}

Write-Host "== Bundled gcc check =="
& (Join-Path $ToolDest "bin\gcc.exe") --version | Select-Object -First 1
if ($LASTEXITCODE -ne 0) {
    throw "Bundled gcc failed to start."
}

Write-Host "== Packaged self-test =="
$TestRoot = Join-Path $env:TEMP "cjudge_build_selftest"
if (Test-Path -LiteralPath $TestRoot) {
    Remove-Item -LiteralPath $TestRoot -Recurse -Force
}
$OldLocalAppData = $env:LOCALAPPDATA
$env:LOCALAPPDATA = $TestRoot
try {
    $process = Start-Process -FilePath (Join-Path $AppDir "CJudge.exe") -ArgumentList "--selftest" -Wait -PassThru
    $reportPath = Join-Path $TestRoot "CJudge\selftest.txt"
    if (Test-Path -LiteralPath $reportPath) {
        Get-Content -LiteralPath $reportPath -Encoding UTF8
    }
    if ($process.ExitCode -ne 0) {
        throw "Packaged self-test failed with exit code $($process.ExitCode)."
    }
}
finally {
    $env:LOCALAPPDATA = $OldLocalAppData
}

$SizeMB = [math]::Round(((Get-ChildItem -LiteralPath $AppDir -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB), 1)
Write-Host ("Unpacked size: {0} MB" -f $SizeMB)

if (-not $SkipZip) {
    Write-Host "== Creating ZIP =="
    if (Test-Path -LiteralPath $ZipPath) {
        Remove-Item -LiteralPath $ZipPath -Force
    }
    Compress-Archive -Path $AppDir -DestinationPath $ZipPath -CompressionLevel Optimal -Force
    $ZipMB = [math]::Round(((Get-Item -LiteralPath $ZipPath).Length / 1MB), 1)
    $Hash = (Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash
    Write-Host ("ZIP: {0}" -f $ZipPath)
    Write-Host ("ZIP size: {0} MB" -f $ZipMB)
    Write-Host ("SHA256: {0}" -f $Hash)
    if ($ZipMB -gt 200) {
        Write-Warning "ZIP is larger than 200 MB. Consider removing gdb/g++/docs from the toolchain."
    }
}

Write-Host "== Build finished =="
