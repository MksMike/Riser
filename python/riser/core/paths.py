"""Resolucao de caminhos do RISER.

Duas raizes, separadas de proposito (invariante 7):

    repo_root()   codigo, versionado
    data_root()   tick, log, barra, janela — NUNCA versionado

Nenhum caminho de terminal MT5 aparece aqui. O hash da pasta do terminal e
especifico de cada PC e se descobre em runtime, por `tools/setup-junctions.ps1`.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

_DEFAULT_DATA_ROOT = Path(r"C:\dev\RISER-data")


def repo_root() -> Path:
    """Sobe a partir deste arquivo ate encontrar o CLAUDE.md.

    Procurar pelo marcador em vez de contar `.parent` evita que mover um modulo
    de diretorio quebre a resolucao em silencio.
    """
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        if (candidate / "CLAUDE.md").is_file():
            return candidate
    raise RuntimeError(
        f"raiz do RISER nao encontrada a partir de {here}: nenhum CLAUDE.md nos pais"
    )


def data_root() -> Path:
    """Raiz dos dados, fora do repositorio.

    Ordem: RISER_DATA_ROOT no ambiente, depois `data_root` de
    config/terminals.json, depois o padrao. A variavel de ambiente vem primeiro
    para que um VPS ou um segundo disco nao exijam editar arquivo versionado.
    """
    env = os.environ.get("RISER_DATA_ROOT")
    if env:
        return Path(env)

    local = repo_root() / "config" / "terminals.json"
    if local.is_file():
        try:
            cfg = json.loads(local.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"config/terminals.json invalido: {exc}") from exc
        if cfg.get("data_root"):
            return Path(cfg["data_root"])

    return _DEFAULT_DATA_ROOT


def raw_dir(feed: str) -> Path:
    """Bruto, como veio do feed. Baixar uma vez, parsear muitas."""
    return data_root() / "raw" / feed


def ticks_dir() -> Path:
    return data_root() / "ticks"


def bars_dir() -> Path:
    """Barra e cache descartavel: derivada de tick, nunca baixada."""
    return data_root() / "bars"


def windows_dir() -> Path:
    return data_root() / "windows"


def logs_dir(comp: str, alias: str, account_hash: str | None = None) -> Path:
    """logs/<comp>/<alias>/<hash-login>/

    O nivel da conta e obrigatorio quando existe conta: junction e por terminal,
    conta e por login, e sem esse nivel duas contas com custo diferente escrevem
    no mesmo lugar (ADR 0002).

    `account_hash=None` e para componente que nao fala com corretora nenhuma —
    o downloader de um feed publico, por exemplo. Nesse caso o nivel vira
    `no-account`, explicito, em vez de desaparecer: um diretorio a menos seria
    indistinguivel de um esquecimento.
    """
    return data_root() / "logs" / comp / alias / (account_hash or "no-account")


def hash_account_login(login: int | str) -> str:
    """Numero de conta sempre com hash, nunca em claro.

    Vale desde ja, mesmo com um unico usuario: retrofitar redacao em log
    historico nao funciona. O hash e truncado porque o objetivo e distinguir
    contas, nao resistir a ataque — o espaco de logins e pequeno e um hash
    completo nao acrescentaria segredo real.
    """
    return hashlib.sha256(str(login).encode("utf-8")).hexdigest()[:12]
