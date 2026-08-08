# 0005 — Ferramentas em ASCII puro, leitura com encoding explícito

2026-08-08 | Status: aceita

## Contexto

`tools/check-invariants.ps1` foi escrito com acentuação literal em dois lugares:
no texto de saída e, o que importa, dentro de um padrão de busca que reconhece
frases enunciando o invariante em vez de violando-o — `não pode`, `não deve`,
`só na borda`.

O PowerShell 5.1 lê `.ps1` sem BOM assumindo a codepage ANSI do sistema, e
`Get-Content` sem `-Encoding` faz o mesmo com os arquivos lidos. O resultado foi
que o padrão chegou corrompido ao motor de regex.

Não houve erro. Não houve aviso. O regex simplesmente deixou de casar.

O modo de falha é o pior possível para esta categoria de ferramenta: ela falha
**aberta**. Um verificador quebrado responde exatamente o mesmo que um
repositório limpo — nenhuma saída, exit 0. O sinal de sucesso e o sinal de
avaria são indistinguíveis.

Isto foi detetado por acaso, ao notar caracteres estranhos na saída de
diagnóstico. Sem esse detalhe cosmético, a ferramenta teria sido commitada
aparentemente funcional.

## Decisão

Vira o invariante 10 do `CLAUDE.md`.

1. Todo script em `tools/` é ASCII puro. Acentuação em padrão de busca entra por
   escape Unicode (`não pode`), nunca como caractere literal.
2. Toda leitura de arquivo declara o encoding: `Get-Content -Encoding UTF8`.
3. Ferramenta de verificação se testa contra caso **positivo** conhecido, não só
   contra o repositório limpo.

O ponto 3 é o que generaliza. Os pontos 1 e 2 tratam desta causa; o 3 trata da
classe. `check-invariants.ps1` foi verificado contra um repositório sintético
com um caso que deve acusar e um que não deve, para cada regra que ele
implementa.

## Consequências

Fica mais fácil: confiar no verde. Um verificador que já provou acusar o que
deve acusar torna o silêncio informativo.

Fica mais difícil: escrever ferramenta nova. Passa a exigir os casos de teste
junto, e a saída em ASCII fica menos agradável de ler em português.

Fica impossível: uma regra morrer em silêncio por corrupção de encoding sem que
o teste positivo denuncie.

Custo aceito: o texto de saída dos scripts perde acentuação.

## Alternativas descartadas

**Salvar os `.ps1` como UTF-8 com BOM.**
Resolveria a leitura do script pelo PowerShell 5.1 e permitiria acentuação
natural. O que teria acontecido: funcionaria neste PC e continuaria frágil. O
BOM é invisível no editor e sobrevive mal a operações rotineiras — um script
regenerado, um copiar-colar entre editores, uma normalização de fim de linha do
Git, e o BOM some sem que nada indique. A falha voltaria com a mesma assinatura
de antes, e desta vez com a suposição de que o problema já tinha sido resolvido,
o que torna o diagnóstico mais lento, não mais rápido. ASCII puro não tem estado
oculto para perder.

**Corrigir só o encoding e não acrescentar o teste positivo.**
O que teria acontecido: esta ocorrência específica ficaria resolvida e a classe
inteira continuaria aberta. Qualquer regra futura que deixe de casar — por regex
mal escrito, por caminho que mudou, por arquivo que passou a não ser varrido —
produziria o mesmo silêncio tranquilizador. A ferramenta acumularia regras
mortas ao longo dos meses, cada uma passando a impressão de cobertura que não
existe, e a descoberta viria pelo caminho caro: um documento contradizendo um
invariante em produção, com o verificador verde ao lado.

**Reescrever a ferramenta em Python para evitar o problema de encoding do
PowerShell.**
Python 3 lê UTF-8 por padrão e a classe de bug desapareceria. Descartada pelo
motivo já registado na escolha original da linguagem: o verificador precisa
rodar quando o ambiente Python está quebrado, que é justamente quando ele é mais
necessário — e o invariante 9 exige instalação por lock, o que acoplaria a
ferramenta ao lock que ela deveria ajudar a proteger. Trocar um modo de falha
conhecido e agora testado por uma dependência de ambiente foi considerado pior
negócio.
