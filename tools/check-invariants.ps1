<#
.SYNOPSIS
  Varre docs/ e config/ procurando contradicao com os invariantes do CLAUDE.md.

.DESCRIPTION
  Documentacao apodrece em silencio. Quando um invariante muda, os lugares que
  o repetiam continuam dizendo a versao antiga, e ninguem percebe ate alguem
  implementar a partir do documento errado.

  Este script nao tenta ser exaustivo. Ele cobre os quatro modos de falha que
  ja aconteceram neste repositorio. Falso negativo e aceitavel; falso positivo
  ruidoso nao - um verificador que grita todo dia deixa de ser lido, e a partir
  dai vale menos que nenhum.

  Cada regra foi calibrada contra o conteudo real do repositorio ate dar zero
  achado em texto legitimo. Ao acrescentar regra nova, rode antes de commitar:
  se ela acusar algo que esta correto, o problema e da regra, nao do texto.

  CLAUDE.md e README.md NAO sao varridos de proposito. CLAUDE.md e a fonte dos
  invariantes e cita violacoes como exemplo do que nao fazer - varre-lo geraria
  falso positivo garantido.

.PARAMETER Path
  Raiz do repositorio. Padrao: pasta pai deste script.

.PARAMETER ShowRules
  Lista as regras e sai, sem varrer.

.EXAMPLE
  .\tools\check-invariants.ps1
  .\tools\check-invariants.ps1 -ShowRules

.NOTES
  Saida: <arquivo>:<linha>  [REGRA]  descricao
  Zero achados: nenhuma saida, exit 0.
  Com achados: exit 1.

  Supressao, quando o texto cita deliberadamente a forma antiga (tipico em
  docs/decisions/, que registra o que mudou). Na mesma linha ou na anterior:

      <!-- invariant-ok: LOG_PATH cita o caminho anterior a decisao 0002 -->
      # invariant-ok: UNITS_POINTS ...

  Supressao sem motivo escrito depois do ID e aceita, mas nao ajuda ninguem.
#>

[CmdletBinding()]
param(
    [string] $Path = (Split-Path -Parent $PSScriptRoot),
    [switch] $ShowRules
)

$ErrorActionPreference = 'Stop'

# ------------------------------------------------------------------ regras

$Rules = [ordered]@{
    'LOG_PATH' = @{
        Invariant = 'Dado do MT5 e particionado por conta, nao por terminal (CLAUDE.md, Estrutura e caminhos)'
        Why       = 'caminho de log sem o nivel do login: duas contas do mesmo terminal colidem no mesmo arquivo'
    }
    'SYMBOL_HARDCODED' = @{
        Invariant = 'Invariante 3 - simbolo nunca e hardcoded'
        Why       = 'simbolo literal em bloco de codigo; resolver em runtime pelo manifesto da corretora'
    }
    'UNITS_POINTS' = @{
        Invariant = 'Invariante 2 - unidades internas sao neutras de corretora'
        Why       = 'ponto ou lote fora da borda de execucao; internamente e USD por onca e oncas'
    }
    'GITIGNORE_ANCHOR' = @{
        Invariant = 'Regra de .gitignore que se refere a raiz precisa de barra inicial (CLAUDE.md)'
        Why       = 'regra de diretorio sem ancora casa codigo em qualquer nivel; core.ignorecase agrava'
    }
}

if ($ShowRules) {
    foreach ($id in $Rules.Keys) {
        Write-Host $id -ForegroundColor Cyan
        Write-Host ('  invariante : {0}' -f $Rules[$id].Invariant) -ForegroundColor DarkGray
        Write-Host ('  detecta    : {0}' -f $Rules[$id].Why) -ForegroundColor DarkGray
        Write-Host ''
    }
    return
}

$Path = (Resolve-Path $Path).Path
if (-not (Test-Path (Join-Path $Path 'CLAUDE.md'))) {
    throw "'$Path' nao parece a raiz do RISER (CLAUDE.md nao encontrado)."
}

$findings = New-Object System.Collections.ArrayList

function Add-Finding {
    param([string] $File, [int] $Line, [string] $RuleId, [string] $Text)
    $rel = $File.Substring($Path.Length).TrimStart('\')
    [void]$findings.Add([pscustomobject]@{
        File = $rel; Line = $Line; Rule = $RuleId; Text = $Text.Trim()
    })
}

# Supressao vale na propria linha ou na imediatamente anterior. A anterior
# existe porque dentro de bloco de codigo markdown nao cabe comentario HTML.
function Test-Suppressed {
    param([string[]] $Lines, [int] $Index, [string] $RuleId)
    $pattern = 'invariant-ok:\s*' + [regex]::Escape($RuleId)
    if ($Lines[$Index] -match $pattern) { return $true }
    if ($Index -gt 0 -and $Lines[$Index - 1] -match $pattern) { return $true }
    return $false
}

# Linguagens que representam codigo-fonte de verdade. Blocos json/yaml/text
# ficam de fora: ali um simbolo literal e um valor de dado ilustrativo, nao
# uma decisao de implementacao.
$CodeLangs = @('python','py','mql5','mq5','cpp','c','cs','csharp','powershell','ps1','bash','sh','js','ts')

# ------------------------------------------------------- varredura de texto

$targets = New-Object System.Collections.ArrayList
foreach ($sub in @('docs','config')) {
    $dir = Join-Path $Path $sub
    if (-not (Test-Path $dir)) { continue }
    Get-ChildItem $dir -Recurse -File -Include '*.md','*.yaml','*.yml','*.json' |
        ForEach-Object { [void]$targets.Add($_.FullName) }
}

foreach ($file in $targets) {
    # -Encoding UTF8 nao e opcional: os documentos tem acento, e o PowerShell
    # 5.1 assume a codepage ANSI. Sem isso o texto chega corrompido e os
    # padroes com acento deixam de casar em silencio.
    $lines = @(Get-Content -LiteralPath $file -Encoding UTF8)
    # Manifesto de corretora E a declaracao da borda de execucao. Ponto, lote e
    # o simbolo a resolver pertencem ali por definicao - e o unico lugar onde
    # pertencem. Varrer isso seria acusar o arquivo de fazer o seu trabalho.
    $isBrokerManifest = $file -match '\\config\\brokers\\'

    $inFence = $false
    $fenceLang = ''

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $line = $lines[$i]
        $no = $i + 1

        if ($line -match '^\s*```(\w*)') {
            if ($inFence) { $inFence = $false; $fenceLang = '' }
            else          { $inFence = $true;  $fenceLang = $Matches[1].ToLower() }
            continue
        }

        # -- LOG_PATH ---------------------------------------------------
        # So considera algo que realmente pareca um template de caminho de
        # log: contem 'logs', separador e termina em .jsonl. Prosa sobre logs
        # nao casa, que e o ponto.
        if ($line -match 'logs[\\/][^\s`''"]*\.jsonl') {
            $pathText = $Matches[0]
            if ($pathText -notmatch 'hash[-_]login|hash-de-ACCOUNT_LOGIN|account_hash') {
                if (-not (Test-Suppressed $lines $i 'LOG_PATH')) {
                    Add-Finding $file $no 'LOG_PATH' $line
                }
            }
        }

        # -- SYMBOL_HARDCODED -------------------------------------------
        if ($line -match 'XAUUSD|BTCUSD') {
            $flag = $false
            if ($isBrokerManifest) {
                # 'resolve:' e a lista autorizada. Simbolo em qualquer outra
                # chave do manifesto e hardcode disfarcado de configuracao.
                if ($line -notmatch '^\s*resolve\s*:') { $flag = $true }
            }
            elseif ($inFence -and $CodeLangs -contains $fenceLang) {
                $flag = $true
            }
            if ($flag -and -not (Test-Suppressed $lines $i 'SYMBOL_HARDCODED')) {
                Add-Finding $file $no 'SYMBOL_HARDCODED' $line
            }
        }

        # -- UNITS_POINTS -----------------------------------------------
        # Padroes de unidade em uso, nao mencao da palavra. 'o lote' solto em
        # prosa nao casa; 'em pontos', 'degrau_pontos' e '240 pontos' casam.
        if (-not $isBrokerManifest) {
            $unitPattern = '(\bem pontos\b|\bin points\b|[a-z0-9]_pontos\b|[a-z0-9]_points\b|\b\d+([.,]\d+)?\s*pontos\b|\bpor lote\b|\bper[_ ]lot\b)'
            if ($line -match $unitPattern) {
                # Frase que ENUNCIA o invariante cita a unidade proibida de
                # proposito. Acusar isso seria acusar a regra de existir.
                $isStatingTheRule = $line -match '(?i)(errad|nunca|jamais|proibid|nao pode|n\u00e3o pode|nao deve|n\u00e3o deve|borda de execu|so na borda|s\u00f3 na borda|neutr[ao]s? de corretora)'
                if (-not $isStatingTheRule -and -not (Test-Suppressed $lines $i 'UNITS_POINTS')) {
                    Add-Finding $file $no 'UNITS_POINTS' $line
                }
            }
        }
    }
}

# --------------------------------------------------------- .gitignore

$gitignore = Join-Path $Path '.gitignore'
if (Test-Path $gitignore) {
    $lines = @(Get-Content -LiteralPath $gitignore -Encoding UTF8)

    # Diretorios que DEVEM casar em qualquer nivel. Sao artefatos gerados por
    # ferramenta, aparecem aninhados por natureza, e nunca contem fonte.
    $anyLevelOk = @('__pycache__', 'venv', 'node_modules')

    for ($i = 0; $i -lt $lines.Count; $i++) {
        $raw = $lines[$i]
        $rule = $raw.Trim()
        $no = $i + 1

        if ($rule -eq '' -or $rule.StartsWith('#')) { continue }
        if ($rule.StartsWith('!')) { continue }          # negacao
        if (-not $rule.EndsWith('/')) { continue }       # padrao de arquivo
        if ($rule.StartsWith('/')) { continue }          # ja ancorado
        if ($rule.StartsWith('.')) { continue }          # .venv/ .idea/ etc
        if ($rule.StartsWith('*')) { continue }          # *.egg-info/

        # Barra no meio ja torna a regra relativa a raiz (regra do gitignore).
        if ($rule.TrimEnd('/').Contains('/')) { continue }

        $name = $rule.TrimEnd('/')
        if ($anyLevelOk -contains $name) { continue }

        if (-not (Test-Suppressed $lines $i 'GITIGNORE_ANCHOR')) {
            Add-Finding $gitignore $no 'GITIGNORE_ANCHOR' $raw
        }
    }
}

# ------------------------------------------------------------------ saida

if ($findings.Count -eq 0) { exit 0 }

Write-Host ''
foreach ($g in ($findings | Group-Object Rule)) {
    $id = $g.Name
    Write-Host ('[{0}] {1}' -f $id, $Rules[$id].Invariant) -ForegroundColor Yellow
    Write-Host ('   {0}' -f $Rules[$id].Why) -ForegroundColor DarkGray
    foreach ($f in $g.Group) {
        Write-Host ('   {0}:{1}' -f $f.File, $f.Line) -ForegroundColor White
        Write-Host ('      {0}' -f $f.Text) -ForegroundColor DarkGray
    }
    Write-Host ''
}
Write-Host ('{0} achado(s). Corrija o texto, ou suprima com  invariant-ok: <REGRA> <motivo>  se a citacao for deliberada.' -f $findings.Count) -ForegroundColor Yellow
Write-Host ''
exit 1
