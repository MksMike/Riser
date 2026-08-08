<#
.SYNOPSIS
  Cria as junctions entre o repositorio RISER e as pastas MQL5 dos terminais MT5.

.DESCRIPTION
  Descobre os terminais em runtime. O hash da pasta do terminal e especifico de
  cada instalacao e de cada PC, entao nunca deve ser escrito em codigo.

  Pastas de codigo apontam para dentro do repositorio.
  A pasta Files aponta para fora do repositorio (dados nunca entram no Git).

.EXAMPLE
  .\tools\setup-junctions.ps1 -List
  .\tools\setup-junctions.ps1 -TerminalId 53785E099C927DB68A545C249CDBCE06 -Alias exness-standard
  .\tools\setup-junctions.ps1 -TerminalId 53785E09... -Alias exness-standard -Remove
#>

[CmdletBinding()]
param(
    [string] $TerminalId,
    [string] $Alias,
    [string] $Repo     = (Split-Path -Parent $PSScriptRoot),
    [string] $DataRoot = 'C:\dev\RISER-data',
    [switch] $List,
    [switch] $Remove
)

$ErrorActionPreference = 'Stop'
$ProjectName = 'RISER'

$CodeLinks = @{
    'Experts'    = 'mql5\Experts'
    'Indicators' = 'mql5\Indicators'
    'Include'    = 'mql5\Include\RISER'
    'Scripts'    = 'mql5\Scripts'
    'Libraries'  = 'mql5\Libraries'
}

function Get-Terminals {
    $base = Join-Path $env:APPDATA 'MetaQuotes\Terminal'
    if (-not (Test-Path $base)) { throw "Pasta de terminais nao encontrada: $base" }

    Get-ChildItem $base -Directory | Where-Object {
        $_.Name -match '^[0-9A-F]{32}$' -and (Test-Path (Join-Path $_.FullName 'MQL5'))
    } | ForEach-Object {
        $originPath = Join-Path $_.FullName 'origin.txt'
        $origin = if (Test-Path $originPath) { (Get-Content $originPath -Raw).Trim() } else { '' }
        [pscustomobject]@{
            Id       = $_.Name
            MQL5     = Join-Path $_.FullName 'MQL5'
            Origin   = $origin
            Modified = $_.LastWriteTime
        }
    } | Sort-Object Modified -Descending
}

function Remove-JunctionSafe {
    param([string] $Path)

    if (-not (Test-Path $Path)) { return $false }

    $item = Get-Item $Path -Force
    if ($item.LinkType -ne 'Junction') {
        throw "ABORTADO: '$Path' existe e NAO e uma junction. Mova ou apague manualmente."
    }

    # Remove apenas o link. Remove-Item -Recurse numa junction pode apagar o ALVO.
    [System.IO.Directory]::Delete($Path, $false)
    return $true
}

function New-JunctionSafe {
    param([string] $Path, [string] $Target)

    if (-not (Test-Path $Target)) {
        New-Item -ItemType Directory -Path $Target -Force | Out-Null
    }

    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Path $parent -Force | Out-Null
    }

    Remove-JunctionSafe -Path $Path | Out-Null
    New-Item -ItemType Junction -Path $Path -Target $Target | Out-Null
    Write-Host "  OK  $Path" -ForegroundColor Green
    Write-Host "      -> $Target" -ForegroundColor DarkGray
}

# ---------------------------------------------------------------- listagem

if ($List -or -not $TerminalId) {
    $terminals = Get-Terminals
    if (-not $terminals) { throw 'Nenhum terminal MT5 encontrado.' }

    Write-Host ''
    Write-Host 'Terminais MT5 encontrados:' -ForegroundColor Cyan
    Write-Host ''
    foreach ($t in $terminals) {
        Write-Host ('  {0}' -f $t.Id) -ForegroundColor White
        if ($t.Origin) { Write-Host ('      origem   : {0}' -f $t.Origin) -ForegroundColor DarkGray }
        Write-Host ('      alterado : {0}' -f $t.Modified) -ForegroundColor DarkGray
        Write-Host ''
    }
    Write-Host 'Use: .\tools\setup-junctions.ps1 -TerminalId <id> -Alias <apelido>' -ForegroundColor Yellow
    Write-Host 'O apelido nomeia a pasta de dados. Ex: exness-standard, exness-raw-demo' -ForegroundColor DarkGray
    Write-Host ''
    return
}

# ---------------------------------------------------------------- validacao

if (-not $Alias) { throw 'Informe -Alias (ex: exness-standard). Ele nomeia a pasta de dados do terminal.' }
if ($Alias -notmatch '^[a-z0-9\-]+$') { throw "Alias invalido: '$Alias'. Use minusculas, numeros e hifen." }

$terminal = Get-Terminals | Where-Object { $_.Id -eq $TerminalId }
if (-not $terminal) { throw "Terminal '$TerminalId' nao encontrado. Rode com -List." }

$Repo = (Resolve-Path $Repo).Path
if (-not (Test-Path (Join-Path $Repo 'CLAUDE.md'))) {
    throw "'$Repo' nao parece a raiz do RISER (CLAUDE.md nao encontrado)."
}

$mql5 = $terminal.MQL5

Write-Host ''
Write-Host "Repositorio : $Repo"     -ForegroundColor Cyan
Write-Host "Terminal    : $TerminalId" -ForegroundColor Cyan
Write-Host "Alias       : $Alias"     -ForegroundColor Cyan
Write-Host ''

# ---------------------------------------------------------------- remocao

if ($Remove) {
    Write-Host 'Removendo junctions...' -ForegroundColor Yellow
    foreach ($folder in $CodeLinks.Keys) {
        $link = Join-Path $mql5 "$folder\$ProjectName"
        if (Remove-JunctionSafe -Path $link) { Write-Host "  removido  $link" -ForegroundColor DarkGray }
    }
    $filesLink = Join-Path $mql5 "Files\$ProjectName"
    if (Remove-JunctionSafe -Path $filesLink) { Write-Host "  removido  $filesLink" -ForegroundColor DarkGray }
    Write-Host ''
    Write-Host 'Concluido. Nenhum arquivo do repositorio ou de dados foi tocado.' -ForegroundColor Green
    Write-Host ''
    return
}

# ---------------------------------------------------------------- criacao

Write-Host 'Codigo (aponta para o repositorio):' -ForegroundColor Yellow
foreach ($folder in $CodeLinks.Keys | Sort-Object) {
    New-JunctionSafe -Path (Join-Path $mql5 "$folder\$ProjectName") `
                     -Target (Join-Path $Repo $CodeLinks[$folder])
}

Write-Host ''
Write-Host 'Dados (aponta para FORA do repositorio):' -ForegroundColor Yellow
New-JunctionSafe -Path (Join-Path $mql5 "Files\$ProjectName") `
                 -Target (Join-Path $DataRoot "mt5\$Alias")

Write-Host ''
Write-Host 'Concluido.' -ForegroundColor Green
Write-Host 'No MetaEditor os arquivos aparecem em Experts\RISER, Indicators\RISER, etc.' -ForegroundColor DarkGray
Write-Host ''
