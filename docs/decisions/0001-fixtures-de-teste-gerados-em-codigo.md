# 0001 — Fixtures de teste gerados em código, não versionados como arquivo

2026-08-08 | Status: aceita

## Contexto

O `.gitignore` ignora `*.parquet`, `*.jsonl` e `*.csv` em qualquer nível, para
manter o invariante 7 (dado nunca entra no Git). Isso também bloqueia
`python/tests/fixtures/`, e os testes do agregador de barras precisam de casos
de fronteira — entre eles a pausa diária 20:58–22:00 e a abertura de domingo
às 22:01.

Havia duas saídas: abrir exceção no `.gitignore` para o diretório de fixtures,
ou gerar os fixtures em código dentro do próprio teste.

## Decisão

Fixtures gerados em código. Nenhuma exceção no `.gitignore`.

O teste constrói a série de ticks que precisa, a partir da configuração
efetiva, e compara contra o resultado esperado calculado no próprio teste.

## Consequências

Fica mais fácil: o diff de um teste mostra o caso de fronteira em texto legível,
não um binário opaco; o invariante 7 continua sem exceção, e exceção em regra de
ignore é o começo de toda erosão de `.gitignore`.

Fica mais difícil: amostras grandes ou capturadas do feed real não podem ser
congeladas no repositório. Quando forem necessárias, vivem em
`C:\dev\RISER-data` e o teste que depende delas é marcado para pular quando o
arquivo não existir — nunca falhar silenciosamente por ausência de dado.

Fica impossível: congelar por acidente uma suposição não medida dentro de um
arquivo binário que ninguém revisa.

## Alternativas descartadas

**Exceção `!python/tests/fixtures/**` no `.gitignore`.** Descartada pelo motivo
decisivo: o caso de fronteira que mais importa é a pausa 20:58–22:00, cujo
instante real depende de `server.timezone_offset`, hoje marcado `VERIFICAR` nos
dois manifestos de `config/brokers/`. Um fixture em arquivo congelaria esse
offset como se fosse fato. Gerado em código, ele lê o offset da config e se
ajusta sozinho quando o valor for medido — e os testes que dependiam da
suposição errada passam a falhar, que é exatamente o comportamento desejado.

**Fixtures em CSV com exceção só para CSV.** Mesmo defeito da anterior, com o
agravante de que CSV perde o tipo de `ts_utc` e reintroduz ambiguidade de fuso
na fronteira do teste — justamente onde o teste existe para ser exato.
