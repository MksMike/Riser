# RISER

Sistema modular de scalp para MetaTrader 5. Ativo primário: XAUUSD. BTCUSD depois.

**Fase atual: 1 (Fundação).** Nada de EA em produção. Nada de produto.

---

## Como este projeto pensa

A unidade atômica não é o EA — é o **sensor**. Sensores descrevem o mercado.
EAs orquestram sensores. O Guardian veta. O motor de saída executa saídas.

O valor do projeto não está em nenhum sensor específico. Está na infraestrutura
que permite testar e descartar sensores rápido.

### O EA busca edge independente. O estilo manual não é especificação.

Decidido em 2026-08-09. Era a decisão em aberto nº 1.

O histórico manual não é alvo a replicar nem fonte de rótulo. Sensor não é
avaliado por concordar com o dono — é avaliado pelos critérios do seu próprio
documento, contra o mercado.

Duas coisas decorrem disto, e valem mais que a decisão em si:

- **Captura de operações manuais deixa de ser caminho crítico.** Ela existia
  para produzir dado rotulado pelo julgamento do dono; sem esse consumidor, é
  arquivo sem leitor.
- **Um sensor que só funciona quando confirma o que o dono faria é sensor
  reprovado**, não sensor validado.

Evidência que acompanhou a decisão: o baseline aleatório casado, sobre 193
entradas manuais reais, não encontrou edge de timing — P(real melhor) de 0,39
contra sintéticas aleatórias e 0,47 casando por volatilidade, com o intervalo a
cruzar 0,50. Amostra pequena demais para concluir, e é por isso que a decisão
não se apoia nela: apoia-se em para onde o projeto vai.

### Modelo de camadas

| | Camada | Pergunta que responde | Critério de saída |
|---|---|---|---|
| 1 | Fundação | Consigo medir? | Paridade Python↔MQL5 provada; coletor rodando em 2 corretoras |
| 2 | Core | Os sensores enxergam? | todos os critérios de aceitação do formato SVC (ver `docs/sensors/`) cumpridos e registrados |
| 3 | Demo + real mínimo, em paralelo | Sobrevive fora do backtest? | Desempenho ao vivo dentro da faixa prevista |
| 4 | Real com capital | Sobrevive com dinheiro? | Curva consistente por 3 meses |
| 5 | Produto | Outra pessoa consegue usar? | — |

Camadas 3 e 4 **não** são sequenciais: demo mede estabilidade de código, real
mede custo e slippage. São perguntas diferentes, respondidas ao mesmo tempo.

---

## Invariantes

Estas regras não se negociam por conveniência. Se uma tarefa parece exigir que
uma delas seja quebrada, pare e levante a questão em vez de contornar.

### 1. Sensor descreve, nunca decide

Sensor é função pura de `(janela de dados, parâmetros)`. Não acessa conta,
posição, ordem ou lote. Nenhum sensor retorna "compra" ou "venda".

Se um sensor precisar do estado da conta para produzir sua leitura, a
implementação está errada.

Contrato de saída, idêntico para todos:

```
ts, value (0..1), state (enum), confidence (0..1), freshness_ms, components{}
```

`components` é sempre preenchido e sempre logado. Logar só o valor composto
torna impossível diagnosticar qual metade errou.

### 2. Unidades internas são neutras de corretora

Ponto e lote são unidades específicas de corretora. Dígitos variam (2 ou 3),
tamanho de contrato varia (100 oz, 10 oz). "240 pontos" não significa nada fora
da Exness.

| Domínio | Unidade interna | Convertida onde |
|---|---|---|
| Preço e distância | USD por onça | só na borda de execução |
| Tamanho de posição | onças | só na borda de execução |
| Risco e resultado | JPY | nunca |

### 3. Símbolo nunca é hardcoded

Nada de `"XAUUSDm"` em código. Resolver em runtime: `SYMBOL_DIGITS`,
`SYMBOL_POINT`, `SYMBOL_TRADE_CONTRACT_SIZE`, `SYMBOL_TRADE_TICK_VALUE`,
`SYMBOL_TRADE_STOPS_LEVEL`, `SYMBOL_FILLING_MODE`.

Cada corretora tem um manifesto em `config/brokers/`. O EA declara requisitos;
o Guardian recusa iniciar se o manifesto não os satisfizer.

### 4. Causalidade — sem lookahead

Sensor só usa informação disponível até o instante T. O harness prova isso
empiricamente truncando o histórico posterior e verificando saída idêntica.
Repintura é praticamente invisível na leitura de código.

### 5. Indicador é casca de exibição

O indicador que desenha um sensor **chama exatamente o mesmo código** que o EA
chama. Nunca reimplementa. Uma fonte de verdade, dois consumidores.

Por isso todo sensor mora em `mql5/Include/RISER/Sensors/` como `.mqh`, nunca
dentro do EA.

Desenho é caro e o tester tem teto de objetos: `visual_debug` é parâmetro,
desligado por padrão.

### 6. Toda linha de log carrega três identificadores

`run_id`, `build_hash`, `config_hash`. Sem eles, resultados de meses de teste em
várias corretoras viram arquivos que não se atribuem a nada.

### 7. Dado nunca entra no Git

Código no repositório. Tick, log, resultado de backtest em `C:\dev\RISER-data`.
Se `git status` mostrar arquivo de dado, o `.gitignore` está errado — corrija o
`.gitignore`, não adicione exceção.

### 8. Custo é parâmetro, não constante

Todo backtest roda contra os perfis de `config/cost_profiles.yaml` e reporta as
curvas lado a lado. "Standard ou Raw" é coluna de relatório, não decisão.

### 9. Ambiente é reproduzível ou não é ambiente

Instalação sempre por `requirements.lock.txt`. O `pyproject.toml` declara
intenção; o lock define a realidade. Os dois PCs devem produzir resultado
idêntico a partir dos mesmos ticks.

`requires-python` tem teto, não só piso. Sem teto, um venv mais novo resolve
versões diferentes em silêncio e a divergência aparece meses depois, num
resultado de backtest, longe da causa.

Versão nova de dependência não entra por conveniência: entra por decisão, com
o lock regenerado nos dois PCs e uma comparação de agregação sobre a mesma
amostra de ticks antes de ser aceita.

### 10. Ferramenta que verifica não pode falhar em silêncio

Todo script em `tools/` é **ASCII puro**. Acentuação em padrão de busca entra
por escape (`não`), nunca como caractere literal.

Toda leitura de arquivo declara o encoding:

```powershell
Get-Content -LiteralPath $f -Encoding UTF8
```

O motivo é concreto: o PowerShell 5.1 lê `.ps1` como ANSI e assume a codepage
do sistema em `Get-Content` sem `-Encoding`. Um regex com acento chega
corrompido e **deixa de casar sem erro nenhum** — a verificação passa a
responder "nada encontrado" enquanto está quebrada.

Falso negativo silencioso em ferramenta de verificação é pior que não ter
ferramenta: sem ela ninguém confia no repositório e as coisas se conferem à
mão; com ela quebrada, todos confiam e ninguém confere.

Por isso ferramenta de verificação se testa contra caso **positivo** conhecido,
não só contra o repositório limpo. Verde num repositório sem problema não
distingue "não há achado" de "não consigo achar".

---

## Estrutura e caminhos

```
C:\dev\RISER\          repositório (este)
C:\dev\RISER-data\     dados — fora do Git, nunca versionado
```

Dentro da pasta MQL5 do terminal, tudo vive em subpasta `RISER`:

```
MQL5\Experts\RISER\      → junction para mql5\Experts
MQL5\Indicators\RISER\   → junction para mql5\Indicators
MQL5\Include\RISER\      → junction para mql5\Include\RISER
MQL5\Scripts\RISER\      → junction para mql5\Scripts
MQL5\Libraries\RISER\    → junction para mql5\Libraries
MQL5\Files\RISER\        → junction para C:\dev\RISER-data\mt5\<alias>
```

`Files` aponta para fora do repositório de propósito — é onde o MT5 escreve
dados.

### Dado do MT5 é particionado por conta, não por terminal

Junction é por terminal. Conta é por login. Um mesmo terminal troca de conta
sem que nada no sistema de arquivos mude — e a partir daí duas contas escrevem
no mesmo diretório, com custo, spread e execução diferentes, sem forma de
separar depois.

Por isso o alias do terminal nunca é o nível mais profundo. Quem escreve
resolve `ACCOUNT_LOGIN` em runtime e desce mais um nível:

```
C:\dev\RISER-data\mt5\<alias>\<hash-de-ACCOUNT_LOGIN>\
```

O login entra sempre com hash, nunca em claro — mesma regra do schema de log.

Regras que decorrem disto:

- Nenhum componente escreve direto na raiz de `Files\RISER\`. O diretório da
  conta é criado na inicialização, depois de ler o login.
- Trocar a conta de um terminal em execução muda o diretório de destino. Se o
  login mudar durante uma execução, o componente encerra e registra o erro em
  vez de continuar escrevendo no diretório anterior.
- Contas diferentes da mesma corretora exigem terminais instalados em pastas
  separadas, cada um com seu alias. Uma instalação, uma conta ativa.

### Regra de `.gitignore` que se refere à raiz precisa de barra inicial

Sem âncora, a regra casa diretório de código em qualquer nível — e no Windows
`core.ignorecase` faz `Logs/` casar `logs/` também.

Ao adicionar regra nova, teste com `git check-ignore` contra um caminho de
código plausível:

```powershell
git check-ignore -v python/riser/logs/writer.py
```

O modo de falha é silencioso: o arquivo simplesmente não aparece em
`git status`, e o problema só se manifesta ao clonar em outro PC.

Includes usam o prefixo do projeto: `#include <RISER\Core\Sensor.mqh>`

O hash do terminal (`53785E09...`) é específico de cada PC. Nunca escreva esse
caminho em código ou script. Use `tools\setup-junctions.ps1`, que descobre os
terminais em runtime.

---

## Antes de commitar

```powershell
.\tools\check-invariants.ps1                 # exit 0, silencioso
.\tools\compile-mql5.ps1                     # exit 0; nem erro nem aviso
python\.venv\Scripts\python.exe -m pytest    # ao menos um teste, e passa
```

- `check-invariants` sai 0. Achado é para corrigir, não para suprimir; se mais
  de dois arquivos suprimirem a mesma regra, o problema é da regra.
- `compile-mql5` sai 0. Também roda sozinho, por hook, a cada `.mq5`/`.mqh`
  escrito — o comando acima é a rede para quando o hook não estiver ativo
  (sessão sem `.claude/settings.json` carregado, ou edição feita fora daqui).
  **Aviso reprova como erro.** Aviso de MQL5 costuma ser perda de dado em
  conversão, e este projeto converte preço.
- `pytest` roda **pelo menos um teste** e passa. `no tests ran` é verde que não
  distingue passou de não executou — o mesmo modo de falha do invariante 10, e
  vale tanto quanto suíte nenhuma.
- `git status` não mostra nenhum arquivo de dado.
- Sensor novo tem documento em `docs/sensors/`.

---

## Higiene de ferramenta

### Arquivo novo entra no Git no momento em que existe

Mesmo incompleto. Mesmo feio. Commit com `wip:` é sempre mais barato que arquivo
perdido. **Nada de trabalho untracked atravessando uma sessão.**

A regra não é teórica: um fonte MQL5 já existiu por horas apenas no disco, foi
apagado por um comando de terceiro durante uma sondagem, e o Git não tinha o que
restaurar. `git status` mostrando `??` é uma janela de perda aberta, não um
estado neutro.

Corolário — trabalho não versionado muda o que é seguro fazer. Agente que roda
experimento destrutivo vai para **worktree isolado**, nunca para o repositório
de trabalho. Se a tarefa envolve apagar, mover ou reescrever em massa, o
isolamento vem antes da tarefa, não depois do primeiro susto.

### Ferramenta de build nunca escolhe entre alternativas ambíguas

Para e pede. Duas instalações de compilador, dois terminais candidatos, dois
arquivos de configuração — a ferramenta enumera o que achou e recusa continuar.

O motivo é o invariante 9: dois PCs precisam produzir resultado idêntico. Uma
escolha automática é determinística no código e arbitrária na prática, porque
depende do que está instalado em cada máquina. O PC-Escritório escolheria um
compilador diferente do PC-Casa, em silêncio, e a divergência apareceria num
binário que não carrega — longe da causa.

Falha explícita custa trinta segundos, uma vez, para fixar a escolha em
configuração local. Escolha silenciosa custa uma investigação.

### O compilador do MetaEditor mente, e por isso não se chama ele direto

Medido nesta máquina, não deduzido da documentação. O código de saída **não é
status**: é a contagem de `.ex5` produzidos. Compilar limpo dá 1, compilar com
três erros dá **0**, e arquivo inexistente dá 0 sem gerar log nenhum. Um
`if errorlevel` comum reprova o build bom e aprova o quebrado.

Some com isso: há uma linha `Result:` por arquivo, não uma por execução;
compilar uma pasta sem `/inc` resolve include contra a pasta de instalação e
quebra todo `#include <RISER\...>`; compilar uma pasta **ignora `.mqh`**; e o
compilador não rastreia dependência de `.mqh`, então mexer num sensor não
recompila quem o inclui.

Esse último é o que mais importa aqui: pelo invariante 5 todo sensor mora em
`.mqh`, então a edição mais comum do repositório é justamente a que o
compilador não enxerga. Por isso `compile-mql5.ps1` apaga os `.ex5`, compila a
árvore inteira e verifica cada `.mqh` à parte. Não há modo incremental de
propósito — um atalho que às vezes não recompila é a forma mais confiável de
trazer o verde falso de volta.

---

## Trabalho em paralelo

Duas sessões usam `git worktree`, nunca a mesma pasta:

```bash
git worktree add ../RISER-dashboard feature/dashboard
```

**Particione por diretório, não por tarefa.** Se duas sessões precisam tocar
`mql5/Include/RISER/Core/`, o corte está errado — refaça o corte.

Só um worktree pode estar junctionado no terminal por vez. Para trocar:
`tools\setup-junctions.ps1 -Repo <caminho-do-worktree>`

---

## Modo debate

Quando a mensagem começar com `/debate`, ou pedir explicitamente para debater:

- Não editar nenhum arquivo. Nenhum. A saída é texto.
- Apresentar no mínimo duas alternativas com o trade-off de cada uma.
- Declarar discordância explicitamente quando houver. Concordar por educação é
  o pior resultado possível desta sessão.
- Apontar o que está faltando na proposta, não só avaliar o que está nela.
- Perguntar quando faltar informação, em vez de assumir.

Debate de arquitetura funciona melhor em sessão limpa, sem arquivos carregados.
Contexto cheio de código ancora o raciocínio em implementação.

---

## Não construa ainda

Explicitamente adiado para a camada 5. Se uma tarefa parecer exigir isso, ela
está fora de escopo:

licenciamento · instalador · interface de configuração · múltiplos idiomas ·
telemetria remota · dashboard web · compatibilidade entre versões ·
catálogo completo de erros · pacote de diagnóstico

Construir para usuários hipotéticos é a forma mais confiável de nunca terminar
nada. O usuário é o dono do projeto até o sistema estar validado em conta real.

---

## Decisões em aberto

Não assuma resposta para nenhuma destas. Pergunte.

1. **Margem compartilhada.** O EA vai rodar na mesma conta em que o dono opera à
   mão, separado por magic number — isso está decidido. O que não está: as
   posições manuais não têm stop e consomem margem de forma imprevisível, e
   podem liquidar as posições do EA sem que nada no EA tenha errado. O Guardian
   enxerga apenas o que é dele. Não resolvido, e não assumir resolução.
2. Coleta contínua de ticks: VPS, PC sempre ligado, ou aceitar lacunas?
3. BTCUSD opera 24/7 e quebra a normalização por horário do SVC. Assumir
   estrutura de sessão ou generalizar agora?
4. Backup do `RISER-data`. Dado de tick perdido é insubstituível.
5. Como distinguir **ausência esperada** de **feed morto**. Durante a pausa
   diária nenhum tick chega, nenhuma barra fecha e `freshness_ms` cresce sem
   parar — indistinguível de queda de conexão, e as duas pedem reações opostas:
   esperar numa, parar tudo na outra. Ver `docs/decisions/0008-*`.

---

## Ordem de trabalho

1. Fundação: junctions, gitignore, schema de log, coletor de ticks
2. Dashboard Trader v1 — **anotação e disponibilidade**, não captura
3. Harness de backtest com custo real + teste de paridade
4. Sensor de Volatilidade Curta (ver `docs/sensors/`)
5. Guardian + stop catastrófico
6. Motor de trailing por ticket
7. Reentradas e empilhamento

O item 2 vem cedo de propósito: enquanto o dono opera manualmente, cada dia sem
ele é um dia de dado que não se compra nem se simula. Mas o que se perde não é a
janela de ticks — é a **anotação**.

O coletor do item 1 roda no mesmo terminal em que ele opera. Logo o bruto já
está no disco de ponta a ponta, e qualquer recorte em torno de uma operação se
reconstrói dele. Gastar o v1 em captura de janela seria gravar duas vezes o que
já está gravado. O que o coletor **não** produz, e que desaparece para sempre se
não for registrado no instante:

- **Motivo, em dois botões: `técnico` ou `externo`.** Não é tag completa — é a
  divisão mínima que torna tratável a ambiguidade entre "o sensor está cego" e
  "ele agiu por algo que não está na série de preço". Sem ela, as duas hipóteses
  ficam indistinguíveis para sempre, e nenhum dado posterior as separa.
- **Disponibilidade: ele estava na mesa?** Sem isso, ausência de operação
  significa tanto "avaliou e não quis" quanto "estava dormindo", e a segunda é a
  maior parte do tempo. É o que transforma o silêncio dele — a única classe
  negativa abundante que existe — em dado utilizável.

**A janela de ±15 min não é dataset rotulado.** Ela vale, e o motivo é outro: é
o que permite calcular retroativamente qualquer sensor inventado no futuro sobre
operações já realizadas. Como dado de aprendizado ela não serve, por duas razões
independentes: sob "segurar até virar positivo" não existe classe "errei", e a
janela é centrada na entrada por construção — nenhuma amostra dela é negativa,
nem por acidente. A classe negativa, quando for necessária, sai do coletor
contínuo, e como ela é amostrada é escolha que determina o resultado.
