<#
.SYNOPSIS
  Ponte entre o hook PostToolUse do Claude Code e tools/compile-mql5.ps1.

.DESCRIPTION
  Le o JSON do hook em stdin, decide se o arquivo tocado e fonte MQL5 deste
  repositorio, e se for, compila. Nada mais.

  Existe como ARQUIVO e nao como comando embutido no settings.json por dois
  motivos concretos:

  - Comando dentro de JSON nao se testa. Este pode ser exercitado sozinho:
        echo '{"tool_input":{"file_path":"..."}}' | powershell -File este.ps1
  - Comando dentro de JSON escapa aspas e barras invertidas duas vezes, num
    caminho do Windows. O primeiro erro de escape produz um hook que nunca
    dispara - e hook que nao dispara e indistinguivel de codigo que compila.

  CONTRATO DE SAIDA, que e o que faz o erro chegar em quem pode corrigir:

      exit 0    nada a fazer, ou compilou limpo. Silencio.
      exit 2    falhou. O texto vai para stderr e o Claude Code o devolve ao
                modelo como erro bloqueante, junto com arquivo, linha e coluna.

  SILENCIO NO SUCESSO E DELIBERADO. O CLAUDE.md e explicito: um verificador que
  grita todo dia deixa de ser lido, e a partir dai vale menos que nenhum.
#>

[CmdletBinding()]
param(
    [string] $Repo,
    [switch] $DryRun   # imprime a decisao e sai; usado pelo autoteste
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------ raiz do repo
#
# CLAUDE_PROJECT_DIR vem do Claude Code. O resgate pela pasta do proprio script
# nao e enfeite: em git worktree, a variavel aponta para o worktree ativo, e o
# script tambem vive nele. As duas formas convergem no lugar certo, e a segunda
# funciona quando o script e chamado a mao.
if ([string]::IsNullOrEmpty($Repo)) { $Repo = $env:CLAUDE_PROJECT_DIR }
if ([string]::IsNullOrEmpty($Repo)) {
    $here = $PSScriptRoot
    if ([string]::IsNullOrEmpty($here)) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
    $Repo = Split-Path -Parent $here
}
if (-not (Test-Path -LiteralPath (Join-Path $Repo 'CLAUDE.md'))) { exit 0 }
$Repo = (Resolve-Path -LiteralPath $Repo).Path

# ------------------------------------------------------------ stdin
$bruto = [Console]::In.ReadToEnd()
if ([string]::IsNullOrWhiteSpace($bruto)) { exit 0 }

try { $evento = $bruto | ConvertFrom-Json }
catch {
    # JSON que nao parseia nao e motivo para reprovar uma edicao: o hook nao
    # sabe nem de que arquivo se trata. Sai quieto.
    exit 0
}

$alvo = $null
if ($evento.tool_input -and $evento.tool_input.file_path) { $alvo = [string]$evento.tool_input.file_path }
elseif ($evento.tool_response -and $evento.tool_response.filePath) { $alvo = [string]$evento.tool_response.filePath }
if ([string]::IsNullOrWhiteSpace($alvo)) { exit 0 }

# ------------------------------------------------------------ filtro
#
# Extensao case-insensitive: o Windows nao distingue .mq5 de .MQ5, e um filtro
# sensivel a caixa deixaria passar exatamente o arquivo que precisava compilar.
$ext = [System.IO.Path]::GetExtension($alvo)
if ($ext -notin @('.mq5', '.mqh')) { exit 0 }

# So compila o que e deste repositorio. Sem isto, editar um .mq5 de qualquer
# outro lugar dispararia um build daqui, e o resultado falaria de outro codigo.
try { $alvoAbs = [System.IO.Path]::GetFullPath($alvo) } catch { exit 0 }
$arvore = [System.IO.Path]::GetFullPath((Join-Path $Repo 'mql5'))
if (-not $alvoAbs.StartsWith($arvore, [StringComparison]::OrdinalIgnoreCase)) { exit 0 }

if ($DryRun) {
    Write-Host "compilaria: $alvoAbs"
    exit 0
}

# ------------------------------------------------------------ compila
#
# A arvore INTEIRA, nunca so o arquivo tocado. Um .mqh alterado nao recompila
# quem o inclui - o compilador do MetaEditor nao rastreia essa dependencia - e
# compilar so o arquivo salvo daria verde enquanto o consumidor dele esta
# quebrado. Ver o cabecalho de compile-mql5.ps1.
$compilador = Join-Path $Repo 'tools\compile-mql5.ps1'
if (-not (Test-Path -LiteralPath $compilador)) { exit 0 }

# `6>&1` nao e enfeite. O compilador formata com Write-Host, que desde o
# PowerShell 5 escreve no stream de INFORMACAO (6), nao no de sucesso (1).
# Sem o `6>`, `$saida` volta com zero caractere e o hook reporta "falhou" sem
# dizer onde - a informacao util fica no console de um processo que ninguem
# esta olhando. Isso passou despercebido no teste por terminal, onde o console
# ia direto para o pipe e a saida aparecia por outro caminho.
$saida = & $compilador -Repo $Repo -Quiet 2>&1 6>&1 | Out-String
$codigo = $LASTEXITCODE

# ------------------------------------------------------------ segundo portao
#
# Fonte que existe no disco e nao no Git. O portao de cima pega codigo que nao
# compila; este pega codigo que nao sobrevive - um .mq5 untracked ja foi perdido
# neste repositorio, apagado por um comando de terceiro, sem nada de onde
# restaurar (CLAUDE.md, Higiene de ferramenta).
#
# AVISO, nunca bloqueio. Arquivo em criacao passa por untracked por definicao, e
# reprovar a propria criacao seria hostil. O objetivo e que ele nao fique
# invisivel, nao que ele nao exista.
function Get-FontesForaDoGit {
    param([string] $Raiz)
    try {
        # --others: nao rastreados. --exclude-standard: respeita o .gitignore,
        # senao todo .ex5 apareceria como "esquecido".
        $saida = & git -C $Raiz ls-files --others --exclude-standard -- 'mql5' 2>$null
        if ($LASTEXITCODE -ne 0) { return @() }
        return @($saida | Where-Object { $_ -match '\.(mq5|mqh)$' })
    }
    catch { return @() }
}

$soltos = Get-FontesForaDoGit -Raiz $Repo
$aviso = ''
if ($soltos.Count -gt 0) {
    $aviso = "Fonte MQL5 fora do Git: " + ($soltos -join ', ') +
             ". Arquivo untracked nao sobrevive a um comando errado; " +
             "commit com 'wip:' e mais barato que reescrever."
}

if ($codigo -eq 0) {
    if ($aviso) {
        # systemMessage mostra ao usuario SEM bloquear. Sair 2 aqui devolveria
        # o texto, mas como erro bloqueante - e reprovar a criacao de um arquivo
        # por ele acabar de ser criado seria hostil e treinaria todo mundo a
        # ignorar o hook.
        $j = @{ systemMessage = "AVISO  $aviso"; suppressOutput = $true } |
             ConvertTo-Json -Compress
        [Console]::Out.Write($j)
    }
    exit 0
}

[Console]::Error.WriteLine("Compilacao MQL5 falhou apos editar $alvo")
[Console]::Error.WriteLine($saida.TrimEnd())
if ($aviso) { [Console]::Error.WriteLine("AVISO  $aviso") }
[Console]::Error.WriteLine("Corrija antes de seguir. Para rodar a mao: .\tools\compile-mql5.ps1")
exit 2
