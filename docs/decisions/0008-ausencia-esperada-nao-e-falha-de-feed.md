# 0008 — Ausência esperada não é falha de feed

2026-08-08 | Status: **em aberto — problema registrado, decisão adiada**

Nada a implementar por enquanto. O Guardian ainda não existe e a decisão depende
de `server.timezone_offset`, que segue marcado `VERIFICAR` nos dois manifestos.

Este documento existe para que, quando o sintoma aparecer ao vivo, ele não seja
diagnosticado como bug de sensor.

## Contexto

Ao escrever as regras de fronteira de `bars.py` apareceu uma consequência que
não é do agregador, e sim de todo o sistema a jusante.

O fecho de uma barra é dirigido por evento, não por relógio: a barra só fecha
quando chega um tick posterior ao seu intervalo. Durante a pausa diária —
20:58 às 22:00 no servidor da Exness — nenhum tick chega. Logo:

- nenhuma barra fecha;
- todo sensor que dependa de barra congela com a última leitura, a de 20:58;
- `freshness_ms` cresce sem parar, por mais de uma hora.

Este comportamento é **idêntico** ao de um feed morto por queda de conexão.
Mesma leitura congelada, mesmo `freshness_ms` crescente, mesmo silêncio.

E as duas situações pedem reações opostas:

| situação | o que é | o que o Guardian deve fazer |
|---|---|---|
| pausa diária | ausência **esperada** | esperar, sem alarme, sem vetar por dado velho |
| feed morto | **falha** | vetar entrada, alertar, possivelmente encerrar posição |

Hoje não há como distinguir. Um Guardian que trate silêncio como falha vai
alarmar todo santo dia às 21:00; um que trate silêncio como normal não vai
perceber uma queda de conexão real às 14:00, que é exatamente quando importa.

O documento do SVC já manda tratar leitura envelhecida como **ausente**, nunca
como zero. Isso continua correto e não resolve isto: ausente-porque-pausa e
ausente-porque-caiu exigem decisões diferentes de quem consome.

## Opções

**A. Calendário de sessão declarado no manifesto.**
`config/brokers/*.yaml` já tem `daily_break` e `sunday_open`. O Guardian
consultaria e saberia que está dentro de uma janela de ausência esperada.
Barato e explícito. Depende inteiramente de `timezone_offset`, hoje
`VERIFICAR`, e de o arquivo estar certo — e o schema de log já registra que
metade dos problemas de corretora nova é o arquivo dizer uma coisa e o servidor
fazer outra. Feriado não está no manifesto e produziria falso alarme.

**B. Detecção estatística a partir do histórico.**
Aprender, por minuto do dia, quanto silêncio é normal — a mesma estrutura de
baseline que o SVC já usa. Silêncio de 60 min às 21:00 é o esperado; aos 14:00,
não. Cobre feriado e mudança de horário de verão sem ninguém declarar nada, e
não depende de `timezone_offset`. Custa aquecimento, e classifica errado
justamente no primeiro contato com uma corretora nova, que é quando o risco é
maior.

**C. Sinal independente do terminal.**
O MT5 expõe estado de conexão. Desconectado é falha, sem ambiguidade. Não
resolve o caso central: durante a pausa o terminal permanece conectado e o
sintoma é o mesmo. Serve como sinal adicional, não como resposta.

**D. Combinação: A declara, B verifica, C corta.**
O manifesto declara a janela; a estatística confirma que a janela declarada
corresponde ao silêncio observado — e discordância vira erro, no espírito de
"divergência é erro, não aviso"; a desconexão do terminal veta imediatamente,
por caminho próprio. Mais partes, e é a única que fecha os três buracos.

## O que precisa ser decidido junto

1. **Onde mora a distinção.** É estado do feed, do Guardian, ou entra no
   contrato de saída do sensor? Hoje o contrato tem `freshness_ms` e
   `confidence`, e nenhum dos dois distingue pausa de falha. Acrescentar um
   estado ao sensor mexe num contrato que vale para todos.
2. **Se a ausência esperada deve ser logada.** Silêncio não gera linha de log
   nenhuma, então uma pausa e uma queda de duas horas produzem o mesmo arquivo:
   nada. Talvez precise de um registro periódico de "vivo e em silêncio".
3. **O que fazer com posição aberta na fronteira.** Uma posição que atravessa a
   pausa fica sem leitura de sensor por uma hora, e a reabertura pode abrir com
   gap. Isso é decisão de Guardian, não de sensor, mas depende desta.

## Consequências de deixar em aberto

Aceito enquanto não houver Guardian nem coleta ao vivo. O risco é o custo de
diagnóstico: sem este registro, a primeira aparição do sintoma ao vivo seria
investigada como bug do sensor ou do coletor — e a investigação começaria pelo
lugar errado, porque tudo indica falha e nada indica calendário.

Revisar quando: `timezone_offset` for medido, **ou** o Guardian entrar em
desenvolvimento, o que vier primeiro.
