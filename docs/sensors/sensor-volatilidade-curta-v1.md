# Sensor de Volatilidade Curta — SVC v1.0.0-draft

**Status:** especificação. Não validado. Nenhum EA pode depender deste sensor até que todos os critérios de aceitação da secção 4 estejam cumpridos e registados.

**Ativo alvo:** XAUUSD (Exness Standard e Raw Spread, em paralelo)
**Dependências:** camada de dados (Fase 1), harness de paridade (Fase 3)
**Consumidores previstos:** Motor de Trailing, Guardian, Dashboard, EAs futuros

---

## 1. Resumo

O SVC mede quanta volatilidade existe **agora** em relação ao que é normal **para este momento do dia**, e responde numa escala única e comparável ao longo de todo o dia.

Ele combina duas escalas temporais:

- **Micro** — janela de segundos, derivada de ticks. Reage rápido, capta o momento atual.
- **Macro** — ATR de timeframe superior. Reage devagar, fornece contexto de regime.

O micro tem peso maior; o macro impede que um pico isolado de ticks seja interpretado como mudança de regime.

### O que o SVC não faz

- Não emite sinal de compra ou venda.
- Não decide tamanho de posição.
- Não decide entrada, saída ou veto. Quem decide são o EA e o Guardian, consumindo esta leitura.

Se em algum momento o SVC precisar saber o estado da conta, o lote ou a posição aberta para produzir a sua leitura, a implementação está errada.

### Contrato de saída

Função pura de `(janela de ticks, parâmetros)`. Nenhum acesso a conta, posição ou ordem.

```
SVCOutput {
  ts              // timestamp da leitura (servidor)
  value           // 0..1  volatilidade normalizada por horário
  state           // CALM | NORMAL | EXPANDING | CLIMAX
  dq              // 0..1  qualidade direcional (efficiency ratio)
  confidence      // 0..1  penalizado por dados insuficientes ou feed lento
  freshness_ms    // ms desde a última atualização válida
  components{}    // todos os componentes brutos, sempre expostos
}
```

**Regra inegociável:** `components` é sempre preenchido e sempre logado. Se apenas o `value` composto for registado, no dia em que ele errar não haverá como saber qual metade errou.

---

## 2. Detalhes técnicos

### 2.1 Componentes candidatos

Cada componente é normalizado para 0..1 antes de entrar na composição.

| ID | Nome | Definição | Papel |
|---|---|---|---|
| `tr` | Taxa de ticks | contagem de ticks na janela W | atividade / fluxo |
| `rg` | Range | (max − min) na janela W, em pontos | amplitude |
| `dsp` | Deslocamento | \|último − primeiro\| na janela W | movimento líquido |
| `er` | Efficiency ratio | `dsp` ÷ Σ\|Δtick\| | direcional vs lateral |
| `spr` | Spread relativo | spread atual ÷ mediana do spread daquela hora | stress de liquidez |
| `atr` | ATR de contexto | ATR do timeframe macro | regime |

`er` é o componente que separa *volátil e indo a algum lado* de *volátil e serrando*. Para scalp esta é a distinção que mais importa e é a que faltava no desenho original.

### 2.2 Janela rolante, não balde fixo

A janela W é **deslizante**, recalculada a cada tick sobre os últimos W segundos.

Baldes fixos alinhados à barra M1 partem uma explosão que atravesse a fronteira em duas metades mortas — o sensor lê "calmo" duas vezes seguidas num momento de pico. O balde fixo mantém-se implementado, mas **apenas como saída de diagnóstico** para comparação em fecho de barra. Nunca alimenta o `value`.

### 2.3 Normalização por horário — o núcleo do sensor

O XAU tem volatilidade fortemente sazonal ao longo do dia. Ásia parada, abertura de Londres a explodir, abertura de NY a explodir, divulgações a explodir. Um valor absoluto vai gritar "volatilidade alta" todos os dias à mesma hora, e isso é o comportamento *normal* — não é informação.

O SVC responde: **quanta volatilidade acima do normal para este minuto do dia**.

```
value_bruto     = composição dos componentes
baseline(m)     = mediana móvel de value_bruto para o minuto-do-dia m,
                  sobre os últimos N dias de sessão
value           = clamp( value_bruto / baseline(m), 0, 1 ) após escalonamento
```

Notas de implementação:

- O minuto-do-dia usa **hora do servidor**, não hora local. A âncora de calibração é a pausa diária 20:58–22:00 e a abertura de domingo às 22:01.
- Dias de feriado e a sessão de domingo distorcem a baseline e devem ser excluídos do cálculo, mas não da leitura ao vivo.
- A baseline precisa de aquecimento. Antes de N dias disponíveis, `confidence` é reduzido proporcionalmente.

### 2.4 Composição micro × macro

```
value_bruto = α · micro + (1 − α) · macro
```

com `α > 0.5` por decisão de projeto: o micro tem peso maior, conforme pedido.

### 2.5 Protocolo de seleção de variantes

Substitui a força bruta. Três etapas sequenciais, cada uma fixando o que a anterior decidiu. Todas as variantes são **registadas antes** de olhar para os resultados.

**Etapa A — componente micro e janela**
Fixar `α = 0.7`, macro em M15, normalização por horário ativa.
Variar: W ∈ {10s, 20s, 30s, 60s} × micro ∈ {`tr`, `rg`, `tr+rg`, `rg+er`, `tr+rg+er`}
→ 20 testes. Selecionar pelo critério da secção 4.

**Etapa B — normalização**
Fixar o vencedor da Etapa A.
Variar: {bruta, mediana móvel por minuto-do-dia, percentil móvel}
→ 3 testes.

**Etapa C — peso e timeframe macro**
Variar: α ∈ {0.6, 0.7, 0.8} × macro ∈ {M5, M15, M30}
→ 9 testes.

Total: 32 testes com significado individual, em vez de ~500 combinações cegas.

**Regra de seleção:** vence o **planalto**, não o pico. Se uma variante tem o melhor resultado isolado mas os vizinhos de parâmetro caem bruscamente, é ruído. Prefere-se a região onde variantes adjacentes têm desempenho semelhante.

**Separação de dados:** in-sample para as Etapas A–C, out-of-sample para confirmação, e um bloco de holdout tocado **uma única vez** no fim. Se o holdout reprovar, volta-se ao desenho — não se escolhe outra variante.

### 2.6 Cadência e orçamento de latência

| Componente | Cadência |
|---|---|
| `tr`, `rg`, `dsp`, `er` | a cada tick, atualização incremental |
| `spr` | a cada tick, mediana em cache por hora |
| `atr` | apenas no fecho de barra do timeframe macro |
| `baseline` | uma vez por minuto |

A janela rolante usa estrutura incremental (deque + soma corrente). Recalcular a janela inteira a cada tick é inaceitável: em divulgação o XAU produz milhares de ticks por minuto e o sensor engasgaria exatamente no momento que interessa.

**Teto:** 200 µs por atualização, medido e logado em `lat_us`.

### 2.7 Causalidade

O SVC só pode usar informação disponível até ao instante T. Sem exceções.

O harness deve provar isto empiricamente: alimentar ticks um a um e verificar que a saída em T é idêntica à saída em T quando o histórico posterior é truncado. Um sensor que repinta não é detetável por inspeção visual de código.

---

## 3. Sistema de log

Formato JSONL, schema idêntico em Python e MQL5. Um ficheiro por dia por conta.

```
logs/svc/{acct}/{YYYY-MM-DD}.jsonl
```

Registo:

```json
{"ts":"2026-08-08T13:45:22.317Z","sensor":"SVC","ver":"1.0.0",
 "symbol":"XAUUSDm","acct":"standard","value":0.72,"state":"EXPANDING",
 "dq":0.61,"conf":0.90,"fresh_ms":180,
 "c":{"tr":0.81,"rg":0.68,"dsp":0.44,"er":0.61,"spr":0.55,"atr":0.49},
 "base":0.31,"lat_us":142}
```

**Política de amostragem** — registar tick a tick enche disco em dias:

- Registo completo em **toda** mudança de `state`.
- Fora disso, amostragem a cada 5 segundos.
- Janela bruta de ticks de ±15 min gravada em torno de **cada operação**, manual ou automática.

Essa última regra é a mais importante do documento. Ela permite que qualquer sensor inventado no futuro seja calculado retroativamente sobre operações já realizadas. Não é preciso saber hoje o que se vai querer medir amanhã — basta não deitar fora o dado bruto.

**Rotação:** compressão diária, retenção indefinida das janelas de operação, retenção de 90 dias das amostras de 5 segundos.

---

## 4. Critérios de aceitação

O sensor só passa a `v1.0.0` estável quando **todos** forem cumpridos e os números registados no fim deste documento.

| # | Critério | Alvo proposto |
|---|---|---|
| 1 | Correlação de Spearman entre `value(T)` e amplitude realizada em T→T+5min, out-of-sample | ≥ 0,45 |
| 2 | Monotonicidade: amplitude realizada média por quintil de `value` estritamente crescente | 5/5 quintis |
| 3 | Estabilidade: mudanças de `state` por hora, em sessão normal | ≤ 12 |
| 4 | Latência de deteção após mudança de regime | mediana ≤ 30 s |
| 5 | Custo por atualização | ≤ 200 µs |
| 6 | Paridade Python ↔ MQL5 sobre a mesma amostra de ticks | \|Δvalue\| ≤ 1e-4 |
| 7 | Robustez cross-feed: critérios 1–4 mantidos em Exness **e** Dukascopy | ambos |

O critério 7 é o mais severo e o mais importante. Um sensor que só funciona num feed está a ler artefacto do feed, não o mercado.

Os alvos numéricos são propostas iniciais. Devem ser fixados **antes** da primeira execução da Etapa A e não podem ser afrouxados depois para acomodar um resultado.

---

## 5. Usabilidade

### Motor de Trailing

Consome `value` para dimensionar o degrau da escada e o gatilho de ativação.

```
degrau_pontos = degrau_base × (1 + k × value)
```

`degrau_base` e `k` são configuráveis. Volatilidade alta alarga o degrau, evitando que ruído normal encerre a posição cedo demais.

### Guardian

- `state == CLIMAX` → veto de novas entradas.
- `spr` acima do limiar → veto por custo.
- `value` → **dimensiona o lote**, não a distância do stop catastrófico. O limite de perda é definido em JPY; se `k × value` exigir uma distância maior do que a permitida por esse limite, o Guardian reduz o lote até caber, ou recusa a entrada.

### Dashboard

Mostrador único com `value`, cor por `state`, e os componentes em detalhe expandível.

### Como não usar

- **Não usar como sinal de entrada.** Volatilidade alta não indica direção. Usar isoladamente para entrar é ler o sensor ao contrário.
- **Não comparar `value` entre símbolos.** A normalização é específica de cada ativo.
- **Não consumir `value` sem verificar `confidence` e `freshness_ms`.** Uma leitura envelhecida ou de baseline em aquecimento tem de ser tratada como ausente, não como zero.

---

## 6. Decisões em aberto

1. **`er` deve ser um sensor autónomo?** A qualidade direcional é conceptualmente distinta de volatilidade. Em v1 fica exposta como componente; se demonstrar valor preditivo independente, deve ser extraída para um Sensor de Direcionalidade próprio.

2. **`spr` perde informação na conta Raw.** Na Standard o spread alarga com stress e é um termómetro gratuito. Na Raw o spread é próximo de zero na maior parte do tempo, e o componente carrega muito menos sinal. Se a Etapa A selecionar uma variante que depende de `spr`, essa variante pode não transferir entre contas. Verificar explicitamente.

3. **Definição operacional de "mudança de regime"** para medir o critério 4. Proposta: um degrau na amplitude realizada de 5 minutos superior a 2× a mediana da hora, sustentado por 3 minutos. A definir antes dos testes.

4. **Número de dias N da baseline.** Muito curto acompanha demasiado o ruído recente; muito longo não acompanha mudanças estruturais de regime. Candidatos: 10, 20, 40 dias de sessão.

---

## 7. Registo de resultados

*A preencher após a execução do protocolo da secção 2.5. Cada variante testada, com os cinco critérios medidos, data e commit do harness.*

| Data | Commit | Variante | Crit. 1 | Crit. 2 | Crit. 3 | Crit. 4 | Crit. 5 | Notas |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
