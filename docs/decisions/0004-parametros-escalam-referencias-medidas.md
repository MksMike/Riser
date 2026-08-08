# 0004 — Parâmetros escalam referências medidas, não constantes

2026-08-08 | Status: aceita

## Contexto

O Motor de Trailing dimensionava o degrau da escada assim:

```
# invariant-ok: UNITS_POINTS cita a forma anterior, que esta ADR substitui
degrau_pontos = degrau_base × (1 + k × value)     <- forma anterior
```

O verificador de invariantes acusou a unidade: `pontos` é unidade de corretora
dentro de um documento de sensor, o que o invariante 2 não permite. Mas a
unidade era sintoma.

O problema real são `degrau_base` e `k`: dois parâmetros livres fazendo o
trabalho de um. `degrau_base` é uma constante em unidade de corretora, e o
único jeito de descobrir o seu valor é calibrar contra um feed específico. Toda
corretora nova exige recalibração manual, e nada no sistema indica quando ela
deixou de valer.

O mesmo movimento aparece no stop catastrófico, que já é definido por um limite
de perda em JPY — uma quantidade medida — em vez de uma distância fixa. E vai
reaparecer no Guardian e no dimensionamento de posição.

## Decisão

**Parâmetro de configuração é adimensional e multiplica uma referência medida
no feed local. Não multiplica constante.**

Aplicado ao Motor de Trailing:

```
degrau_usd_oz = k × range_tipico_do_horario(m)
```

`range_tipico_do_horario(m)` é `base_rg_usd_oz(m)` — a mediana móvel do
componente `rg` bruto, em USD por onça, para aquele minuto do dia, sobre a
mesma janela de N dias que o SVC já usa para normalizar. Logada como `base_rg`.

Isto obrigou o SVC a manter **duas** baselines: `baseline(m)`, adimensional,
que normaliza o sensor; e `base_rg_usd_oz(m)`, em unidade física, que os
consumidores usam para dimensionar distância. A primeira não serve para a
segunda função, por ser adimensional por construção.

Regra geral, para quando o padrão reaparecer:

- Se um parâmetro tem unidade, ele é candidato a estar errado. Procure a
  quantidade medida que ele está substituindo.
- A referência é medida **no feed onde o sistema está operando**, não importada
  de outro.
- A referência medida é logada junto com a leitura. Sem isso, o parâmetro
  efetivo de uma operação passada não é reconstituível.

## Correção durante a redação

A primeira formulação desta decisão usava o campo `base` como referência:

```
degrau = k × base
```

Falha dimensionalmente. `base` é a mediana móvel de `value_bruto`, que compõe
componentes já normalizados para 0..1 — é adimensional. O produto `k × base`
é adimensional e estava a ser tratado como distância de preço.

O erro foi corrigido antes de qualquer implementação, trocando a referência
para `base_rg_usd_oz(m)`: a mediana do componente `rg` **bruto**, guardada
antes da normalização, em USD por onça.

Registado aqui porque é a parte que dá para errar de novo, e porque revela um
limite do próprio padrão:

**Escalar uma referência medida não protege sozinho. A referência precisa estar
na unidade certa — e uma referência normalizada deixa de estar.**

Normalizar é justamente destruir a unidade. Um campo que passou por
normalização parece uma quantidade medida, tem procedência de quantidade
medida, e satisfaz a leitura superficial do padrão. Não serve para dimensionar
nada físico.

Ao aplicar este padrão, a pergunta de verificação não é "isto foi medido?" mas
**"em que unidade isto está, e a conta fecha?"**. Se o parâmetro é adimensional
por construção, a referência tem de carregar a unidade inteira do resultado.

Este modo de falha não produz exceção nem valor absurdo: produz um número
plausível, sistematicamente errado, que muda de significado a cada alteração na
composição dos componentes — e que a normalização por horário mascara até a
comparação cross-feed. Passa em teste unitário. Aparece em dinheiro real.

O critério 8 do SVC existe por causa disto.

## Consequências

Fica mais fácil: levar o sistema a uma corretora nova. Não há recalibração — a
referência se mede sozinha a partir do feed local, e o mesmo `k` vale nos dois
lugares. Isto ataca diretamente o critério 7 do SVC (robustez cross-feed), que
é o critério mais severo do sensor.

Fica mais fácil também: interpretar o parâmetro. `k = 0,8` significa "degrau de
80% do range típico daquele horário" em qualquer corretora e em qualquer hora
do dia. `degrau_base = 240` não significava nada sem saber qual corretora, qual
dígito e qual horário.

Fica mais difícil: o arranque a frio. A referência precisa da baseline aquecida
— N dias de sessão. Antes disso não há degrau confiável, e o consumidor tem de
tratar a leitura como ausente, exatamente como já manda a secção *Como não
usar* do SVC para `confidence` baixo. Uma constante estava disponível no
primeiro tick; a referência medida não está.

Fica mais difícil também: o sensor carrega uma segunda estrutura incremental na
memória, e o orçamento de 200 µs por atualização passa a incluí-la.

Fica impossível: um valor calibrado numa corretora vazar silenciosamente para
outra.

## Alternativas descartadas

**Manter `degrau_base` e só renomear a unidade para `degrau_usd_oz`.**
Era a correção mínima, e conserta o invariante 2 sem tocar no problema. O que
teria acontecido: o documento passaria no verificador, e a constante continuaria
ali. Cada corretora nova exigiria uma sessão de recalibração manual do
`degrau_base` — trabalho que ninguém agenda e que só se descobre necessário
quando o trailing começa a encerrar posições cedo demais num feed novo. Pior é o
efeito de médio prazo: seis meses depois, o valor calibrado fica
indistinguível de um valor arbitrário. Ninguém lembra contra que amostra, que
corretora e que período ele foi obtido, e como não há nada que o invalide
automaticamente, ele sobrevive por inércia e passa a ser tratado como se fosse
uma propriedade do mercado. Foi por isso que a correção mínima foi recusada.

**Usar o campo `base` que já existe, em vez de criar `base_rg_usd_oz`.**
Era a leitura natural de "usar a baseline que o SVC já calcula", e não fecha:
`base` é a mediana de `value_bruto`, que compõe componentes já normalizados
para 0..1. É adimensional. O que teria acontecido: `k × base` produziria um
número adimensional tratado como distância de preço. O erro não apareceria como
falha — apareceria como um degrau numericamente plausível e sistematicamente
errado, que mudaria de significado a cada mudança na composição dos
componentes, e que a normalização por horário mascararia até a comparação
cross-feed. É a família de bug que passa em teste unitário e só se manifesta em
dinheiro real.

**Derivar o degrau do ATR macro em vez do range da janela curta.**
O ATR já está disponível como componente `atr` e é uma quantidade medida, então
satisfaria o padrão. Descartada por escala temporal: o ATR de M15 responde em
dezenas de minutos, e o trailing de uma operação de scalp vive segundos a
minutos. O que teria acontecido: o degrau ficaria correto na média do dia e
errado exatamente nos momentos que importam — a explosão de abertura e a
divulgação, onde a distância certa muda antes de o ATR se mover. Continua
candidato para o Guardian, cuja decisão é de regime e não de tick.
