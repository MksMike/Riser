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
# MEDIDO 2026-08-09: o arquivo de candles e por DIA, nao por mes. O caminho
# mensal devolve timeout e o de 60 minutos devolve 404; so este responde 200.
# O offset de cada registo conta a partir do inicio do DIA.
PATH_TEMPLATE = "{instrument}/{year:04d}/{month0:02d}/{day:02d}/BID_candles_min_1.bi5"


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
        """Maior divergencia absoluta, em USD por onca.

        CUIDADO: `0.0` tem dois significados — nenhuma divergencia, ou nenhuma
        comparacao. Sempre leia junto com `comparou`.
        """
        return max(self.max_abs.values()) if self.max_abs else 0.0

    @property
    def comparou(self) -> bool:
        """Houve ao menos um rotulo em comum para comparar.

        Existe porque `max_geral == 0.0` sem rotulos comuns pareceria um
        resultado perfeito, quando na verdade nada foi verificado. E a mesma
        armadilha do invariante 10, um nivel abaixo: verde que nao distingue
        'nao ha achado' de 'nao consigo achar'.
        """
        return self.comuns > 0

    def exigir_comparacao(self) -> None:
        """Levanta se nada foi comparado. Use antes de declarar aprovacao."""
        if not self.comparou:
            raise ValueError(
                f"nenhum rotulo em comum: {self.so_nossas} barras so nossas, "
                f"{self.so_referencia} so da referencia. Divergencia zero aqui "
                "significa que nada foi comparado, nao que esta tudo certo."
            )


def reference_url(cfg: FeedConfig, instrument: str, year: int, month: int, day: int) -> str:
    path = PATH_TEMPLATE.format(
        instrument=instrument, year=year, month0=month - 1, day=day
    )
    return f"{cfg.base_url}/{path}"


def reference_path(
    instrument: str, year: int, month: int, day: int, root: Path | None = None
) -> Path:
    """Guardado ao lado do bruto, num diretorio que se anuncia como referencia."""
    base = root if root is not None else raw_dir("dukascopy-reference")
    return (base / instrument / f"{year:04d}" / f"{month:02d}"
            / f"{day:02d}_BID_candles_min_1.bi5")


def download_reference_day(
    instrument: str,
    year: int,
    month: int,
    day: int,
    *,
    cfg: FeedConfig | None = None,
    root: Path | None = None,
) -> Path | None:
    """Baixa o M1 de um dia. Devolve None se o servidor disser 404."""
    cfg = cfg or FeedConfig.load()
    dest = reference_path(instrument, year, month, day, root)
    if dest.exists():
        return dest

    payload = fetch(
        cfg, reference_url(cfg, instrument, year, month, day),
        RateLimiter(cfg.min_interval_s),
    )
    if payload is None:
        return None
    write_atomic(dest, payload)
    return dest


def load_reference_month(
    instrument: str,
    year: int,
    month: int,
    spec: InstrumentSpec,
    *,
    cfg: FeedConfig | None = None,
    root: Path | None = None,
) -> pd.DataFrame:
    """Baixa e decodifica o mes inteiro, dia a dia.

    Dia ausente e pulado em silencio: fim de semana nao publica candle, e isso
    nao e falha. O que interessa e haver rotulos em comum suficientes — quem
    decide se houve comparacao e `exigir_comparacao()`.
    """
    import calendar

    partes = []
    for day in range(1, calendar.monthrange(year, month)[1] + 1):
        p = download_reference_day(
            instrument, year, month, day, cfg=cfg, root=root
        )
        if p is None:
            continue
        df = decode_candles(p.read_bytes(), year, month, day, spec)
        if not df.empty:
            partes.append(df)
    if not partes:
        return pd.DataFrame(
            columns=["ts_utc", "open", "high", "low", "close", "volume"]
        )
    return pd.concat(partes, ignore_index=True).sort_values(
        "ts_utc", kind="stable", ignore_index=True
    )


def decode_candles(
    payload: bytes, year: int, month: int, day: int, spec: InstrumentSpec
) -> pd.DataFrame:
    """Decodifica o M1 de um DIA em barras com o schema de `bars.py`."""
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
    # Offset conta a partir do inicio do DIA, nao do mes: o arquivo e diario.
    base = datetime(year, month, day, tzinfo=timezone.utc)

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
