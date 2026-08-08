"""Escritor de log JSONL no schema de docs/schemas/log-schema.md.

Um schema, duas linguagens. Se Python e MQL5 escreverem formatos diferentes, os
dados nao se comparam e a validacao cross-feed inteira perde sentido.

O envelope obrigatorio e o motivo deste modulo existir. Sem `run_id`,
`build_hash` e `config_hash`, um resultado ruim nao se distingue de uma
configuracao errada, e meses de teste viram arquivos que nao se atribuem a nada.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from riser.core.paths import logs_dir, repo_root

LEVELS = ("debug", "info", "warn", "error")


def build_hash() -> str:
    """7 primeiros do commit, com sufixo `-dirty` se houver alteracao pendente.

    O sha fica mesmo quando sujo: `dirty` sozinho responde "havia alteracao" e
    destroi "sobre qual base", que e a pergunta que o campo existe para
    responder.

    Sem git disponivel, devolve `nogit`. Nunca levanta: um log que se recusa a
    escrever porque o git falhou e pior que um log com procedencia parcial.
    """
    try:
        root = repo_root()
        sha = subprocess.run(
            ["git", "rev-parse", "--short=7", "HEAD"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        sujo = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root, capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
        return f"{sha}-dirty" if sujo else sha
    except (subprocess.SubprocessError, OSError, RuntimeError):
        return "nogit"


def config_hash(*paths: Path | str) -> str:
    """Hash do conteudo dos arquivos de configuracao efetivos.

    Recebe os caminhos em vez de descobri-los: o que conta como "configuracao
    efetiva" depende do componente, e adivinhar produziria um hash que muda por
    motivo errado.
    """
    h = hashlib.sha256()
    for p in sorted(str(x) for x in paths):
        path = Path(p)
        h.update(path.name.encode("utf-8"))
        h.update(path.read_bytes() if path.is_file() else b"<ausente>")
    return h.hexdigest()[:8]


def _now_iso_utc() -> str:
    """ISO 8601 com milissegundos, UTC, sufixo Z — como no schema."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + (
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )


class JsonlLogger:
    """Um arquivo por dia, uma linha JSON por evento.

    Nao e thread-safe de proposito: cada componente tem o seu, e um arquivo
    JSONL escrito por dois processos ao mesmo tempo corrompe linhas de um jeito
    que so aparece na analise, meses depois.
    """

    def __init__(
        self,
        comp: str,
        *,
        alias: str,
        config_paths: tuple[Path | str, ...] = (),
        feed_id: str,
        account_hash: str | None = None,
        broker_id: str | None = None,
        server_tz_offset_h: float | None = None,
        run_id: str | None = None,
    ) -> None:
        self.comp = comp
        self.alias = alias
        self.account_hash = account_hash
        # feed_id: de onde o dado veio.  broker_id: onde isto executou.
        # Coletando na Exness os dois sao iguais, o que faz parecer que um
        # bastaria. Nao basta: com a Dukascopy dentro de broker_id, agregar por
        # corretora para comparar custo incluiria um feed sem execucao, sem
        # custo e sem conta — resultado plausivel e errado.
        self.feed_id = feed_id
        self.broker_id = broker_id
        self.server_tz_offset_h = server_tz_offset_h
        self.run_id = run_id or str(uuid.uuid4())
        self.build_hash = build_hash()
        self.config_hash = config_hash(*config_paths) if config_paths else "noconfig"

        self._dir = logs_dir(comp, alias, account_hash)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fh: TextIO | None = None
        self._day: str | None = None

    # ---------------------------------------------------------------- interno

    def _stream(self, day: str) -> TextIO:
        if self._fh is None or self._day != day:
            if self._fh is not None:
                self._fh.close()
            self._fh = (self._dir / f"{day}.jsonl").open("a", encoding="utf-8")
            self._day = day
        return self._fh

    def _envelope(self, lvl: str) -> dict[str, Any]:
        ts = _now_iso_utc()
        if self.server_tz_offset_h is None:
            # Ausencia deliberada e null, nunca omissao (log-schema.md).
            # Componente sem servidor de corretora nao tem hora de servidor;
            # omitir o campo tornaria isso indistinguivel de um bug, e quem le
            # o arquivo meses depois nao teria como saber qual dos dois foi.
            ts_srv = None
        else:
            secs = int(self.server_tz_offset_h * 3600)
            ts_srv = datetime.now(timezone.utc).timestamp() + secs
            ts_srv = datetime.fromtimestamp(ts_srv, timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%f"
            )[:-3]
        return {
            "ts": ts,
            "ts_srv": ts_srv,
            "run_id": self.run_id,
            "build_hash": self.build_hash,
            "config_hash": self.config_hash,
            "src": "py",
            "comp": self.comp,
            "lvl": lvl,
            "account_hash": self.account_hash,
            "feed_id": self.feed_id,
            "broker_id": self.broker_id,
        }

    # ---------------------------------------------------------------- publico

    def envelope(self, lvl: str = "info") -> dict[str, Any]:
        """O envelope sozinho, para artefato que NAO e linha de log.

        Relatorio gravado em arquivo proprio precisa da mesma procedencia de uma
        linha de log, pelo mesmo motivo (invariante 6): sem `run_id`,
        `build_hash` e `config_hash`, a medicao de um mes nao se atribui ao
        codigo e a configuracao que a produziram, e comparar dois meses vira
        comparar duas coisas de origem desconhecida.

        Existe como metodo publico para que ninguem precise montar o envelope a
        mao e esquecer um campo — que e exatamente como schemas divergem.
        """
        return self._envelope(lvl)

    def write(self, lvl: str, **fields: Any) -> None:
        if lvl not in LEVELS:
            raise ValueError(f"lvl invalido: {lvl!r}; esperado um de {LEVELS}")
        rec = self._envelope(lvl)
        rec.update(fields)
        day = rec["ts"][:10]
        fh = self._stream(day)
        fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        fh.flush()

    def debug(self, **f: Any) -> None: self.write("debug", **f)
    def info(self, **f: Any) -> None: self.write("info", **f)
    def warn(self, **f: Any) -> None: self.write("warn", **f)

    def error(self, code: str, **f: Any) -> None:
        """Todo erro carrega `code`. Mensagem em texto e complemento.

        Converter trinta Print() improvisados em codigos depois e trabalho de
        semanas; a convencao comeca agora.
        """
        self.write("error", code=code, **f)

    def boot(self, **f: Any) -> None:
        """Primeiro registro da execucao, com a configuracao efetiva inteira."""
        self.write("info", event="boot", pid=os.getpid(), **f)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "JsonlLogger":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
