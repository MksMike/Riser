# Notas de laboratório

Observações a verificar contra dado real. **Nada aqui é decisão.** O que virar
decisão sai daqui e vira ADR em `docs/decisions/`; o que for descartado fica,
com o motivo, para não ser reinventado.

---

## 2026-08-08 — Overlap de 72h pode não cobrir feriado prolongado

**Status:** a verificar quando os 2 anos estiverem no disco. Nada alterado.

`ticks.read_month_with_overlap` usa 72 horas por padrão. O número foi escolhido
para cobrir a virada de mês que cai em fim de semana: sexta ~21:00 UTC até
domingo ~22:00 UTC são cerca de 49 horas, e 72 dá folga.

**A dúvida:** Natal e Ano Novo podem esticar além disso em alguns anos. Se o
mercado fechar na véspera e só reabrir depois de um feriado emendado com fim de
semana, o silêncio pode passar de 72h — e nesse caso a última barra de dezembro
e a de janeiro voltariam a ser descartadas pela regra 2 do agregador.

**Por que importa mais do que parece:** dezembro e janeiro são exatamente as
viradas que mais se quer íntegras, porque delimitam o ano. Um sensor com
componente sazonal avaliado por ano teria a fronteira do período de avaliação
justamente no ponto com dado faltando.

**Como verificar, com dado real:**

1. Para cada virada de mês dos 2 anos, medir o intervalo entre o último tick do
   mês e o primeiro do mês seguinte.
2. Listar as viradas cujo intervalo passa de 72h.
3. Confirmar se a última barra desses meses aparece no agregado.

Suspeitas óbvias: 2024-12 → 2025-01 e 2025-12 → 2026-01.

**Por que não mudar agora:** aumentar o padrão por especulação trocaria um
número medido por um número inventado, e leria mais dado do que o necessário em
todas as outras 22 viradas. Se a medição mostrar que 72h não basta, o número
sai da medição — e o candidato natural é derivar o overlap do maior silêncio
observado no histórico, em vez de fixar constante.

Relacionado: ADR 0008, que trata do mesmo silêncio prolongado sob outra ótica —
distinguir ausência esperada de feed morto.

---

## 2026-08-08 — Baseline aleatório casado: implementado, ainda não rodado

**Status:** `baseline_casado.py` existe e passa o autoteste. **Nenhum resultado
sobre dado real.** Faltam as duas entradas, e nenhuma delas se inventa.

Decide a decisão em aberto 1 do CLAUDE.md antes que ela seja fechada por
opinião: as entradas manuais têm edge de *timing*, ou o resultado vem de capital
e paciência?

**Por que MAE e tempo-até-verde, e não lucro.** Sob "segurar até virar positivo"
a taxa de acerto é ~100% por construção. O dinheiro não distingue entrada boa de
entrada qualquer — quase toda entrada acaba no verde se houver capital e
paciência bastante. O que distingue é o *preço da espera*: quanto o mercado
andou contra, quanto tempo levou, e a fração que não virou dentro de um teto.

**Os três casamentos rodam sempre.** Aleatório puro, casado por dia-da-semana e
hora, e casado por hora mais decil de range pré-entrada. Escolher um seria
escolher a resposta: sortear no dia inteiro compara a entrada dele com a média
do dia, inclusive horas em que ele nunca opera, e nesse desenho qualquer trader
pareceria bom. A divergência entre os três **é** o resultado. Se o edge some ao
casar por horário, o edge era horário. Se some ao casar por volatilidade, o edge
era escolher regime — o que continua sendo edge, mas mora em outro sensor.

**O que falta para rodar:**

1. Histórico de deals exportado, com timestamp de entrada e direção, em
   `RISER-data` (CSV com `ts_utc,side`). Quantas operações existem e desde
   quando é pergunta aberta.
2. Ticks ingeridos cobrindo o período das operações mais uma semana à frente,
   pelo horizonte máximo.
3. `--rollover-hour-utc`, que não tem padrão de propósito: `server.timezone_offset`
   ainda está VERIFICAR nos manifestos, e para posição de horas essa hora decide
   entre pagar swap e não pagar nenhum.

**Autoteste, e por que ele não é opcional numa pasta de exploração.** O estudo
existe para produzir um número em que se vai acreditar. Testar contra
repositório limpo não distingue "não há achado" de "não consigo achar" — é o
invariante 10 um degrau acima. Por isso `--self-test` roda contra caso positivo
conhecido: séries com MAE calculável à mão, contagem de noites de swap com dia
triplo e fim de semana, e dois controles de ponta a ponta —

- **negativo:** passeio aleatório com entradas aleatórias tem de dar P ≈ 0,50.
  Longe disso, o viés está no sorteio, e qualquer resultado sobre dado real
  estaria contaminado pelo mesmo viés.
- **positivo:** a mesma série com movimento favorável plantado logo após cada
  entrada. A rampa sobe rápido e volta ao ponto de partida, para não mexer no
  range pré-entrada nem no nível médio — assim nenhum dos três casamentos
  consegue absorvê-la. Se a ferramenta não acusar *isso*, não acusaria nada.

Três achados vieram do próprio autoteste, e valem registro porque nenhum deles
aparece na leitura do código:

- `.asi8` devolve o inteiro na unidade do índice, e o pandas 2 tem datetime de
  milissegundo tanto quanto de nanossegundo. Misturar as duas não levanta erro:
  produz horizontes um milhão de vezes menores.
- `falhas += _controles(...)` lê `falhas` **antes** de chamar a função e grava o
  valor velho por cima do que ela incrementou. O autoteste saía verde com falha
  na lista — o pior desfecho possível para uma ferramenta de verificação.
- O MAE tem piso no spread: comprado entra no ask e mede o bid. Real e sintética
  empilham no mesmo piso, empate conta meio, e P não chega perto de 1,00 por
  mais forte que seja o edge. O limiar do controle positivo é direcional por
  causa disso, não por frouxidão.

**Empilhamento: medido, não perguntado.** Entradas na mesma direção dentro de um
gap formam um episódio; inverter abre episódio novo, porque inverter não é
empilhar. O relatório traz **N nominal e N efetivo** separados — a diferença
entre os dois *é* a medida do empilhamento, e não depende de ninguém lembrar
como opera. A contagem sai em vários gaps (5, 15, 60, 240 min) porque a escala
de tempo do empilhamento também é desconhecida.

O IC95 reamostra **episódio**, não entrada. Isto não é cosmético: no controle do
autoteste, com 20 episódios de 5 entradas, o intervalo por entrada sai com
metade da largura do intervalo por episódio. Reamostrar entrada a entrada
supõe independência que não existe e devolve falsa precisão — que é pior que
imprecisão, porque convida a concluir. Quando não há empilhamento, cada episódio
tem uma entrada e a fórmula degenera no bootstrap comum; não há custo em usá-la
sempre.

---

## 2026-08-08 — MAE como métrica desbloqueia backtest sem stop

**Status:** vale ADR, **depois** que o baseline rodar. Antes disso seria decidir
sobre um método cuja utilidade ainda não foi demonstrada em dado real.

O argumento antigo era que operar sem stop torna backtest inviável, por falta de
saída definida. Isso vale para métrica de *resultado*: sem regra de saída não há
lucro a calcular. Não vale para métrica de *percurso*.

MAE e tempo-até-verde existem no histórico já gravado e não dependem de stop, de
lote, nem de saldo. São propriedades do caminho do preço entre a entrada e o
momento em que a posição fica positiva — e esse caminho existiu, foi observado, e
está no tick.

Consequências, se o baseline confirmar que a métrica separa:

- O SVC (e todo sensor de estado) ganha um eixo de avaliação que não exige
  inventar uma política de saída antes de ter uma: o sensor lê diferente nas
  entradas de MAE baixo e nas de MAE alto?
- Substitui "momentos em que eu acertei" — que sob segurar-até-verde é o
  conjunto inteiro e não filtra nada — por uma nota **graduada**. É o que torna
  o caminho "operações do dono como rótulo de teste" bem-posto em vez de um
  teste que sempre passa.
- MAE em USD por onça é neutro de corretora e independe de lote, então transfere
  entre contas e entre feeds sem conversão. Casa com o invariante 2 sem esforço.

**Decidido: diagnóstico, não portão.** MAE mede *percurso*; aceitação de sensor
mede *retorno líquido de custo*. Sensor com MAE ótima e retorno negativo não
passa — e passaria, se MAE gateasse. A ADR registra assim quando for escrita.

Isso deixa MAE com dois usos legítimos, os dois não-gateantes:

- **Eixo de diagnóstico do sensor:** o sensor lê diferente nas entradas de MAE
  baixo e nas de MAE alto? Um sensor que não separa os dois quartis não está
  vendo o que se pensava que via.
- **Nota graduada no lugar de "acertei":** sob segurar-até-verde o conjunto de
  acertos é o conjunto inteiro e não filtra nada. MAE e tempo-até-verde
  graduam, e é isso que torna o caminho "operações do dono como rótulo de
  teste" bem-posto em vez de um teste que sempre passa.

O que continua fora: MAE não entra em nenhum critério de aceitação do SVC nem de
sensor nenhum. Os critérios permanecem prospectivos e líquidos de custo.

---

## 2026-08-08 — O dono como piso adversarial

**Status:** proposta. Nada implementado, nada decidido.

Critério estatístico responde "este sensor lê alguma coisa?". Não responde
"quando o sistema está bom o bastante para confiar dinheiro a ele?" — e essa é a
pergunta que decide a passagem para a camada 4.

A proposta: **o critério de aceitação de um conjunto de sensores é bater a curva
do dono, líquida de custo, sob a mesma política de saída.** Não copia nada dele.
Usa-o como piso. A pergunta sai do abstrato e vira comparação contra um
benchmark que ele já confia, porque é o dinheiro dele.

Cinco coisas que precisam estar certas para o piso não mentir:

**1. Realizado contra simulado favorece o simulado.** A curva dele inclui o
slippage que ele pagou de verdade, os requotes, as execuções ruins. A curva do
sistema, se for simulada, não inclui nada disso. Comparar as duas cruas dá
vitória ao sistema por construção. O simulado tem de rodar líquido de slippage
**medido**, não teórico — e slippage medido só existe ao vivo, o que empurra
esta comparação para depois de haver execução real, ainda que mínima.

**2. Uma curva é uma amostra, não uma distribuição.** Bater o caminho único que
ele realizou não é evidência: aquele caminho é um sorteio entre muitos que a
mesma política produziria. O piso tem de ser uma faixa — reamostrar a sequência
de operações dele e exigir que o sistema bata a faixa, não o ponto.

**3. O piso não é estacionário.** Ele melhora, piora, e o mercado muda. Piso
medido em 2024 não obriga nada em 2026. A comparação só vale **contemporânea**,
sobre o mesmo período — o que significa que ele precisa continuar operando à mão
enquanto o sistema é avaliado. Isso é um custo real da proposta e não tem
contorno.

**4. P&L sozinho premia o estilo de cauda.** Ele tem taxa de acerto perto de
100% com cauda esquerda não observada; um sistema com stop tem acerto menor.
Comparar só resultado final escolhe o de mais risco escondido. O eixo de risco
tem de entrar, e o candidato natural é o mesmo da nota acima: pior excursão
adversa e tempo em drawdown, em JPY, na conta inteira — não por operação.

**5. Piso trivial não serve de piso.** Se a curva dele for medíocre, bater é
fácil e o critério não diz nada. Se for excelente por sorte de um período, é
inatingível pelo motivo errado. Quanto o piso vale é exatamente o que o baseline
casado mede primeiro.

**O que isto é, e o que não é.** É **veto**, não portão: falhar é decisivo — não
confie dinheiro. Passar não basta sozinho, porque os critérios prospectivos
continuam valendo. Um sistema que bate o dono e não tem edge medido bateu um
humano num período, e isso é uma amostra de tamanho um.

---

## 2026-08-08 — A explicação que cobria tudo, e estava errada

**Status:** vira pergunta obrigatória no registo do SVC. Nada a verificar.

Durante o download de julho, o comportamento degradou de forma consistente:

```
503 crescentes
ritmo   12s  ->  33s por arquivo horário
retry   0,7  ->  0,86 por arquivo
```

A explicação corrente era *"a Dukascopy limita por volume acumulado, e por isso
degrada ao longo da corrida"*. Ela cobria **todas** as observações. Nenhuma
medição a contradizia. Foi usada para recomendar desacelerar a corrida de dois
anos.

Estava errada. A causa era um segundo processo, órfão de um `TaskStop` que não
matou o filho, a baixar o mesmo diretório — duas conexões bastam, coisa que o
próprio `config/feeds/dukascopy.yaml` já documentava e que não foi aplicada ao
próprio comportamento.

**A explicação não caiu por análise. Caiu quando o processo apareceu numa
listagem de PIDs, por acaso.**

### Por que isto é sobre sensores, não sobre downloads

É o formato exato que o sobreajuste toma: explica bem o histórico, nada nos
dados o contradiz, e está errado.

Um sensor que separa quintis de amplitude realizada porque leu volatilidade, e
um que os separa porque leu um artefacto de horário do feed, produzem a mesma
tabela de resultados. Ambos passam nos critérios. Só o segundo desaba fora da
amostra — e quando desabar, o histórico de testes vai parecer ter aprovado
corretamente, porque aprovou.

A defesa é a mesma nos dois casos, e é barata: **enunciar a causa alternativa e
dizer o que a descartaria**. Custa minutos. Quase ninguém faz, porque quando a
explicação corrente ajusta bem não parece haver o que procurar — e é exatamente
aí que ela é mais perigosa.

Virou pergunta obrigatória na secção 2.5 do SVC e coluna no registo da secção 7:
*"explicação alternativa considerada, e o que a descartou"*. Não é critério de
aceitação numérico; é campo que precisa estar respondido para a linha contar
como completa.

### O corolário operacional

Antes de dimensionar qualquer decisão sobre uma explicação de degradação,
verifique o que está a correr na máquina. É uma verificação de segundos que
teria poupado horas — e que não foi feita porque a explicação disponível já
parecia suficiente.
