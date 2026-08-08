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

- `git status` não mostra nenhum arquivo de dado
- Nenhum símbolo hardcoded
- Nenhuma unidade em pontos ou lotes fora da borda de execução
- Sensor novo tem documento em `docs/sensors/`
