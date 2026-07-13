$ErrorActionPreference = "Stop"
$env:PYTHONUTF8 = "1"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = [System.IO.Path]::GetFullPath((Split-Path -Parent $ScriptDir))
$RootPrefix = $Root.TrimEnd(
    [char[]]@(
        [System.IO.Path]::DirectorySeparatorChar,
        [System.IO.Path]::AltDirectorySeparatorChar
    )
) + [System.IO.Path]::DirectorySeparatorChar
Set-Location $Root

function Resolve-RepositoryChildPath {
    param([Parameter(Mandatory = $true)][string]$Path)

    $Candidate = [System.IO.Path]::GetFullPath($Path)
    if (-not $Candidate.StartsWith($RootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove path outside the repository: $Candidate"
    }
    return $Candidate
}

$BuildDir = Resolve-RepositoryChildPath (Join-Path $Root "build")
$DistAppDir = Resolve-RepositoryChildPath (Join-Path $Root "dist\CodexBar")
$ReleaseZip = Resolve-RepositoryChildPath (Join-Path $Root "dist\CodexBar-Windows-x64.zip")
$ReleaseHash = Resolve-RepositoryChildPath ($ReleaseZip + ".sha256")
$ExePath = Join-Path $DistAppDir "CodexBar.exe"
$DashboardAsset = Join-Path $DistAppDir "_internal\codexbar\web_assets\dashboard.html"
$IconAsset = Join-Path $DistAppDir "_internal\codexbar\assets\codexbar.ico"

Remove-Item -LiteralPath $BuildDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $DistAppDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ReleaseZip -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ReleaseHash -Force -ErrorAction SilentlyContinue

uv run --frozen --group build pyinstaller --clean --noconfirm CodexBar.spec

if (-not (Test-Path -LiteralPath $ExePath)) {
    throw "Build failed: dist\CodexBar\CodexBar.exe was not created."
}
if (-not (Test-Path -LiteralPath $DashboardAsset)) {
    throw "Build failed: dashboard.html was not bundled."
}
if (-not (Test-Path -LiteralPath $IconAsset)) {
    throw "Build failed: codexbar.ico was not bundled."
}

$ReleaseDocuments = @(
    @{ Source = "LICENSE"; Destination = "LICENSE" },
    @{ Source = "docs\PRIVACY.md"; Destination = "PRIVACY.md" },
    @{ Source = ".github\SECURITY.md"; Destination = "SECURITY.md" },
    @{ Source = "THIRD_PARTY_NOTICES.md"; Destination = "THIRD_PARTY_NOTICES.md" }
)
foreach ($Document in $ReleaseDocuments) {
    Copy-Item -LiteralPath (Join-Path $Root $Document.Source) `
        -Destination (Join-Path $DistAppDir $Document.Destination)
}

$LicenseDir = Join-Path $DistAppDir "licenses"
uv run --frozen --group build python .\scripts\collect_licenses.py $LicenseDir
if ($LASTEXITCODE -ne 0) {
    throw "Build failed: third-party license collection failed."
}

uv run --frozen --group build python .\scripts\audit_release.py $DistAppDir
if ($LASTEXITCODE -ne 0) {
    throw "Build failed: release privacy audit failed."
}

Compress-Archive -LiteralPath $DistAppDir -DestinationPath $ReleaseZip -CompressionLevel Optimal
$Hash = (Get-FileHash -LiteralPath $ReleaseZip -Algorithm SHA256).Hash.ToLowerInvariant()
$HashLine = "$Hash  $(Split-Path -Leaf $ReleaseZip)"
[System.IO.File]::WriteAllText($ReleaseHash, $HashLine + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

Write-Host "Built: $ExePath"
Write-Host "Release: $ReleaseZip"
Write-Host "SHA256: $ReleaseHash"
