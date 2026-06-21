param(
    [string]$OutputName = "DFJP",
    [string]$UvPath = "",
    [switch]$SkipHookBuild,
    [switch]$SkipExeBuild
)

$ErrorActionPreference = "Stop"

function Resolve-UvExe {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        if (-not (Test-Path -LiteralPath $ExplicitPath -PathType Leaf)) {
            throw "uv.exe not found: $ExplicitPath"
        }
        return (Resolve-Path -LiteralPath $ExplicitPath).Path
    }

    $UvCommand = Get-Command uv -ErrorAction SilentlyContinue
    if (-not $UvCommand) {
        throw "uv.exe not found. Pass -UvPath to the script."
    }
    return $UvCommand.Source
}

function Resolve-CmakeExe {
    $Candidates = @(
        "C:\Program Files\Microsoft Visual Studio\2022\Community\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe",
        "C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
    )

    $FromPath = Get-Command cmake -ErrorAction SilentlyContinue
    if ($FromPath) {
        return $FromPath.Source
    }

    return $Candidates |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
        Select-Object -First 1
}

function Resolve-VcpkgToolchain {
    $Candidates = @()

    if ($env:VCPKG_ROOT) {
        $Candidates += (Join-Path $env:VCPKG_ROOT "scripts\buildsystems\vcpkg.cmake")
    }

    $Candidates += @(
        "C:\vcpkg\scripts\buildsystems\vcpkg.cmake",
        (Join-Path $env:LOCALAPPDATA "vcpkg\scripts\buildsystems\vcpkg.cmake"),
        (Join-Path $env:USERPROFILE "vcpkg\scripts\buildsystems\vcpkg.cmake")
    )

    return $Candidates |
        Where-Object { $_ -and (Test-Path -LiteralPath $_ -PathType Leaf) } |
        Select-Object -First 1
}

function Ensure-HookBuildTree {
    param(
        [string]$CmakeExe,
        [string]$HookDir,
        [string]$VcpkgToolchain
    )

    $BuildDir = Join-Path $HookDir "build"
    $CacheFile = Join-Path $BuildDir "CMakeCache.txt"
    if (Test-Path -LiteralPath $CacheFile -PathType Leaf) {
        $CacheText = Get-Content -LiteralPath $CacheFile -Raw -ErrorAction SilentlyContinue
        if ($CacheText -and $CacheText.Contains($VcpkgToolchain)) {
            return
        }
        Remove-Item -LiteralPath $BuildDir -Recurse -Force
    }

    New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

    & $CmakeExe -S $HookDir -B $BuildDir `
        "-DCMAKE_TOOLCHAIN_FILE=$VcpkgToolchain" `
        "-DCMAKE_BUILD_TYPE=Release" `
        "-DVCPKG_TARGET_TRIPLET=x64-windows-static"
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to configure hook/build with CMake."
    }
}

function Assert-PathInside {
    param(
        [string]$BasePath,
        [string]$TargetPath
    )

    $ResolvedBase = [IO.Path]::GetFullPath($BasePath)
    $ResolvedTarget = [IO.Path]::GetFullPath($TargetPath)
    if (-not $ResolvedTarget.StartsWith($ResolvedBase, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to touch unexpected path: $ResolvedTarget"
    }
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$TranslatorDir = Join-Path $RepoRoot "translator"
$HookDir = Join-Path $RepoRoot "hook"
$ReleaseDir = Join-Path $RepoRoot "release"
$DistDir = Join-Path $RepoRoot "dist"
$BuildDir = Join-Path $RepoRoot "build"
$PyInstallerRoot = Join-Path $BuildDir "pyinstaller"
$PyInstallerDist = Join-Path $PyInstallerRoot "dist"
$PyInstallerWork = Join-Path $PyInstallerRoot "work"
$PyInstallerSpec = Join-Path $PyInstallerRoot "spec"
$StageDir = Join-Path $DistDir "$OutputName-stage"
$ZipPath = Join-Path $DistDir "$OutputName.zip"
$OnedirDir = Join-Path $PyInstallerDist "DFJP"
$MainScript = Join-Path $TranslatorDir "main.py"
$DllPath = Join-Path $HookDir "build\Release\dfhooks.dll"
$ManualRulesPath = Join-Path $TranslatorDir "manual_translation_rules.tsv"
$ManualRulesTemplatePath = Join-Path $TranslatorDir "manual_translation_rules.template.tsv"
$ThirdPartyLicensesScript = Join-Path $RepoRoot "tools\collect_third_party_licenses.py"
$UvExe = Resolve-UvExe -ExplicitPath $UvPath
$CmdFile = Get-ChildItem -LiteralPath $ReleaseDir -Filter "DFJP*.cmd" | Select-Object -First 1

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
New-Item -ItemType Directory -Force -Path $BuildDir | Out-Null

if (-not $SkipHookBuild) {
    $CmakeExe = Resolve-CmakeExe
    if (-not $CmakeExe) {
        throw "cmake.exe not found. Install CMake or pass -SkipHookBuild."
    }
    $VcpkgToolchain = Resolve-VcpkgToolchain
    if (-not $VcpkgToolchain) {
        throw "vcpkg toolchain not found. Set VCPKG_ROOT or install vcpkg."
    }

    Ensure-HookBuildTree -CmakeExe $CmakeExe -HookDir $HookDir -VcpkgToolchain $VcpkgToolchain

    & $CmakeExe --build (Join-Path $HookDir "build") --config Release
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to build dfhooks.dll."
    }
}

if (-not (Test-Path -LiteralPath $DllPath -PathType Leaf)) {
    throw "dfhooks.dll not found: $DllPath"
}
if (-not $CmdFile) {
    throw "Launch CMD file not found in release folder."
}

Write-Host "[1/3] Syncing Python dependencies..."
& $UvExe sync --project $TranslatorDir
if ($LASTEXITCODE -ne 0) {
    throw "uv sync failed for translator project."
}

if (-not $SkipExeBuild) {
    if (Test-Path -LiteralPath $PyInstallerRoot) {
        Assert-PathInside -BasePath $BuildDir -TargetPath $PyInstallerRoot
        Remove-Item -LiteralPath $PyInstallerRoot -Recurse -Force
    }
    New-Item -ItemType Directory -Force -Path $PyInstallerDist | Out-Null
    New-Item -ItemType Directory -Force -Path $PyInstallerWork | Out-Null
    New-Item -ItemType Directory -Force -Path $PyInstallerSpec | Out-Null

    Write-Host "[2/3] Building DFJP.exe..."
    & $UvExe run --project $TranslatorDir --with pyinstaller python -m PyInstaller `
        --noconfirm `
        --clean `
        --name DFJP `
        --windowed `
        --paths $RepoRoot `
        --distpath $PyInstallerDist `
        --workpath $PyInstallerWork `
        --specpath $PyInstallerSpec `
        --hidden-import deep_translator `
        --hidden-import deepl `
        --hidden-import pywintypes `
        --hidden-import win32file `
        --hidden-import win32pipe `
        $MainScript
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed."
    }
}

if (-not (Test-Path -LiteralPath (Join-Path $OnedirDir "DFJP.exe") -PathType Leaf)) {
    throw "Built DFJP.exe not found: $OnedirDir"
}

if (Test-Path -LiteralPath $StageDir) {
    Assert-PathInside -BasePath $DistDir -TargetPath $StageDir
    Remove-Item -LiteralPath $StageDir -Recurse -Force
}
if (Test-Path -LiteralPath $ZipPath) {
    Assert-PathInside -BasePath $DistDir -TargetPath $ZipPath
    Remove-Item -LiteralPath $ZipPath -Force
}

New-Item -ItemType Directory -Force -Path $StageDir | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "dfjp-data") | Out-Null

Copy-Item -Path (Join-Path $OnedirDir "*") -Destination $StageDir -Recurse
Copy-Item -LiteralPath $DllPath -Destination (Join-Path $StageDir "dfhooks.dll")
Copy-Item -LiteralPath $CmdFile.FullName -Destination $StageDir
Copy-Item -LiteralPath (Join-Path $RepoRoot "LICENSE") -Destination (Join-Path $StageDir "LICENSE")
Copy-Item -LiteralPath (Join-Path $ReleaseDir "README_DFJP.txt") -Destination $StageDir
Copy-Item -LiteralPath (Join-Path $TranslatorDir "config.toml") `
    -Destination (Join-Path $StageDir "dfjp-data\config.toml")
if (-not (Test-Path -LiteralPath $ManualRulesTemplatePath -PathType Leaf)) {
    throw "Manual rules template not found: $ManualRulesTemplatePath"
}
Copy-Item -LiteralPath $ManualRulesTemplatePath `
    -Destination (Join-Path $StageDir "dfjp-data\manual_translation_rules.template.tsv")

if (Test-Path -LiteralPath $ManualRulesPath -PathType Leaf) {
    Copy-Item -LiteralPath $ManualRulesPath `
        -Destination (Join-Path $StageDir "dfjp-data\manual_translation_rules.tsv")
} else {
    Copy-Item -LiteralPath $ManualRulesTemplatePath `
        -Destination (Join-Path $StageDir "dfjp-data\manual_translation_rules.tsv")
}

if (-not (Test-Path -LiteralPath $ThirdPartyLicensesScript -PathType Leaf)) {
    throw "Third-party license collector not found: $ThirdPartyLicensesScript"
}
& $UvExe run --project $TranslatorDir --with pyinstaller python $ThirdPartyLicensesScript `
    --output (Join-Path $StageDir "THIRD_PARTY_LICENSES")
if ($LASTEXITCODE -ne 0) {
    throw "Third-party license collection failed."
}

Write-Host "[3/3] Creating ZIP..."
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath

$Hash = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash
Write-Host "Created: $ZipPath"
Write-Host "SHA256:  $Hash"
