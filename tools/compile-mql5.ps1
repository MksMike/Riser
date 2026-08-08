<#
.SYNOPSIS
  Compila os fontes MQL5 do repositorio e REPROVA quando o compilador reclama.

.DESCRIPTION
  Existe porque o MetaEditor em linha de comando mente de quatro maneiras
  diferentes, e todas as quatro produzem verde falso. Cada uma foi medida
  contra o MetaEditor64 desta maquina, nao deduzida da documentacao.

  1. O CODIGO DE SAIDA E INUTIL, E ENGANA NA DIRECAO PIOR.

     Ele nao e status: e a contagem de .ex5 produzidos.

         compila limpo ........... exit 1
         compila com aviso ....... exit 1
         3 erros de sintaxe ...... exit 0
         arquivo inexistente ..... exit 0   (e sem log nenhum)
         pasta, 2 de 4 ok ........ exit 2

     Um `if ($LASTEXITCODE -ne 0)` reprova a compilacao boa e APROVA a
     quebrada. Este script nunca olha o codigo de saida: le o log.

  2. HA UMA LINHA `Result:` POR ARQUIVO, NAO UMA POR EXECUCAO.

     Compilar uma pasta com quatro arquivos produz quatro linhas. Quem ler a
     ultima reporta o estado do ultimo arquivo e engole os erros dos outros.
     Aqui todas sao somadas, e a soma e conferida contra a contagem de linhas
     de diagnostico.

  3. A RAIZ DE INCLUDE MUDA CONFORME O MODO, E EM UM DELES NAO EXISTE.

         arquivo unico, sem /inc ... pasta de dados do terminal (a junction)
         PASTA, sem /inc .......... pasta de INSTALACAO, que pode nao existir
         com /inc ................. o que se mandou

     Sem /inc, compilar a pasta quebra todo `#include <RISER\...>` com erro
     106. Este script sempre passa /inc apontando para o proprio repositorio.
     Isso tambem resolve o caso do worktree: a junction do terminal aponta
     para UM worktree por vez, e compilar por ela compilaria o codigo do
     worktree errado sem nenhum sintoma.

  4. O COMPILADOR NAO RASTREIA DEPENDENCIA DE .mqh.

     Mexer num .mqh NAO recompila quem o inclui. E compilar uma pasta ignora
     os .mqh por completo - nem os verifica.

     Para este projeto isso e o pior dos quatro: pelo invariante 5 todo sensor
     mora em .mqh, entao a edicao mais comum do repositorio e exatamente a que
     o compilador nao enxerga. Por isso aqui:

         - o build e sempre COMPLETO (os .ex5 sao apagados antes), e
         - cada .mqh e verificado individualmente, porque nada mais o faz.

     Nao ha modo incremental, de proposito. Um atalho rapido que as vezes nao
     recompila e a forma mais confiavel de reintroduzir o verde falso.

.PARAMETER Repo
  Raiz do repositorio. Padrao: pasta pai deste script.

.PARAMETER Path
  Arquivo ou pasta a compilar. Padrao: <Repo>\mql5

.PARAMETER MetaEditor
  Caminho do MetaEditor64.exe. Padrao, nesta ordem: este parametro, a variavel
  RISER_METAEDITOR, a chave `metaeditor` de config/terminals.json, e por fim
  varredura das pastas de programas. Nunca ha caminho fixo no codigo: a
  instalacao e o hash do terminal sao especificos de cada PC.

  Se a varredura achar MAIS DE UM, o script PARA e pede que se fixe qual
  (CLAUDE.md, Higiene de ferramenta). Nao escolhe. Builds diferentes do
  MetaEditor nao sao a mesma ferramenta.

.PARAMETER AllowWarnings
  Aviso deixa de reprovar. Padrao: aviso reprova. Aviso de MQL5 costuma ser
  perda de dado em conversao ou declaracao que esconde outra - as duas coisas
  que este projeto nao pode ter passando silenciosamente numa conversao de
  preco.

.PARAMETER SkipHeaders
  Nao verifica os .mqh individualmente. So use se algum .mqh legitimamente
  depender de contexto de quem o inclui - e nesse caso registre por que.

.PARAMETER SelfTest
  Roda contra casos POSITIVOS conhecidos e sai. Ver a nota no fim.

.EXAMPLE
  .\tools\compile-mql5.ps1
  .\tools\compile-mql5.ps1 -SelfTest
  .\tools\compile-mql5.ps1 -Path mql5\Scripts\ReadSymbolSpecs.mq5

.NOTES
  Saida: um bloco por arquivo com problema, depois o resumo. Zero achados:
  uma linha de resumo, exit 0. Com achados: exit 1.

  POR QUE O -SelfTest EXISTE (invariante 10). Rodar num repositorio que
  compila da verde tanto quando esta tudo certo quanto quando o parser esta
  quebrado e nao acha nada. Os dois estados sao indistinguiveis pela saida. O
  autoteste gera, em pasta temporaria, um fonte com erro conhecido, um com
  aviso conhecido e um limpo, e exige que cada um seja classificado como tal.
  Fixture gerada em codigo, pela ADR 0001.
#>

[CmdletBinding()]
param(
    [string] $Repo,
    [string] $Path,
    [string] $MetaEditor,
    [switch] $AllowWarnings,
    [switch] $SkipHeaders,
    [switch] $SelfTest,
    [switch] $Quiet
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot chega vazio quando o script e invocado por caminho relativo
# atraves de -File. Sem este resgate, o padrao de -Repo explode com "cadeia de
# caracteres vazia" e a mensagem nao diz nada sobre a causa.
$here = $PSScriptRoot
if ([string]::IsNullOrEmpty($here)) { $here = Split-Path -Parent $MyInvocation.MyCommand.Path }
if ([string]::IsNullOrEmpty($Repo)) { $Repo = Split-Path -Parent $here }

# ------------------------------------------------------------ descoberta

function Find-MetaEditor {
    param([string] $Explicito, [string] $RepoRoot)

    if ($Explicito) {
        if (-not (Test-Path -LiteralPath $Explicito)) {
            throw "MetaEditor nao encontrado em '$Explicito'."
        }
        return (Resolve-Path -LiteralPath $Explicito).Path
    }

    if ($env:RISER_METAEDITOR) {
        if (-not (Test-Path -LiteralPath $env:RISER_METAEDITOR)) {
            throw "RISER_METAEDITOR aponta para '$($env:RISER_METAEDITOR)', que nao existe."
        }
        return (Resolve-Path -LiteralPath $env:RISER_METAEDITOR).Path
    }

    # config/terminals.json e local de cada PC e nao entra no Git - mesmo
    # lugar de onde o Python le o data_root.
    $cfg = Join-Path $RepoRoot 'config\terminals.json'
    if (Test-Path -LiteralPath $cfg) {
        # -Encoding UTF8 nao e opcional (invariante 10): o PowerShell 5.1
        # assume a codepage ANSI e um caminho com acento chega corrompido.
        $json = (Get-Content -LiteralPath $cfg -Encoding UTF8) -join "`n" | ConvertFrom-Json
        if ($json.metaeditor) {
            if (-not (Test-Path -LiteralPath $json.metaeditor)) {
                throw "config/terminals.json aponta metaeditor para '$($json.metaeditor)', que nao existe."
            }
            return (Resolve-Path -LiteralPath $json.metaeditor).Path
        }
    }

    $raizes = @($env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ -and (Test-Path $_) }
    $achados = @()
    foreach ($r in $raizes) {
        $achados += Get-ChildItem -LiteralPath $r -Filter 'MetaEditor64.exe' -Recurse -Depth 2 `
            -ErrorAction SilentlyContinue | ForEach-Object { $_.FullName }
    }
    $achados = @($achados | Sort-Object -Unique)

    if ($achados.Count -eq 0) {
        throw ("MetaEditor64.exe nao encontrado. Informe -MetaEditor, ou defina " +
               "RISER_METAEDITOR, ou a chave 'metaeditor' em config/terminals.json.")
    }

    # Ferramenta de build nao escolhe entre alternativas ambiguas: para e pede
    # (CLAUDE.md, Higiene de ferramenta). Builds diferentes do MetaEditor nao
    # sao a mesma ferramenta, e um .ex5 gerado por build mais novo pode nao
    # carregar num terminal mais antigo. Escolher o primeiro faria este PC e o
    # outro compilarem com compiladores diferentes, em silencio, contra o
    # invariante 9 - e a divergencia apareceria num binario que nao carrega,
    # longe da causa.
    if ($achados.Count -gt 1) {
        $lista = ($achados | ForEach-Object { "     $_" }) -join "`n"
        throw (
            "mais de um MetaEditor instalado, e este script nao escolhe por voce:`n" +
            $lista + "`n`n" +
            "   Fixe um, e prefira o de build MAIS ANTIGO entre os terminais em uso:`n" +
            "   um .ex5 gerado por build mais novo pode nao carregar num terminal`n" +
            "   mais antigo; o contrario sempre carrega.`n`n" +
            "   Em config/terminals.json (local, fora do Git):`n" +
            '     "metaeditor": "C:\\...\\MetaEditor64.exe"' + "`n" +
            "   Ou por variavel de ambiente RISER_METAEDITOR, ou por -MetaEditor."
        )
    }
    return $achados[0]
}

# ------------------------------------------------------------ enumeracao

# Extensoes que sao fonte MQL5. Comparacao por Extension, nunca por -Include
# nem por -Filter, e o motivo e medido:
#
#   Get-ChildItem -LiteralPath <pasta> -Recurse -Include '*.mq5','*.mqh'
#
# devolve TUDO - diretorios e .gitkeep inclusive. Com -LiteralPath apontando
# para um diretorio, o -Include e casado contra o nome do proprio caminho, nao
# contra os filhos, e como nao casa, o filtro simplesmente nao se aplica. Nao
# ha erro, nao ha aviso: a contagem sai errada e quem le acredita nela.
#
# -Filter tem outro vicio, mais antigo: casa tambem contra o nome curto 8.3, e
# a semantica de curinga do Windows trata a extensao por prefixo.
$FONTES = @('.mq5', '.mqh')

function Get-Fontes {
    param([string] $Alvo)
    $item = Get-Item -LiteralPath $Alvo
    if (-not $item.PSIsContainer) {
        if ($FONTES -contains $item.Extension) { return @($item) }
        return @()
    }
    return @(
        Get-ChildItem -LiteralPath $Alvo -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $FONTES -contains $_.Extension }
    )
}

function Get-Binarios {
    param([string] $Alvo)
    $item = Get-Item -LiteralPath $Alvo
    if (-not $item.PSIsContainer) { return @() }
    return @(
        Get-ChildItem -LiteralPath $Alvo -Recurse -File -ErrorAction SilentlyContinue |
            Where-Object { $_.Extension -eq '.ex5' }
    )
}

# ------------------------------------------------------------ invocacao

function Invoke-Compilador {
    param([string] $Exe, [string] $Alvo, [string] $IncludeRoot)

    $log = Join-Path ([System.IO.Path]::GetTempPath()) ("riser-mql5-{0}.log" -f [guid]::NewGuid())
    $argumentos = @("/compile:$Alvo", "/inc:$IncludeRoot", "/log:$log")

    $p = Start-Process -FilePath $Exe -ArgumentList $argumentos -Wait -PassThru -WindowStyle Hidden

    $linhas = @()
    if (Test-Path -LiteralPath $log) {
        # UTF-16LE com BOM. O -Encoding explicito e a defesa do invariante 10:
        # hoje o BOM salvaria a leitura mesmo sem ele, e e exatamente por isso
        # que a linha parece dispensavel e some numa limpeza - deixando o
        # parser mudo no dia em que o formato mudar.
        $linhas = @(Get-Content -LiteralPath $log -Encoding Unicode)
        Remove-Item -LiteralPath $log -Force -ErrorAction SilentlyContinue
    }
    return [pscustomobject]@{ Linhas = $linhas; ExitCode = $p.ExitCode; Alvo = $Alvo }
}

# ------------------------------------------------------------ parsing

# Uma por arquivo compilado. 'compiling' para .mq5, 'checking' para .mqh.
$RX_ARQUIVO = '^(?<f>.+\.(?:mq5|mqh))\s*:\s*information:\s*(?:compiling|checking)\s'
# Duas formas para a mesma coisa, e a segunda so aparece em .mqh verificado
# sozinho. Um parser que conheca apenas 'Result:' passa por cima do .mqh
# quebrado sem acusar nada.
$RX_RESULT  = '^\s*(?::\s*information:\s*result|Result:)\s*(?<e>\d+)\s*errors?,\s*(?<w>\d+)\s*warnings?'
$RX_DIAG    = '^(?<f>.*?)\((?<l>\d+),(?<c>\d+)\)\s*:\s*(?<sev>error|warning)\s+(?<code>\d+):\s*(?<msg>.*)$'

function Read-Log {
    param([string[]] $Linhas)

    $arquivos = New-Object System.Collections.ArrayList
    $diags    = New-Object System.Collections.ArrayList
    $somaErr = 0; $somaWarn = 0; $nResult = 0

    foreach ($l in $Linhas) {
        if ($l -match $RX_ARQUIVO) { [void]$arquivos.Add($Matches['f'].Trim()); continue }
        if ($l -match $RX_RESULT) {
            $somaErr  += [int]$Matches['e']
            $somaWarn += [int]$Matches['w']
            $nResult++
            continue
        }
        if ($l -match $RX_DIAG) {
            [void]$diags.Add([pscustomobject]@{
                Arquivo = $Matches['f'].Trim(); Linha = [int]$Matches['l']
                Coluna  = [int]$Matches['c'];   Sev   = $Matches['sev']
                Codigo  = $Matches['code'];     Msg   = $Matches['msg'].Trim()
            })
            continue
        }
    }

    $contErr  = @($diags | Where-Object { $_.Sev -eq 'error' }).Count
    $contWarn = @($diags | Where-Object { $_.Sev -eq 'warning' }).Count

    # Discordancia entre o total declarado e o total contado significa que o
    # log tem forma que este parser nao conhece. Nesse caso o numero MAIOR
    # vale: errar para o lado de reprovar e recuperavel; errar para o lado de
    # aprovar e o modo de falha que este script inteiro existe para evitar.
    $incerto = ($nResult -gt 0) -and (($somaErr -ne $contErr) -or ($somaWarn -ne $contWarn))

    return [pscustomobject]@{
        Arquivos  = @($arquivos)
        Diags     = @($diags)
        Erros     = [Math]::Max($somaErr, $contErr)
        Avisos    = [Math]::Max($somaWarn, $contWarn)
        NResult   = $nResult
        Incerto   = $incerto
        Vazio     = ($Linhas.Count -eq 0)
        # Log com conteudo mas sem nada reconhecivel: nunca reportar "0 erros".
        Ilegivel  = ($Linhas.Count -gt 0 -and $arquivos.Count -eq 0 -and $nResult -eq 0 -and $diags.Count -eq 0)
    }
}

# ------------------------------------------------------------ apresentacao

function Show-Diags {
    param($Leitura, [string] $RepoRoot)
    $porArquivo = $Leitura.Diags | Group-Object Arquivo
    foreach ($g in $porArquivo) {
        $nome = $g.Name
        if ($nome -and $nome.StartsWith($RepoRoot, [StringComparison]::OrdinalIgnoreCase)) {
            $nome = $nome.Substring($RepoRoot.Length).TrimStart('\')
        }
        if (-not $nome) { $nome = '(sem arquivo)' }
        Write-Host $nome -ForegroundColor White
        foreach ($d in $g.Group) {
            $cor = if ($d.Sev -eq 'error') { 'Red' } else { 'Yellow' }
            Write-Host ("   {0}:{1}  {2} {3}: {4}" -f $d.Linha, $d.Coluna, $d.Sev, $d.Codigo, $d.Msg) -ForegroundColor $cor
        }
        Write-Host ''
    }
}

# ------------------------------------------------------------ compilacao

function Invoke-Build {
    param([string] $Exe, [string] $Alvo, [string] $RepoRoot, [switch] $SemCabecalhos, [switch] $Silencioso)

    $incRoot = Join-Path $RepoRoot 'mql5'

    # Build sempre completo: sem isto, um .mqh alterado nao recompila ninguem e
    # o resultado e um "0 erros" que nao compilou nada.
    $ex5 = Get-Binarios -Alvo $Alvo
    $travados = 0
    foreach ($f in $ex5) {
        # Guarda redundante de proposito. Esta funcao apaga arquivos dentro do
        # repositorio; um erro de enumeracao aqui destroi fonte, e fonte novo
        # ainda nao commitado nao volta de lugar nenhum. A checagem custa nada
        # e transforma um bug de filtro em erro visivel.
        if ($f.Extension -ne '.ex5') {
            throw "recusa de apagar '$($f.FullName)': so .ex5 pode ser removido aqui."
        }
        try { Remove-Item -LiteralPath $f.FullName -Force }
        catch { $travados++ }
    }
    if ($travados -gt 0 -and -not $Silencioso) {
        Write-Host ("AVISO: {0} arquivo(s) .ex5 nao puderam ser apagados (terminal com o EA carregado?)." -f $travados) -ForegroundColor Yellow
        Write-Host '   Esses ficam com o binario antigo e o resultado abaixo nao fala sobre eles.' -ForegroundColor Yellow
        Write-Host ''
    }

    $execucoes = New-Object System.Collections.ArrayList
    [void]$execucoes.Add((Invoke-Compilador -Exe $Exe -Alvo $Alvo -IncludeRoot $incRoot))

    # Compilar uma pasta IGNORA os .mqh. Sem esta passagem, nenhum cabecalho do
    # projeto e verificado por coisa nenhuma - e pelo invariante 5 e la que
    # mora todo sensor.
    $cabecalhos = @()
    if (-not $SemCabecalhos) {
        $item = Get-Item -LiteralPath $Alvo
        if ($item.PSIsContainer) {
            $cabecalhos = @(Get-Fontes -Alvo $Alvo | Where-Object { $_.Extension -eq '.mqh' })
            foreach ($h in $cabecalhos) {
                [void]$execucoes.Add((Invoke-Compilador -Exe $Exe -Alvo $h.FullName -IncludeRoot $incRoot))
            }
        }
    }

    $todas = @()
    foreach ($e in $execucoes) { $todas += $e.Linhas }
    $leitura = Read-Log -Linhas $todas
    $leitura | Add-Member -NotePropertyName Execucoes -NotePropertyValue $execucoes.Count
    $leitura | Add-Member -NotePropertyName Cabecalhos -NotePropertyValue $cabecalhos.Count
    return $leitura
}

# ------------------------------------------------------------ autoteste

function Invoke-SelfTest {
    param([string] $Exe, [string] $RepoRoot)

    $tmp = Join-Path ([System.IO.Path]::GetTempPath()) ("riser-selftest-{0}" -f [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tmp 'Scripts') -Force | Out-Null
    New-Item -ItemType Directory -Path (Join-Path $tmp 'Include\RISER\Core') -Force | Out-Null

    function Checa {
        param([string] $Nome, [bool] $Ok, [string] $Detalhe)
        $tag = if ($Ok) { 'ok ' } else { 'FALHA' }
        $cor = if ($Ok) { 'DarkGray' } else { 'Red' }
        Write-Host ("  [{0}] {1}: {2}" -f $tag, $Nome, $Detalhe) -ForegroundColor $cor
        if (-not $Ok) { $script:falhasSelfTest++ }
    }
    $script:falhasSelfTest = 0

    # Fixtures geradas em codigo (ADR 0001). ASCII puro, como todo o resto.
    Set-Content -LiteralPath (Join-Path $tmp 'Include\RISER\Core\Bom.mqh') -Encoding UTF8 -Value @(
        '#ifndef BOM_MQH', '#define BOM_MQH', 'int Soma(const int a, const int b) { return(a + b); }', '#endif')
    Set-Content -LiteralPath (Join-Path $tmp 'Include\RISER\Core\Quebrado.mqh') -Encoding UTF8 -Value @(
        '#ifndef QUEBRADO_MQH', '#define QUEBRADO_MQH', 'int Ruim(const int a) { return(a + ; }', '#endif')
    Set-Content -LiteralPath (Join-Path $tmp 'Scripts\Limpo.mq5') -Encoding UTF8 -Value @(
        '#include <RISER\Core\Bom.mqh>', 'void OnStart() { Print(Soma(2, 3)); }')
    Set-Content -LiteralPath (Join-Path $tmp 'Scripts\ComErro.mq5') -Encoding UTF8 -Value @(
        'void OnStart() { int x = ; Print(x) }')
    Set-Content -LiteralPath (Join-Path $tmp 'Scripts\ComAviso.mq5') -Encoding UTF8 -Value @(
        'void OnStart() { double d = 1.5; int i = d; Print(i); }')

    $inc = $tmp

    Write-Host 'caso limpo' -ForegroundColor Cyan
    $r = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Scripts\Limpo.mq5') -IncludeRoot $inc).Linhas
    Checa 'compila sem erro' ($r.Erros -eq 0) "erros=$($r.Erros)"
    Checa 'compila sem aviso' ($r.Avisos -eq 0) "avisos=$($r.Avisos)"
    Checa 'o include resolveu pelo /inc' ($r.Arquivos.Count -ge 1) "arquivos=$($r.Arquivos.Count)"

    Write-Host 'caso com erro conhecido' -ForegroundColor Cyan
    $r = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Scripts\ComErro.mq5') -IncludeRoot $inc).Linhas
    Checa 'acusa o erro' ($r.Erros -gt 0) "erros=$($r.Erros)"
    Checa 'aponta arquivo e linha' (@($r.Diags | Where-Object { $_.Sev -eq 'error' }).Count -gt 0) "diagnosticos=$($r.Diags.Count)"

    Write-Host 'caso com aviso conhecido' -ForegroundColor Cyan
    $r = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Scripts\ComAviso.mq5') -IncludeRoot $inc).Linhas
    Checa 'acusa o aviso' ($r.Avisos -gt 0) "avisos=$($r.Avisos)"
    Checa 'aviso nao vira erro' ($r.Erros -eq 0) "erros=$($r.Erros)"

    Write-Host 'cabecalho quebrado (a pasta ignora .mqh; a passagem individual e a unica defesa)' -ForegroundColor Cyan
    $r = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Include\RISER\Core\Quebrado.mqh') -IncludeRoot $inc).Linhas
    Checa 'acusa erro em .mqh sozinho' ($r.Erros -gt 0) "erros=$($r.Erros)"
    $rp = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Include') -IncludeRoot $inc).Linhas
    Checa 'a pasta REALMENTE ignora .mqh (por isso a passagem existe)' ($rp.Arquivos.Count -eq 0) "arquivos=$($rp.Arquivos.Count)"

    Write-Host 'alvo inexistente' -ForegroundColor Cyan
    $r = Read-Log -Linhas (Invoke-Compilador -Exe $Exe -Alvo (Join-Path $tmp 'Scripts\NaoExiste.mq5') -IncludeRoot $inc).Linhas
    Checa 'nada compilado e detectado' ($r.Arquivos.Count -eq 0 -and $r.NResult -eq 0) "arquivos=$($r.Arquivos.Count) result=$($r.NResult)"

    # A enumeracao ja errou uma vez, em silencio, e contou diretorio e
    # .gitkeep como fonte. O sintoma foi o pior possivel: o script reprovou uma
    # arvore limpa dizendo "nada foi compilado, mas ha 16 fontes".
    Write-Host 'enumeracao de fontes' -ForegroundColor Cyan
    $en = Join-Path $tmp 'enum'
    New-Item -ItemType Directory -Path (Join-Path $en 'sub') -Force | Out-Null
    foreach ($n in @('a.mq5', 'b.mqh', 'c.txt', '.gitkeep', 'd.ex5', 'e.MQ5')) {
        Set-Content -LiteralPath (Join-Path $en $n) -Encoding UTF8 -Value 'x'
    }
    Set-Content -LiteralPath (Join-Path $en 'sub\f.mqh') -Encoding UTF8 -Value 'x'
    $fon = Get-Fontes -Alvo $en
    Checa 'conta so .mq5/.mqh, e desce em subpasta' ($fon.Count -eq 4) `
        "achou=$($fon.Count) [$(($fon | ForEach-Object { $_.Name }) -join ', ')]"
    Checa 'nao conta diretorio nem .gitkeep nem .txt' `
        (@($fon | Where-Object { $_.Extension -notin @('.mq5', '.mqh') }).Count -eq 0) 'so fontes'
    $bin = Get-Binarios -Alvo $en
    Checa 'binarios: so .ex5' ($bin.Count -eq 1 -and $bin[0].Name -eq 'd.ex5') `
        "achou=$($bin.Count)"
    $um = Get-Fontes -Alvo (Join-Path $en 'a.mq5')
    Checa 'alvo que e arquivo devolve o proprio' ($um.Count -eq 1) "achou=$($um.Count)"
    $nao = Get-Fontes -Alvo (Join-Path $en 'c.txt')
    Checa 'arquivo que nao e fonte devolve vazio' ($nao.Count -eq 0) "achou=$($nao.Count)"

    Write-Host 'parser' -ForegroundColor Cyan
    $fake = @(
        'C:\x\a.mq5 : information: compiling C:\x\a.mq5',
        'C:\x\a.mq5(1,26) : error 157: expression expected',
        'Result: 1 errors, 0 warnings',
        'C:\x\b.mq5 : information: compiling C:\x\b.mq5',
        'C:\x\b.mq5(3,4) : warning 43: possible loss of data',
        'Result: 0 errors, 1 warnings, 10 ms elapsed'
    )
    $r = Read-Log -Linhas $fake
    Checa 'soma TODAS as linhas Result, nao so a ultima' ($r.Erros -eq 1 -and $r.Avisos -eq 1) "erros=$($r.Erros) avisos=$($r.Avisos)"
    $r = Read-Log -Linhas @(' : information: result 5 errors, 0 warnings')
    Checa 'entende o formato de .mqh' ($r.Erros -eq 5) "erros=$($r.Erros)"
    $r = Read-Log -Linhas @('bla bla bla', 'nada reconhecivel aqui')
    Checa 'log ilegivel nao vira zero erro' ($r.Ilegivel) "ilegivel=$($r.Ilegivel)"
    $r = Read-Log -Linhas @('C:\x\a.mq5(1,1) : error 1: x', 'Result: 9 errors, 0 warnings')
    Checa 'discordancia usa o MAIOR (reprovar e recuperavel)' ($r.Erros -eq 9 -and $r.Incerto) "erros=$($r.Erros) incerto=$($r.Incerto)"

    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue

    Write-Host ''
    if ($script:falhasSelfTest -gt 0) {
        Write-Host ("autoteste: FALHOU ({0})" -f $script:falhasSelfTest) -ForegroundColor Red
        return 1
    }
    Write-Host 'autoteste: ok' -ForegroundColor Green
    return 0
}

# ------------------------------------------------------------------ main

$script:Quiet = $Quiet
$Repo = (Resolve-Path -LiteralPath $Repo).Path
if (-not (Test-Path (Join-Path $Repo 'CLAUDE.md'))) {
    throw "'$Repo' nao parece a raiz do RISER (CLAUDE.md nao encontrado)."
}

# Problema de CONFIGURACAO nao merece stack trace. O rastro do PowerShell
# empurra a linha acionavel para cima da tela e faz uma mensagem clara parecer
# um defeito do script - e uma ferramenta que parece quebrada deixa de ser
# usada. Erro interno continua levantando normalmente, com rastro.
try {
    $exe = Find-MetaEditor -Explicito $MetaEditor -RepoRoot $Repo
}
catch {
    Write-Host ''
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Host ''
    exit 1
}

if ($SelfTest) { exit (Invoke-SelfTest -Exe $exe -RepoRoot $Repo) }

if (-not $Path) { $Path = Join-Path $Repo 'mql5' }
if (-not [System.IO.Path]::IsPathRooted($Path)) { $Path = Join-Path $Repo $Path }
if (-not (Test-Path -LiteralPath $Path)) {
    Write-Host "alvo nao existe: $Path" -ForegroundColor Red
    exit 1
}
$Path = (Resolve-Path -LiteralPath $Path).Path

$fontes = Get-Fontes -Alvo $Path
if ((Get-Item -LiteralPath $Path).PSIsContainer -and $fontes.Count -eq 0) {
    if (-not $Quiet) { Write-Host 'nenhum fonte MQL5 em ' -NoNewline; Write-Host $Path -ForegroundColor DarkGray }
    exit 0
}

$r = Invoke-Build -Exe $exe -Alvo $Path -RepoRoot $Repo -SemCabecalhos:$SkipHeaders -Silencioso:$Quiet

$reprova = $false
$motivos = New-Object System.Collections.ArrayList

if ($r.Ilegivel) {
    [void]$motivos.Add('o log do compilador nao foi entendido por este parser')
    $reprova = $true
}
if ($r.Arquivos.Count -eq 0 -and $fontes.Count -gt 0) {
    [void]$motivos.Add("nada foi compilado, mas ha $($fontes.Count) fonte(s) no alvo")
    $reprova = $true
}
if ($r.Erros -gt 0) {
    [void]$motivos.Add("$($r.Erros) erro(s) de compilacao")
    $reprova = $true
}
if ($r.Avisos -gt 0 -and -not $AllowWarnings) {
    [void]$motivos.Add("$($r.Avisos) aviso(s) - use -AllowWarnings para nao reprovar por aviso")
    $reprova = $true
}

if ($r.Diags.Count -gt 0) {
    Write-Host ''
    Show-Diags -Leitura $r -RepoRoot $Repo
}

if ($r.Incerto) {
    Write-Host 'ATENCAO: o total declarado pelo compilador difere da contagem de diagnosticos.' -ForegroundColor Yellow
    Write-Host '   Vale o maior. Se isto se repetir, o formato do log mudou e o parser precisa de revisao.' -ForegroundColor Yellow
    Write-Host ''
}

if (-not $Quiet -or $reprova) {
    $cor = if ($reprova) { 'Red' } else { 'Green' }
    Write-Host ("{0} arquivo(s) compilado(s), {1} cabecalho(s) verificado(s): {2} erro(s), {3} aviso(s)" -f `
        $r.Arquivos.Count, $r.Cabecalhos, $r.Erros, $r.Avisos) -ForegroundColor $cor
}
foreach ($m in $motivos) { Write-Host "   $m" -ForegroundColor Red }
if ($reprova) { Write-Host '' }

exit ([int]$reprova)
