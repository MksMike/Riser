"""Barras M1 publicadas pela Dukascopy — REFERENCIA DE VALIDACAO, nao fonte.

Este modulo existe para uma unica pergunta: *o agregador esta certo?* Ele baixa
as barras que a Dukascopy publica e as compara com as que `bars.py` produziu a
partir dos ticks.

**Barra baixada nunca alimenta sensor, backtest ou log de produção.** Barra e
derivada de tick, sempre, e o unico papel deste arquivo e servir de segunda
opiniao independente sobre a agregacao. Por isso ele vive em `harness/` e nao
em `data/`: quem procurar fonte de dados nao vai tropecar nele.

A validacao de escala de `ticks.py` responde outra pergunta — se os precos
foram lidos na escala certa. Esta aqui responde se a agregacao esta certa. Um
parser correto com agregador errado passa naquela e falha nesta.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from riser.data.dukascopy import FeedConfig, fetch, RateLimiter, write_atomic
from riser.data.ticks import InstrumentSpec
from riser.core.paths import raw_dir

# Registro de candle: 24 bytes, big-endian.
#
#   int32   segundos desde o inicio do MES
#   int32   open
#   int32   close     <- ATENCAO: close vem antes de low e high
#   int32   low
#   int32   high
#   float32 volume
#
# A ordem nao e OHLC. Ler como OHLC produz barras em que 'high' e o low e
# vice-versa, e a comparacao acusaria o agregador por um erro que e do leitor.
# `decode_candles` valida a coerencia e recusa se a ordem nao fechar.
_CANDLE = struct.Struct(">5if")
CANDLE_SIZE = _CANDLE.size  # 24

# O arquivo mensal de candles de BID. BID porque e o que o agregador produz e o
# que a Exness plota.
PATH_TEMPLATE = "{instrument}/{year:04d}/{month0:02d}/BID_candles_min_1.bi5"


class ReferenceFormatError(ValueError):
    """O conteudo nao corresponde ao formato de candle esperado."""


@dataclass(frozen=True)
class OhlcDiff:
    """Divergencia entre as barras agregadas e as de referencia."""

    comuns: int
    so_nossas: int
    so_referencia: int
    max_abs: dict[str, float]
    pior: dict[str, object]

    @property
    def max_geral(self) -> float:
        return max(self.max_abs.values()) if self.max_abs else 0.0


def reference_url(cfg: FeedConfig, instrument: str, year: int, month: int) -> str:
    path = PATH_TEMPLATE.format(instrument=instrument, year=year, month0=month - 1)
    return f"{cfg.base_url}/{path}"


def reference_path(instrument: str, year: int, month: int, root: Path | None = None) -> Path:
    """Guardado ao lado do bruto, num diretorio que se anuncia como referencia."""
    base = root if root is not None else raw_dir("dukascopy-reference")
    return base / instrument / f"{year:04d}" / f"{month:02d}" / "BID_candles_min_1.bi5"


def download_reference_m1(
    instrument: str,
    year: int,
    month: int,
    *,
    cfg: FeedConfig | None = None,
    root: Path | None = None,
) -> Path | None:
    """Baixa o arquivo mensal de M1. Devolve None se o servidor disser 404."""
    cfg = cfg or FeedConfig.load()
    dest = reference_path(instrument, year, month, root)
    if dest.exists():
        return dest

    payload = fetch(cfg, reference_url(cfg, instrument, year, month), RateLimiter(cfg.min_interval_s))
    if payload is None:
        return None
    write_atomic(dest, payload)
    return dest


def decode_candles(
    payload: bytes, year: int, month: int, spec: InstrumentSpec
) -> pd.DataFrame:
    """Decodifica o arquivo mensal de M1 em barras com o schema de `bars.py`."""
    from riser.data.ticks import _decompress  # mesma descompressao dos ticks

    blob = _decompress(payload)
    if not blob:
        return pd.DataFrame(columns=["ts_utc", "open", "high", "low", "close", "volume"])
    if len(blob) % CANDLE_SIZE != 0:
        raise ReferenceFormatError(
            f"{len(blob)} bytes nao e multiplo de {CANDLE_SIZE}"
        )

    n = len(blob) // CANDLE_SIZE
    arr = np.frombuffer(blob, dtype=">i4,>i4,>i4,>i4,>i4,>f4", count=n)
    base = datetime(year, month, 1, tzinfo=timezone.utc)

    o = arr["f1"].astype("float64") / spec.divisor
    c = arr["f2"].astype("float64") / spec.divisor
    lo = arr["f3"].astype("float64") / spec.divisor
    hi = arr["f4"].astype("float64") / spec.divisor

    # Coerencia: se a ordem dos campos estivesse errada, isto nao fecha.
    incoerentes = int(((hi < lo) | (hi < o) | (hi < c) | (lo > o) | (lo > c)).sum())
    if n and incoerentes > n // 100:
        raise ReferenceFormatError(
            f"{incoerentes}/{n} candles com OHLC incoerente. O formato guarda "
            "time, open, CLOSE, LOW, HIGH, volume — a leitura parece usar outra "
            "ordem."
        )

    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(base, utc=True)
            + pd.to_timedelta(arr["f0"].astype("int64"), unit="s"),
            "open": o,
            "high": hi,
            "low": lo,
            "close": c,
            "volume": arr["f5"].astype("float32"),
        }
    )
    return df.sort_values("ts_utc", kind="stable", ignore_index=True)


def compare_ohlc(
    ours: pd.DataFrame, reference: pd.DataFrame, *, tol_report: int = 5
) -> OhlcDiff:
    """Compara barra a barra, alinhando por `ts_utc`.

    So compara os rotulos presentes nos dois. Rotulo so nosso ou so deles e
    contado e reportado separadamente: pode ser diferenca de politica de barra
    vazia, que nao e erro de OHLC e nao deve poluir a divergencia maxima.
    """
    a = ours.set_index("ts_utc")[["open", "high", "low", "close"]]
    b = reference.set_index("ts_utc")[["open", "high", "low", "close"]]
    comuns = a.index.intersection(b.index)

    max_abs: dict[str, float] = {}
    pior: dict[str, object] = {}
    if len(comuns):
        d = (a.loc[comuns] - b.loc[comuns]).abs()
        for col in ("open", "high", "low", "close"):
            max_abs[col] = float(d[col].max())
        col_pior = max(max_abs, key=lambda k: max_abs[k])
        i = d[col_pior].idxmax()
        pior = {
            "campo": col_pior,
            "ts_utc": i.isoformat(),
            "nosso": float(a.loc[i, col_pior]),
            "referencia": float(b.loc[i, col_pior]),
            "delta": float(d.loc[i, col_pior]),
        }

    return OhlcDiff(
        comuns=len(comuns),
        so_nossas=len(a.index.difference(b.index)),
        so_referencia=len(b.index.difference(a.index)),
        max_abs=max_abs,
        pior=pior,
    )


def top_divergences(
    ours: pd.DataFrame, reference: pd.DataFrame, n: int = 5
) -> pd.DataFrame:
    """As n barras com maior divergencia, para inspecao manual."""
    a = ours.set_index("ts_utc")[["open", "high", "low", "close"]]
    b = reference.set_index("ts_utc")[["open", "high", "low", "close"]]
    comuns = a.index.intersection(b.index)
    if not len(comuns):
        return pd.DataFrame()
    d = (a.loc[comuns] - b.loc[comuns]).abs()
    d["pior"] = d.max(axis=1)
    return d.sort_values("pior", ascending=False).head(n)
