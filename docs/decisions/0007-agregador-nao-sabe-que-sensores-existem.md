# 0007 — Agregador não sabe que sensores existem

2026-08-08 | Status: aceita

## Contexto

Ao implementar `bars.py`, a pergunta que surgiu foi onde calcular `base_rg` — a
referência de dimensionamento em USD por onça de que o Motor de Trailing depende
(ADR 0004) e que o critério 8 do SVC mede.

A tentação era natural: o agregador já percorre os ticks, já calcula máximo e
mínimo por intervalo, e `base_rg` é uma mediana de ranges. Parecia trabalho
duplicado não aproveitar a passagem.

É acoplamento disfarçado de reuso.

`base_rg` é a mediana móvel do componente `rg` **do SVC**, por minuto do dia,
sobre N dias de sessão. Cada um desses termos é parâmetro do sensor: a janela do
`rg` é rolante em segundos, e N é uma decisão em aberto do próprio documento do
SVC. Nada disso é propriedade de uma barra.

## Decisão

O agregador não conhece nenhum sensor. Ele transforma ticks em barras e para
aí.

O que ele expõe é matéria-prima: `range_usd_oz` — o `high - low` de cada barra,
em USD por onça — como campo normal da barra, igual a `open` ou `close`. Serve a
qualquer consumidor e não presume nenhum.

`base_rg` é calculado pelo SVC, a partir dos **ticks**, na janela do SVC. O
sensor não deriva sua estatística da grade de barras.

Regra geral: módulo de infraestrutura que precisa conhecer um consumidor
específico está na camada errada. Se `bars.py` precisa importar, nomear ou
parametrizar algo do SVC, o corte está errado — refaça o corte.

## Consequências

Fica mais fácil: testar qualquer sensor isoladamente, porque a entrada dele é
tick e a saída não depende de nada que o agregador tenha decidido. E fica mais
fácil trocar o agregador sem tocar em sensor nenhum — o que importa, porque ele
é a referência da futura versão MQL5 e vai ser reescrito lá.

Fica mais difícil: nada de relevante. O SVC percorre os ticks de qualquer forma,
já que a janela dele é rolante em segundos e não se alinha à grade de barras. Se
lesse `base_rg` do agregador, teria de escolher uma grade, e a secção 2.2 do
documento do sensor é explícita em que balde fixo não alimenta o `value`.

Fica impossível: o agregador virar o lugar onde toda estatística mora.

## Alternativas descartadas

**Calcular `base_rg` dentro de `bars.py`.**
O que teria acontecido: funcionaria para o SVC, e essa é a armadilha — o
primeiro caso sempre funciona. O problema é o segundo. Um Sensor de
Direcionalidade precisaria da mediana do `er`; o Guardian precisaria de um
percentil de spread por hora; o dimensionamento de posição precisaria de outra
janela. Cada um desses é uma linha a mais no agregador, cada uma defensável
isoladamente pelo mesmo argumento de "ele já percorre os ticks", e o resultado
é o módulo onde toda estatística do sistema mora.

A partir daí nenhum sensor é testável isoladamente: para testar o SVC seria
preciso construir barras, e para construir barras seria preciso o código que
calcula a estatística de três outros sensores. O agregador também deixaria de
ser portável para MQL5 sem arrastar junto a lógica de todos eles — e a paridade
Python↔MQL5 é o critério de saída da Camada 1.

Pior: a janela do SVC ficaria presa à grade de barras. O desenho é rolante em
segundos justamente porque balde fixo parte uma explosão que atravessa a
fronteira em duas metades mortas, e o sensor lê "calmo" duas vezes seguidas num
momento de pico. Ler `base_rg` de uma grade M1 reintroduziria exatamente o
defeito que a secção 2.2 do SVC existe para evitar — e de forma invisível, já
que o número continuaria saindo.

**Um módulo intermediário de "estatísticas de mercado", entre barras e
sensores.**
Resolveria o acoplamento direto e criaria um pior: o lugar onde tudo que não
cabe em nenhum lado acaba. Sem um critério para decidir o que entra, ele vira o
agregador inchado com outro nome. Descartada por não ter regra de admissão que
se sustente — e a regra "o sensor calcula o que é do sensor" já resolve.

**`bars.py` expor um gancho genérico para o consumidor injetar a estatística.**
Manteria o agregador ignorante quanto a sensores específicos, tecnicamente. Na
prática, o gancho seria chamado uma vez por barra, o que já fixa a grade — o
defeito principal continuaria, agora escondido atrás de uma abstração que dá a
impressão de ter sido resolvido.
