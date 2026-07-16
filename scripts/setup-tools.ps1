[CmdletBinding()]
param(
    [Parameter()]
    [string]$ToolsRoot = (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$packages = @(
    [PSCustomObject]@{
        Name = "BlockParEditor"
        Uri = "https://web.archive.org/web/20251227105508id_/https://vertix.games/tools/BlockParEditor_1.9.zip"
        ArchiveSha256 = "E1D570C007B6999EE18BFA1D273F59986771FE8BD2863C38487F470FF69030E5"
        Executable = "BlockParEditor.exe"
        ExecutableSha256 = "414A289E9F87C4088AD27D79F20A5206D03ACA9124E89D6767D0A042CD794D4F"
    },
    [PSCustomObject]@{
        Name = "RScript"
        Uri = "https://web.archive.org/web/20251227105421id_/https://vertix.games/tools/RScript_4.10f.zip"
        ArchiveSha256 = "E98E2EBD9102D648C744DCB40DA04FC94B00C133BA7C2DF86F58F5AA04C35850"
        Executable = "RScript.exe"
        ExecutableSha256 = "B6E6A0E809EC65215E0C72F58CC9C2707E6F29F56BB625B162523C89489A7777"
    }
)

function Get-Sha256 {
    param([Parameter(Mandatory)][string]$Path)
    return (Get-FileHash -Algorithm SHA256 -LiteralPath $Path).Hash.ToUpperInvariant()
}

$ToolsRoot = [IO.Path]::GetFullPath($ToolsRoot)
New-Item -ItemType Directory -Path $ToolsRoot -Force | Out-Null

foreach ($package in $packages) {
    $target = Join-Path $ToolsRoot $package.Name
    $installedExecutable = Join-Path $target $package.Executable

    if (Test-Path -LiteralPath $installedExecutable -PathType Leaf) {
        $installedHash = Get-Sha256 $installedExecutable
        if ($installedHash -ne $package.ExecutableSha256) {
            throw "$($package.Name) уже существует, но SHA-256 $($package.Executable) не совпадает: $installedHash"
        }
        Write-Host "$($package.Name): уже установлен и проверен"
        continue
    }

    if (Test-Path -LiteralPath $target) {
        throw "Папка уже существует, но проверенный EXE не найден: $target. Установщик не перезаписывает существующие каталоги."
    }

    $token = [Guid]::NewGuid().ToString("N")
    $archive = Join-Path ([IO.Path]::GetTempPath()) "srhd-$($package.Name)-$token.zip"
    $staging = Join-Path $ToolsRoot ".srhd-tool-$($package.Name)-$token"

    try {
        Write-Host "$($package.Name): загрузка $($package.Uri)"
        Invoke-WebRequest -UseBasicParsing -Uri $package.Uri -OutFile $archive

        $archiveHash = Get-Sha256 $archive
        if ($archiveHash -ne $package.ArchiveSha256) {
            throw "SHA-256 архива $($package.Name) не совпадает: $archiveHash"
        }

        Expand-Archive -LiteralPath $archive -DestinationPath $staging
        $stagedExecutable = Join-Path $staging $package.Executable
        if (-not (Test-Path -LiteralPath $stagedExecutable -PathType Leaf)) {
            throw "В архиве отсутствует $($package.Executable)"
        }

        $executableHash = Get-Sha256 $stagedExecutable
        if ($executableHash -ne $package.ExecutableSha256) {
            throw "SHA-256 $($package.Executable) не совпадает: $executableHash"
        }

        Move-Item -LiteralPath $staging -Destination $target
        Write-Host "$($package.Name): установлен в $target"
    }
    finally {
        if (Test-Path -LiteralPath $archive) {
            Remove-Item -LiteralPath $archive -Force
        }
        if (Test-Path -LiteralPath $staging) {
            Remove-Item -LiteralPath $staging -Recurse -Force
        }
    }
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$entryPoint = Join-Path $repositoryRoot "srhd.py"
$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python -and (Test-Path -LiteralPath $entryPoint -PathType Leaf)) {
    Write-Host ""
    Write-Host "Проверка ModKit:"
    & $python.Source -B $entryPoint tools --tools-root $ToolsRoot
}
else {
    Write-Host "Готово. Проверьте установку командой: python -B srhd.py tools --tools-root `"$ToolsRoot`""
}
