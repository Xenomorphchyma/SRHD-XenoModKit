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
        Uri = "https://github.com/indiemagpie/BlockParEditor/releases/download/2.1/BlockParEditor_2.1.zip"
        ArchiveSha256 = "D0C4739F9FBF1C276AC6979C13E838B217694AC16400109DB7ED53435F0D0253"
        Executable = "BlockParEditor.exe"
        ExecutableSha256 = "84D1FA626451F165135139EC9D00306207BA765B2CA3071591009F831B5711FA"
        LegacyExecutableSha256 = "414A289E9F87C4088AD27D79F20A5206D03ACA9124E89D6767D0A042CD794D4F"
        LegacyTarget = "BlockParEditor19"
    },
    [PSCustomObject]@{
        Name = "RScript"
        Uri = "https://github.com/indiemagpie/RScript/releases/download/4.15f/RScript_4.15f.zip"
        ArchiveSha256 = "DF39FF7D00242812EDD543899F5427DFC25B0D0B37032982B7673536AD5E7B40"
        Executable = "RScript.exe"
        ExecutableSha256 = "1CF7724E37657E00103652D4843C8A4D3DCFEEDAA533703CE8EA82209252693F"
        LegacyExecutableSha256 = "B6E6A0E809EC65215E0C72F58CC9C2707E6F29F56BB625B162523C89489A7777"
        LegacyTarget = "RScript410"
    },
    [PSCustomObject]@{
        Name = "RScript410"
        Uri = "https://web.archive.org/web/20251227105421id_/https://vertix.games/tools/RScript_4.10f.zip"
        ArchiveSha256 = "E98E2EBD9102D648C744DCB40DA04FC94B00C133BA7C2DF86F58F5AA04C35850"
        Executable = "RScript.exe"
        ExecutableSha256 = "B6E6A0E809EC65215E0C72F58CC9C2707E6F29F56BB625B162523C89489A7777"
        LegacyExecutableSha256 = $null
        LegacyTarget = $null
    },
    [PSCustomObject]@{
        Name = "RSMCompiler"
        Uri = "https://github.com/indiemagpie/RScript/releases/download/4.15f/rsmc.zip"
        ArchiveSha256 = "FDE03859AA2DB3E15514E3DF9A5044D79EC0D1D684414B6105AED9C1ED1A7E6A"
        Executable = "rsmc.exe"
        ExecutableSha256 = "BFB59C8EDA31BDACF2865DD8179E8278331215E51D4297AF6325566599427CAA"
        LegacyExecutableSha256 = $null
        LegacyTarget = $null
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
        if ($installedHash -eq $package.ExecutableSha256) {
            Write-Host "$($package.Name): уже установлен и проверен"
            continue
        }
        if ($null -ne $package.LegacyExecutableSha256 -and $installedHash -eq $package.LegacyExecutableSha256) {
            $legacyTarget = Join-Path $ToolsRoot $package.LegacyTarget
            if (Test-Path -LiteralPath $legacyTarget) {
                throw "Найдена старая $($package.Name), но каталог сохранения уже существует: $legacyTarget"
            }
            Move-Item -LiteralPath $target -Destination $legacyTarget
            Write-Host "$($package.Name): прежняя проверенная версия сохранена в $legacyTarget"
        }
        else {
            throw "$($package.Name) уже существует, но SHA-256 $($package.Executable) не является ни текущим, ни известным предыдущим: $installedHash"
        }
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
