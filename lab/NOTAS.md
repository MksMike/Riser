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
