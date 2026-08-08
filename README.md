# RISER

Sistema modular de scalp para MetaTrader 5. XAUUSD primeiro, BTCUSD depois.

**Fase 1 — Fundação.** Nada em produção.

As regras de arquitetura estão em [`CLAUDE.md`](CLAUDE.md). Leia antes de
escrever qualquer coisa; toda sessão de Claude Code as herda automaticamente.

---

## Instalação num PC novo

```powershell
git clone https://github.com/MksMike/Riser.git C:\dev\RISER
cd C:\dev\RISER

# 1. Descobrir os terminais MT5 instalados
.\tools\setup-junctions.ps1 -List

# 2. Criar as junctions para cada terminal
.\tools\setup-junctions.ps1 -TerminalId <id> -Alias exness-standard
.\tools\setup-junctions.ps1 -TerminalId <id> -Alias exness-raw-demo

# 3. Config local (não versionada)
copy config\terminals.example.json config\terminals.json
```

O hash da pasta do terminal difere em cada PC. Nunca escreva esse caminho em
código — o script descobre em runtime.

---

## Onde as coisas ficam

```
C:\dev\RISER\          código (este repositório)
C:\dev\RISER-data\     dados — fora do Git, nunca versionado
```

Dentro do terminal, tudo em subpasta `RISER`: `Experts\RISER\`,
`Indicators\RISER\`, `Include\RISER\`. Includes usam
`#include <RISER\Core\Sensor.mqh>`.

`Files\RISER` aponta para `RISER-data`, não para o repositório.

---

## Trabalho em paralelo

```bash
git worktree add ../RISER-dashboard feature/dashboard
```

Particione por diretório, não por tarefa. Só um worktree fica junctionado por
vez — para trocar, rode o script com `-Repo <caminho-do-worktree>`.

---

## Sessões de debate

`/debate <assunto>` no Claude Code. Não edita arquivos, apresenta alternativas,
declara discordância. Funciona melhor em sessão limpa, sem arquivos carregados.

---

## Antes de commitar

```powershell
.\tools\check-invariants.ps1     # silencioso = nada a corrigir
```

Varre `docs/` e `config/` procurando contradição com os invariantes do
`CLAUDE.md`. Não é exaustivo de propósito — cobre os modos de falha que já
aconteceram aqui. `-ShowRules` lista o que ele verifica.

Citação deliberada da forma antiga se suprime com `invariant-ok: <REGRA>` na
linha ou na anterior; para o documento que descreve um antipadrão inteiro, com
`invariant-ok-file: <REGRA>` no cabeçalho.

O script relata quantos arquivos suprimem cada regra. **Se mais de dois
suprimem a mesma, o problema é da regra, não dos arquivos** — recalibre ou
remova, não acrescente supressão. Nesse caso ele sai com erro mesmo sem achado
nenhum: uma regra dispensada em todo lado produz o mesmo silêncio de uma regra
satisfeita, e os dois estados precisam ser distinguíveis.

Checklist do que o script não cobre:

- `git status` não mostra nenhum arquivo de dado
- Nenhum símbolo hardcoded
- Nenhuma unidade em pontos ou lotes fora da borda de execução
- Sensor novo tem documento em `docs/sensors/`
