# 0002 — Log particionado por conta, e encerrar quando o login muda

2026-08-08 | Status: aceita

## Contexto

As junctions do MT5 são criadas por **terminal**: `tools\setup-junctions.ps1`
aponta `MQL5\Files\RISER` para `RISER-data\mt5\<alias>`. O alias nomeia a
instalação.

Mas identidade de conta é o **login**, e um terminal troca de conta sem que
nada no sistema de arquivos mude. Basta um logout/login no MT5. A partir desse
instante, duas contas com custo, spread, execução e `is_demo` diferentes
escrevem no mesmo diretório, sob o mesmo alias, sem marca nenhuma que permita
separá-las depois.

O campo `acct` do envelope não resolve: ele carrega o *tipo* (`standard`,
`raw`), não a identidade. Duas contas standard colidem.

Isto foi percebido ao criar as junctions do PC-Home, antes de existir qualquer
coletor — ou seja, antes de haver dado para perder.

## Decisão

O alias do terminal nunca é o nível mais profundo. Quem escreve resolve
`ACCOUNT_LOGIN` em runtime e desce mais um nível:

```
C:\dev\RISER-data\mt5\<alias>\<hash-de-ACCOUNT_LOGIN>\
logs\<comp>\<alias>\<hash-login>\<YYYY-MM-DD>.jsonl
```

O envelope obrigatório do log ganha `account_hash` (identidade) e `broker_id`
(manifesto contra o qual a linha foi produzida). O login entra sempre com hash,
nunca em claro.

O diretório de destino é resolvido **uma vez**, na inicialização. Se o login
mudar durante a execução, o componente registra
`E2002 E_ACCOUNT_CHANGED_MIDRUN` com `lvl: error` e **encerra**. Não migra, não
abre arquivo novo, não continua.

## Consequências

Fica mais fácil: comparar custo medido entre contas, que é o insumo de
`config/cost_profiles.yaml` e o critério de saída da Camada 1. Cada conta tem
uma série própria desde o primeiro tick coletado.

Fica mais difícil: operar com uma única instalação de terminal servindo duas
contas. Passa a exigir uma instalação por conta, em pasta separada, cada uma
com seu alias. É custo de disco e de configuração, pago uma vez.

Fica impossível: misturar duas contas no mesmo arquivo sem que alguém note.
Esse é o objetivo.

Custo aceito: uma troca de conta interrompe a coleta e perde os minutos até
alguém reiniciar o componente.

## Alternativas descartadas

**Manter a partição só por alias de terminal.**
O que teria acontecido: enquanto cada terminal servisse uma conta fixa, nada
falharia — e é exatamente por isso que a falha seria descoberta tarde. No
primeiro logout/login dentro do mesmo terminal, ticks e logs de duas contas
passariam a se acumular no mesmo diretório, indistinguíveis. Como
`spread_observed_usd_per_oz` sai de medição sobre esses dados, o valor medido
para a Exness Standard seria a média de duas populações diferentes. O sintoma
apareceria meses depois como "o spread da Standard está estranho", sem nenhuma
pista apontando para a causa, e todo o histórico anterior seria irrecuperável
— não há como desfazer a mistura a posteriori.

**Migrar o diretório quando o login muda, em vez de encerrar.**
O que teria acontecido: o `run_id` é gerado uma vez por execução e permanece
constante. Migrar manteria o mesmo `run_id` atravessando duas contas, e
qualquer análise que agrupe por `run_id` — que é o modo natural de atribuir um
resultado a uma execução — somaria as duas. Pior que o caso anterior: seria
silencioso por construção, porque o próprio log que deveria registrar a
anomalia teria mudado de lugar junto. Encerrar perde minutos; migrar contamina
o dia inteiro e produz um artefato que parece íntegro.

**Pôr o login no nome do arquivo em vez de um nível de diretório.**
O que teria acontecido: separaria as contas, mas quebraria a rotação e a
compressão diária descritas em `docs/schemas/log-schema.md`, que operam sobre
`<YYYY-MM-DD>.jsonl`. Varrer uma conta viraria um glob sobre nomes de arquivo
em vez de um caminho, e cada consumidor — harness, dashboard, análise — teria
de reimplementar o parsing do nome. A primeira divergência entre duas dessas
implementações seria um bug de atribuição de dados, da família mais cara de
diagnosticar.

**Usar o login em claro no caminho.**
O que teria acontecido: violaria a seção *Privacidade* do schema de log já no
primeiro arquivo escrito. E o documento é explícito sobre o motivo: retrofitar
redação em log histórico não funciona. O número da conta ficaria em nomes de
diretório, em backups e em qualquer captura de tela do explorador de arquivos.
