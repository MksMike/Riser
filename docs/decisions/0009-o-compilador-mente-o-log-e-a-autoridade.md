# 0009 — O compilador do MetaEditor mente; o log é a autoridade

2026-08-08 | Status: aceita

## Contexto

Compilar MQL5 fora do editor gráfico é `MetaEditor64.exe /compile:<alvo>`. A
forma óbvia de embrulhar isso num script é olhar o código de saída, como se faz
com qualquer compilador. Essa forma óbvia está errada de um jeito que não se
deduz de documentação nenhuma, e o modo de falha é o pior possível.

Quatro comportamentos, todos **medidos** contra o MetaEditor64 desta máquina, em
duas instalações independentes (builds 6090 e 6093), não inferidos.

### 1. O código de saída não é status — é a contagem de binários gerados

| situação | código de saída | log |
|---|---|---|
| compila limpo | **1** | `Result: 0 errors, 0 warnings` |
| compila com aviso | **1** | `Result: 0 errors, 1 warnings` |
| três erros de sintaxe | **0** | `Result: 3 errors, 0 warnings` |
| arquivo inexistente | **0** | *nenhum log é escrito* |
| pasta, dois de quatro compilaram | **2** | quatro linhas `Result:` |

O padrão é `.ex5` produzidos. Consequência direta: `if ($LASTEXITCODE -ne 0)`
**reprova o build limpo e aprova o build com erro**. Um erro de sintaxe passa
como sucesso, e um arquivo com o nome errado no caminho passa como sucesso duas
vezes — sem log e sem código.

### 2. Há uma linha `Result:` por arquivo, não uma por execução

Compilar uma pasta com quatro arquivos escreve quatro linhas. Um parser que leia
a última reporta o estado do último arquivo e engole tudo que veio antes. Na
medição, a última linha dizia `0 errors, 1 warnings` enquanto quatro erros
tinham sido reportados acima.

### 3. A raiz de include muda conforme o modo, e num deles não existe

| invocação | onde `#include <...>` é procurado |
|---|---|
| arquivo único, sem `/inc` | pasta de dados do terminal — resolve pela junction |
| **pasta, sem `/inc`** | pasta de **instalação** do terminal |
| qualquer modo, com `/inc` | o que foi mandado |

A pasta de instalação não tem `Include\` nesta máquina. Logo, compilar a árvore
inteira sem `/inc` quebra todo include do projeto com `error 106: file not
found` — enquanto compilar o mesmo arquivo sozinho funciona. O sintoma aparece e
desaparece conforme o alvo, o que é o pior formato para um bug.

### 4. Não há rastreio de dependência de `.mqh`, e compilar pasta ignora `.mqh`

Alterar um `.mqh` **não** recompila os `.mq5` que o incluem. E compilar uma
pasta não verifica os `.mqh` sequer uma vez: a passagem simplesmente não os
enxerga.

Este é o mais caro dos quatro **para este projeto especificamente**. Pelo
invariante 5, todo sensor mora em `mql5/Include/RISER/Sensors/` como `.mqh`,
consumido pelo EA e pelo indicador. Ou seja: a edição mais frequente do
repositório é exatamente aquela que o compilador não enxerga. Um build
incremental responderia "sem erros" tendo recompilado nada.

Os quatro compartilham uma assinatura: **falham abertos**. Nenhum produz erro,
aviso ou saída anômala. Todos produzem verde. É a mesma classe da ADR 0005, do
`ask`/`bid` trocado no `.bi5` e do mês com base zero da Dukascopy — o processo
segue, o resultado parece utilizável, e a descoberta vem meses depois por um
caminho caro.

## Decisão

`tools/compile-mql5.ps1` é o único ponto de entrada para compilar MQL5. Nada
chama `MetaEditor64.exe` diretamente — nem script, nem hook, nem pessoa.

1. **O código de saída do MetaEditor nunca é consultado.** A autoridade é o log,
   lido com `-Encoding` explícito.
2. **Todas as linhas `Result:` são somadas**, e a soma é conferida contra a
   contagem de linhas de diagnóstico. Discordância entre as duas vale o **maior**
   dos números, com aviso visível: reprovar por engano é recuperável em minutos,
   aprovar por engano é a falha que este documento existe para evitar.
3. **`/inc` é sempre passado**, apontando para `mql5` do próprio repositório —
   nunca para a junction do terminal. Além de tornar o modo irrelevante, isto
   resolve o caso do worktree: a junction aponta para um worktree por vez, e
   compilar por ela compilaria o código do worktree errado sem nenhum sintoma.
4. **O build é sempre completo**: os `.ex5` são apagados antes, e cada `.mqh` é
   verificado à parte, porque nada mais o faz. **Não existe modo incremental**, e
   a ausência é deliberada.
5. **Log com conteúdo e nada reconhecível reprova.** "Não entendi o log" jamais
   é reportado como "zero erros".
6. Aviso reprova como erro, salvo `-AllowWarnings` explícito.

O autoteste (`-SelfTest`) exercita cada um destes contra caso positivo
conhecido, incluindo o formato distinto que o `.mqh` verificado sozinho produz
(`information: result N errors`) e a confirmação empírica de que compilar a
pasta ignora `.mqh` — a asserção que justifica a passagem individual existir.

## Consequências

Fica mais fácil: confiar no verde do build, e escrever sensor em `.mqh` sabendo
que alguém o compila.

Fica mais difícil: builds grandes. Recompilar tudo a cada edição é linear no
tamanho da árvore. Medido hoje: cerca de um segundo. Quando isso incomodar, a
resposta é medir de novo e decidir com número — não reintroduzir incremental por
conforto.

Fica impossível: um `.mqh` alterado passar sem ser compilado; um erro de sintaxe
sair como sucesso; um include quebrado aparecer só na máquina de outra pessoa.

Custo aceito: cerca de um segundo por edição de fonte MQL5, e a perda dos
binários intermediários a cada build.

## Alternativas descartadas

**Confiar no código de saída, como com qualquer outro compilador.**
É o que qualquer pessoa escreveria primeiro, e é por isso que este documento
começa por aí. O que teria acontecido: o portão automático aprovaria todo commit
com erro de sintaxe e reprovaria os limpos. A reação natural a um portão que
reprova build bom é desligar o portão — e nesse ponto o repositório fica sem
verificação nenhuma, tendo passado pela experiência de "já tentamos isso, não
funciona".

**Deixar o MetaEditor decidir o que recompilar.**
Seria mais rápido e é o comportamento padrão dele. O que teria acontecido: como
não há rastreio de dependência de `.mqh`, alterar um sensor produziria "0 erros"
sem ter recompilado o EA que o consome. O sensor quebrado só apareceria quando
alguém apagasse o `.ex5` à mão, ou num outro PC — longe da edição que o causou.
O ganho de tempo é real e pequeno; o custo é uma classe inteira de falha
silenciosa na parte mais movimentada do repositório.

**Compilar pelo caminho da junction, dentro da pasta do terminal.**
Funciona, e tem a vantagem de ser o caminho que o terminal realmente usa. O que
teria acontecido: com dois worktrees, apenas um está junctionado por vez, e o
build de uma sessão compilaria os fontes da outra sem que nada indicasse.
Também tornaria a compilação dependente de as junctions estarem montadas, o que
transforma um problema de instalação num erro de compilação enganoso.

**Ler apenas a última linha `Result:`.**
Mais simples de escrever e correto para arquivo único, que é como se testa
primeiro. O que teria acontecido: ao passar a compilar a árvore inteira — que é
o modo necessário pelo ponto 4 — o resultado passaria a descrever só o último
arquivo em ordem alfabética. Um erro em `Guardian.mq5` ficaria invisível porque
`Trailing.mq5` compilou depois e compilou limpo.

**Escolher um MetaEditor automaticamente quando há mais de um instalado.**
Ver a regra em `CLAUDE.md`, seção Higiene de ferramenta: ferramenta de build não
escolhe entre alternativas ambíguas. Duas instalações compilam com builds
diferentes, e um `.ex5` gerado por build mais novo pode não carregar num
terminal mais antigo. A escolha silenciosa custaria uma investigação; a falha
explícita custa trinta segundos, uma vez.
