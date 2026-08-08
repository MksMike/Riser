# Schema de log — RISER

Um schema, duas linguagens. Se Python e MQL5 escreverem formatos diferentes, os
dados não se comparam e a validação inteira perde sentido.

Formato: **JSONL** (um objeto JSON por linha, sem vírgula, sem array externo).

---

## Envelope obrigatório

Toda linha, de todo componente, carrega estes campos. Sem exceção.

| Campo | Tipo | Descrição |
|---|---|---|
| `ts` | string | ISO 8601 com milissegundos, **UTC** |
| `ts_srv` | string | mesmo instante em hora do servidor |
| `run_id` | string | UUID gerado na inicialização, constante durante a execução |
| `build_hash` | string | 7 primeiros do commit; `dirty` se houver alteração não commitada |
| `config_hash` | string | hash do arquivo de configuração efetivo |
| `src` | string | `py` ou `mql5` |
| `comp` | string | componente que escreveu (`svc`, `guardian`, `trailing`, `dash`) |
| `lvl` | string | `debug` `info` `warn` `error` |
| `account_hash` | string | hash de `ACCOUNT_LOGIN`, resolvido em runtime. Nunca o login em claro |
| `broker_id` | string | `id` do manifesto em `config/brokers/` (ex: `exness-standard`) |

**Por que UTC e hora de servidor juntos:** cada corretora tem fuso próprio. Sem
os dois, dados de corretoras diferentes não se alinham — e o alinhamento é a
premissa da validação cross-feed.

**Por que `account_hash` e `broker_id`:** alias de terminal não identifica quem
gerou a linha. Um terminal troca de conta sem que nada no sistema de arquivos
mude, e `acct` diz apenas o *tipo* (`standard`, `raw`) — duas contas do mesmo
tipo colidem. `account_hash` é a identidade; `broker_id` amarra a linha ao
manifesto contra o qual ela foi produzida, que é o que torna custo e spread
comparáveis entre corretoras.

**Por que `build_hash` e `config_hash`:** sem eles, um resultado ruim não se
distingue de uma configuração errada. Este é o campo que torna meses de teste
interpretáveis em vez de um monte de arquivos.

---

## Caminho

```
C:\dev\RISER-data\logs\<comp>\<alias>\<hash-login>\<YYYY-MM-DD>.jsonl
```

Nunca dentro do repositório.

O alias nomeia o terminal; `<hash-login>` nomeia a conta. Os dois níveis são
obrigatórios, nesta ordem — ver o invariante *Dado do MT5 é particionado por
conta, não por terminal* em `CLAUDE.md`. Junction é por terminal, conta é por
login: sem o segundo nível, duas contas com custo e execução diferentes
escrevem no mesmo diretório e não há como separar depois.

`<hash-login>` usa o mesmo valor de `account_hash` do envelope. Se os dois
divergirem numa linha, o arquivo está corrompido.

### Troca de conta durante a execução

O diretório de destino é resolvido uma única vez, na inicialização, depois de
ler `ACCOUNT_LOGIN`.

Se o login mudar enquanto o componente executa, ele **encerra**. Não continua
escrevendo no diretório anterior, não migra, não abre o novo arquivo no meio da
execução — registra `E2002 E_ACCOUNT_CHANGED_MIDRUN` com `lvl: error` e para.

Continuar escrevendo misturaria duas contas no mesmo arquivo sob um único
`run_id`, e nenhuma análise posterior conseguiria desfazer isso. Encerrar perde
alguns minutos de coleta; continuar contamina o histórico inteiro daquele dia.

---

## Registro de inicialização

Uma vez por execução, primeiro registro do arquivo, `lvl: info`, `comp: boot`.

Contém: envelope completo, configuração efetiva inteira, build do terminal,
número da conta **com hash**, `account_mode`, manifesto da corretora **conforme
detectado do servidor** (não o do arquivo), e as specs do símbolo lidas em
runtime.

O manifesto detectado é o campo mais importante daqui. É onde mora metade dos
problemas de corretora nova — o arquivo diz uma coisa, o servidor faz outra.

---

## Registro de sensor

```json
{"ts":"2026-08-08T13:45:22.317Z","ts_srv":"2026-08-08T16:45:22.317",
 "run_id":"a3f2...","build_hash":"9c1e4b7","config_hash":"5d2a","src":"mql5",
 "comp":"svc","lvl":"info","account_hash":"7b41c9e2","broker_id":"exness-standard",
 "ver":"1.0.0","symbol":"XAUUSDm","acct":"standard",
 "source":"live","value":0.72,"state":"EXPANDING","dq":0.61,"conf":0.90,
 "fresh_ms":180,"c":{"tr":0.81,"rg":0.68,"dsp":0.44,"er":0.61,"spr":0.55,
 "atr":0.49},"base":0.31,"base_rg":0.42,"lat_us":142}
```

`c` sempre presente. Logar só `value` torna impossível diagnosticar qual
componente errou.

`acct` e `account_hash` não são redundantes: `acct` é o **tipo** de conta
(`standard`, `raw`), que agrupa; `account_hash` é a **identidade**, que separa.
`source` (`live` ou `demo`) é propriedade da conta, não do terminal — o mesmo
terminal serve as duas.

---

## Amostragem

Registrar tick a tick enche disco em dias.

| Situação | Política |
|---|---|
| Mudança de `state` | sempre, registro completo |
| Operação aberta ou fechada | sempre, registro completo |
| Regime normal | amostra a cada 5 s |
| Erro | sempre |

**Janela bruta de ticks:** ±15 minutos em torno de **cada** operação, manual ou
automática, em Parquet, em `RISER-data\windows\`.

Esta é a regra mais valiosa do projeto. Qualquer sensor inventado no futuro pode
ser calculado retroativamente sobre operações já realizadas. Não é preciso saber
hoje o que se vai querer medir amanhã — basta não jogar fora o dado bruto.

Toggle `capture_raw_window`, ligado no perfil dev, desligado em build distribuído.

---

## Privacidade

Número de conta sempre com hash, nunca em claro. Nada de nome, e-mail ou
credencial em log. Vale desde já, mesmo com um único usuário — retrofitar
redação em log histórico não funciona.

---

## Erros são códigos, não frases

Catálogo completo fica para a camada 5, mas a **convenção** começa agora, porque
converter trinta `Print()` improvisados depois é trabalho de semanas.

```
E1xxx  corretora / símbolo      E1002  E_STOPS_LEVEL_VIOLATION
E2xxx  conta / margem           E2001  E_ACCOUNT_MODE_MISMATCH
                                E2002  E_ACCOUNT_CHANGED_MIDRUN
E3xxx  sensor                   E3001  E_SENSOR_STALE
E4xxx  execução                 E4001  E_ORDER_REJECTED
E5xxx  dado / log               E5001  E_SCHEMA_MISMATCH
```

Todo erro logado carrega `code`. Mensagem em texto é complemento, nunca o
identificador.
