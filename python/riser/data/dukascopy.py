"""Downloader de ticks brutos da Dukascopy.

Este modulo NAO parseia nada. Ele busca o `.bi5` por hora e o guarda intacto.
A separacao e deliberada: baixar uma vez, parsear muitas. Um bug no parser
custa um reprocessamento local; um bruto apagado custa uma refeitura completa
do download, e horas antigas saem do ar sem aviso.

Ordem cronologica reversa — mes mais recente primeiro. O dado recente e o que
descreve o regime atual do mercado, e um download de dois anos interrompido no
meio deixa o periodo util pronto em vez de deixar 2024 completo e 2026 vazio.

Unidades: este modulo nao interpreta preco. Nao ha ponto nem lote aqui, e nao
pode haver — o `.bi5` guarda inteiros cuja escala e propriedade do instrumento,
resolvida em `ticks.py` a partir de `config/feeds/dukascopy.yaml`.
"""

from __future__ import annotations

import random
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import yaml

from riser.core.log import JsonlLogger
from riser.core.paths import raw_dir, repo_root

FEED = "dukascopy"
CONFIG_PATH = repo_root() / "config" / "feeds" / "dukascopy.yaml"

# Marcador de hora que o servidor declara inexistente. Zero bytes, ao lado do
# lugar onde o .bi5 estaria. Sem ele, uma retomada volta a pedir as mesmas
# horas ausentes para sempre — e ha muitas: todo fim de semana e feriado.
#
# NAO se usa um .bi5 vazio para isso: 200 com zero bytes e uma resposta
# legitima e significa "hora sem tick", que e dado, nao ausencia.
ABSENT_SUFFIX = ".absent"


class DukascopyError(RuntimeError):
    """Falha de download que sobreviveu a todas as tentativas."""


@dataclass(frozen=True)
class FeedConfig:
    base_url: str
    path_template: str
    min_interval_s: float
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    timeout_s: float
    history_starts: dict[str, datetime]

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "FeedConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        pol = raw["politeness"]
        starts: dict[str, datetime] = {}
        for name, spec in (raw.get("instruments") or {}).items():
            if spec.get("history_starts"):
                starts[name] = datetime.fromisoformat(
                    str(spec["history_starts"])
                ).replace(tzinfo=timezone.utc)
        return cls(
            base_url=raw["source"]["base_url"].rstrip("/"),
            path_template=raw["source"]["path_template"],
            min_interval_s=float(pol["min_interval_s"]),
            max_retries=int(pol["max_retries"]),
            backoff_base_s=float(pol["backoff_base_s"]),
            backoff_max_s=float(pol["backoff_max_s"]),
            timeout_s=float(pol["timeout_s"]),
            history_starts=starts,
        )


# --------------------------------------------------------------------- URLs


def hour_url(cfg: FeedConfig, instrument: str, hour: datetime) -> str:
    """Monta a URL de uma hora.

    ARMADILHA: o caminho da Dukascopy usa o mes com base ZERO. Janeiro e `00`,
    dezembro e `11`. A conversao acontece so aqui, num lugar unico e testado —
    espalha-la pelo codigo produziria um erro de um mes que nao levanta excecao
    nenhuma, apenas devolve o mes errado.
    """
    if hour.tzinfo is None:
        raise ValueError("hour precisa ser timezone-aware em UTC")
    h = hour.astimezone(timezone.utc)
    path = cfg.path_template.format(
        instrument=instrument,
        year=h.year,
        month0=h.month - 1,
        day=h.day,
        hour=h.hour,
    )
    return f"{cfg.base_url}/{path}"


def raw_path(instrument: str, hour: datetime, root: Path | None = None) -> Path:
    """Caminho local do bruto.

    Guardado com mes com base UM, ao contrario da URL. A leitura humana de um
    diretorio de dados e frequente e `2026/07/` significar agosto seria uma
    fonte permanente de erro de interpretacao.
    """
    h = hour.astimezone(timezone.utc)
    base = root if root is not None else raw_dir(FEED)
    return (
        base / instrument / f"{h.year:04d}" / f"{h.month:02d}" / f"{h.day:02d}"
        / f"{h.hour:02d}h_ticks.bi5"
    )


def hours_reverse(start: datetime, end: datetime) -> Iterator[datetime]:
    """Horas de [start, end), da MAIS RECENTE para a mais antiga.

    Semiaberto no fim: `end` exclusivo evita baixar a hora corrente, que ainda
    esta a ser escrita pelo servidor e devolveria um arquivo parcial que a
    retomada trataria como completo.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start e end precisam ser timezone-aware em UTC")
    cur = end.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    floor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if cur >= end.astimezone(timezone.utc):
        cur -= timedelta(hours=1)
    while cur >= floor:
        yield cur
        cur -= timedelta(hours=1)


def month_start(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def last_complete_month(now: datetime) -> tuple[datetime, datetime]:
    """[inicio, fim) do ultimo mes inteiro terminado antes de `now`.

    "Completo" importa: o mes corrente tem horas que ainda nao existem, e uma
    distribuicao de spread calculada sobre mes parcial nao e comparavel com as
    dos meses cheios.
    """
    this_month = month_start(now)
    end = this_month
    start = month_start(end - timedelta(days=1))
    return start, end


# ----------------------------------------------------------------- download


class _RateLimiter:
    """Intervalo minimo entre requisicoes.

    Conservador de proposito: este e um servico publico e gratuito, e o custo
    de ser bloqueado e perder o unico feed de referencia independente que o
    projeto tem — o criterio 7 do SVC depende dele existir.
    """

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval_s:
            time.sleep(self.min_interval_s - elapsed)
        self._last = time.monotonic()


def _fetch(cfg: FeedConfig, url: str, limiter: _RateLimiter) -> bytes | None:
    """Busca uma URL. Devolve os bytes, ou None se o servidor disser 404.

    404 nao e retentado: significa "esta hora nao existe", nao "tente de novo".
    Retentar 404 cinco vezes com backoff transformaria um fim de semana normal
    em minutos de espera inutil, multiplicados por cada hora do periodo.
    """
    ultima: Exception | None = None
    for tentativa in range(cfg.max_retries):
        limiter.wait()
        req = urllib.request.Request(
            url, headers={"User-Agent": "RISER/0.1 (pesquisa; dado de tick)"}
        )
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout_s) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return None
            ultima = exc
            # 429 e 5xx sao transitorios: vale esperar.
            if exc.code not in (429, 500, 502, 503, 504):
                raise DukascopyError(f"HTTP {exc.code} em {url}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            ultima = exc

        # Backoff exponencial com jitter. O jitter existe para que uma retomada
        # de milhares de horas nao sincronize as tentativas num padrao que o
        # servidor leia como abuso.
        espera = min(cfg.backoff_base_s * (2**tentativa), cfg.backoff_max_s)
        time.sleep(espera * (0.5 + random.random()))

    raise DukascopyError(f"{cfg.max_retries} tentativas falharam em {url}: {ultima}")


def _write_atomic(dest: Path, payload: bytes) -> None:
    """Grava por arquivo temporario e renomeia.

    Sem isto, uma interrupcao no meio da escrita deixa um .bi5 truncado — e a
    retomada, que decide por existencia do arquivo, o trataria como completo.
    O dado corrompido sobreviveria em silencio ate o parser tropecar nele.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(dest)


def download_hour(
    cfg: FeedConfig,
    instrument: str,
    hour: datetime,
    limiter: _RateLimiter,
    *,
    root: Path | None = None,
) -> str:
    """Baixa uma hora. Devolve `skip`, `ok`, `empty` ou `absent`.

    Retomavel por construcao: se o bruto ou o marcador de ausencia ja existem,
    nao ha requisicao nenhuma.
    """
    dest = raw_path(instrument, hour, root)
    marker = dest.with_name(dest.name + ABSENT_SUFFIX)

    if dest.exists() or marker.exists():
        return "skip"

    payload = _fetch(cfg, hour_url(cfg, instrument, hour), limiter)

    if payload is None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
        return "absent"

    _write_atomic(dest, payload)
    return "empty" if len(payload) == 0 else "ok"


def download_range(
    instrument: str,
    start: datetime,
    end: datetime,
    *,
    cfg: FeedConfig | None = None,
    root: Path | None = None,
    logger: JsonlLogger | None = None,
    progress: bool = True,
) -> dict[str, int]:
    """Baixa [start, end) em ordem cronologica reversa.

    Devolve a contagem por desfecho. Nao levanta ao fim de uma hora que falhou
    depois de todas as tentativas: registra, conta e segue — uma hora
    inalcancavel nao deve custar as outras milhares.
    """
    cfg = cfg or FeedConfig.load()
    limiter = _RateLimiter(cfg.min_interval_s)

    piso = cfg.history_starts.get(instrument)
    if piso is not None and start < piso:
        if logger:
            logger.warn(
                event="history_floor", instrument=instrument,
                pedido=start.isoformat(), disponivel=piso.isoformat(),
            )
        start = piso

    horas = list(hours_reverse(start, end))
    total = len(horas)
    tally = {"ok": 0, "empty": 0, "absent": 0, "skip": 0, "erro": 0}
    bytes_novos = 0

    if logger:
        logger.info(
            event="download_start", feed=FEED, instrument=instrument,
            desde=start.isoformat(), ate=end.isoformat(), horas=total,
            ordem="cronologica_reversa",
        )

    for n, hora in enumerate(horas, 1):
        try:
            desfecho = download_hour(cfg, instrument, hora, limiter, root=root)
            tally[desfecho] += 1
            if desfecho in ("ok", "empty"):
                bytes_novos += raw_path(instrument, hora, root).stat().st_size
        except DukascopyError as exc:
            tally["erro"] += 1
            desfecho = "erro"
            if logger:
                logger.error(
                    "E5002", event="download_fail", instrument=instrument,
                    hora=hora.isoformat(), msg=str(exc),
                )

        if progress and (n % 25 == 0 or n == total or desfecho == "erro"):
            pct = 100.0 * n / total if total else 100.0
            print(
                f"[{n:>6}/{total}] {pct:5.1f}%  {hora:%Y-%m-%d %Hh}  {desfecho:<6}"
                f"  ok={tally['ok']} vazias={tally['empty']} ausentes={tally['absent']}"
                f" puladas={tally['skip']} erros={tally['erro']}",
                flush=True,
            )

    if logger:
        logger.info(
            event="download_done", feed=FEED, instrument=instrument,
            horas=total, bytes_novos=bytes_novos, **tally,
        )
    return tally


def download_month(
    instrument: str,
    year: int,
    month: int,
    **kwargs: object,
) -> dict[str, int]:
    """Atalho para um mes-calendario inteiro, em UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return download_range(instrument, start, end, **kwargs)  # type: ignore[arg-type]
