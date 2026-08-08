<!-- invariant-ok-file: CROSS_FILE_COUNT este documento descreve o antipadrao e
     precisa de o citar; as contagens abaixo sao exemplos, nao afirmacoes -->

# 0006 — Documento normativo não carrega contagem de conteúdo alheio

2026-08-08 | Status: aceita

## Contexto

O `CLAUDE.md` definia o critério de saída da Camada 2 como *"Critérios 1–8 do
formato SVC cumpridos e registrados"*. O documento do SVC tinha sete critérios.

Ao acrescentar um critério novo, a discrepância apareceu, e foi resolvida
numerando-o como 8 — tratando o `CLAUDE.md` como fonte de verdade, por ser o
documento normativo.

**O processo estava certo e o resultado foi acidental.** O que houve foi um erro
de contagem ao escrever "1–8", e o erro passou a valer por estar num documento
com autoridade. Funcionou porque a lacuna era de exatamente um e o critério novo
a preencheu. Se estivesse escrito "1–12", o mesmo raciocínio, aplicado com o
mesmo rigor, teria aberto um buraco de quatro.

A lição não é sobre o número. Um contador dentro de um documento normativo é uma
**afirmação factual sobre outro documento**, e afirmação factual apodrece. O
documento normativo deve dizer o que vale, não quantos são.

## Decisão

Documento normativo — `CLAUDE.md`, schemas, manifestos — não contém contagem,
intervalo numerado nem referência a "N itens" que aponte para conteúdo vivo em
outro arquivo. Diz o que vale e aponta para onde está.

```
antes   Critérios 1–8 do formato SVC cumpridos e registrados
depois  todos os critérios de aceitação do formato SVC (ver docs/sensors/)
        cumpridos e registrados
```

O documento do sensor passa a ser a única fonte de quantos são, e os dois não
têm como discordar.

### A fronteira é a distância, não o arquivo

O limite de arquivo é onde o problema aparece primeiro, não onde ele está. A
fronteira relevante é entre a **contagem** e **o que ela conta** — e um arquivo
longo tem fronteiras internas.

O documento do SVC dizia, na secção 7, *"com os cinco critérios medidos"*, com
colunas `Crit. 1` a `Crit. 5`. A secção 4, trinta linhas acima, tinha oito. A
contagem e o contado estavam no mesmo arquivo e mesmo assim divergiram, porque
ninguém que edita a tabela de aceitação rola até a tabela de registo.

Regra editorial: se a contagem não está à vista do que conta, ela vai apodrecer.
Aponte em vez de contar — *"todos os critérios da secção 4"*.

Isto **não** vira regra do verificador. Uma heurística intra-arquivo teria de
acusar toda contagem que não estivesse adjacente ao seu referente, e o ruído
inviabilizaria a ferramenta. Fica como princípio de escrita, verificado por
quem lê.

### Contagem que descreve × contagem que deriva

Nem todo número é uma afirmação sobre conteúdo. A secção 2.5 do SVC diz:

```
Variar: W ∈ {10s, 20s, 30s, 60s} × micro ∈ {tr, rg, tr+rg, rg+er, tr+rg+er}
→ 20 testes.
```

O `20` não descreve nada: **deriva** da expressão na linha imediatamente
anterior, e existe para que o leitor confira de cabeça que a conta fecha —
4 × 5 = 20. O mesmo vale para o "Total: 32 testes", que soma 20 + 3 + 9.

| tipo | exemplo | apodrece? |
|---|---|---|
| descreve conteúdo | "critérios 1–8 do formato SVC" | sim — o conteúdo muda sem avisar |
| deriva de expressão adjacente | "4 × 5 → 20 testes" | não — o erro é visível no lugar |

Uma contagem derivada é verificável onde está. Se a lista de variantes mudar, a
multiplicação na mesma linha muda junto, e um `20` obsoleto ao lado de um
`4 × 6` é imediatamente errado para quem lê. Estas ficam.

Numeração de um documento sobre si mesmo também continua legítima:
`docs/sensors/` pode dizer "critérios 1–4 mantidos em ambos os feeds" porque os
critérios vivem ali. O que não se faz é afirmar de fora quantos itens outro
arquivo tem.

Aplicado também a `tools/check-invariants.ps1` e à ADR 0005, que diziam "os
quatro modos de falha" e "as quatro regras" — contagens sobre um arquivo que a
própria adição da regra `CROSS_FILE_COUNT` invalidou no mesmo dia.

A regra `CROSS_FILE_COUNT` do verificador cobre o caso: acusa contagem ou
intervalo numerado quando há referência a arquivo alheio na linha ou nas duas
anteriores.

## Consequências

Fica mais fácil: mudar o número de itens de qualquer lista. Acrescentar um
critério ao SVC deixou de exigir uma edição correspondente no `CLAUDE.md`.

Fica mais difícil: saber de relance quantos critérios existem sem abrir o
documento do sensor. É o custo, e é pequeno perto do modo de falha que evita.

Fica impossível: um contador errado num documento com autoridade forçar a
criação de conteúdo para satisfazê-lo.

## Alternativas descartadas

**Manter a contagem e mantê-la sincronizada por disciplina.**
O que teria acontecido no caso concreto, se o `CLAUDE.md` dissesse "1–12": ao
notar a lacuna, o caminho consistente com o processo — documento normativo vence
— seria criar quatro critérios de aceitação para preenchê-la. Não critérios
descobertos por necessidade, mas inventados para satisfazer um contador. E o
documento do SVC é explícito em que os alvos numéricos se fixam **antes** da
Etapa A e não podem ser afrouxados depois. Os quatro critérios inventados
nasceriam com alvo numérico fixado e estatuto de não afrouxável, e passariam a
bloquear a promoção do sensor a `v1.0.0` por uma exigência cuja origem é um erro
de digitação. Meses depois, ninguém saberia distinguir os quatro inventados dos
oito legítimos — todos têm a mesma forma, o mesmo estatuto e a mesma autoridade.

**Deixar a contagem só no documento do sensor e nenhuma no `CLAUDE.md`.**
É o que foi feito, e não é bem uma alternativa descartada: é a decisão. Fica
registrado para deixar claro o que *não* se fez, que seria remover a referência
inteira. O `CLAUDE.md` continua exigindo os critérios e apontando onde vivem —
só deixou de afirmar quantos são. Remover a referência inteira teria custado o
elo entre a Camada 2 e o documento que a define.

**Gerar a linha do `CLAUDE.md` a partir do documento do sensor.**
Um script contaria as linhas da tabela de critérios e reescreveria a célula.
Descartada por desproporção: introduz um passo de geração e um arquivo derivado
versionado para resolver um problema que desaparece ao não afirmar o número.
Também trocaria uma falha silenciosa por outra — a linha ficaria correta até
alguém editar o `CLAUDE.md` à mão sem rodar o gerador.
