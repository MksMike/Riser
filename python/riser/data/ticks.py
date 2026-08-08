"""Parser e armazenamento de ticks: `.bi5` bruto -> Parquet particionado.

O bruto nunca e alterado nem apagado. Este modulo so le.

Unidades: preco em **USD por onca**, sempre. Nao ha ponto nem lote aqui, e nao
pode haver — sao unidades de corretora, e o `.bi5` nem sequer as conhece. O que
o arquivo guarda sao inteiros cuja escala e propriedade do instrumento.

O ASK e guardado junto com o BID. Sem ele nao se reconstroi custo nem preco de
compra, e essa perda e irreversivel: reprocessar o bruto adianta, mas so se o
bruto ainda existir e alguem lembrar que faltou.
"""

from __future__ import annotations

import lzma
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from riser.core.log import JsonlLogger
from riser.core.paths import raw_dir, ticks_dir
from riser.data.dukascopy import ABSENT_SUFFIX, CONFIG_PATH, FEED, raw_path

# Registro do .bi5: 20 bytes, big-endian.
#
#   uint32  ms desde o inicio da hora do arquivo
#   uint32  ASK, inteiro
#   uint32  BID, inteiro
#   float32 volume de ask
#   float32 volume de bid
#
# ARMADILHA: o ASK vem ANTES do bid. Trocar os dois produz spread negativo em
# todo tick — o dado continua parseando e o erro so aparece quando alguem
# calcular custo. `decode_bi5` recusa quando ask < bid na maioria dos ticks.
_RECORD = struct.Struct(">IIIff")
RECORD_SIZE = _RECORD.size  # 20

COLUMNS = ("ts_utc", "bid", "ask", "bid_vol", "ask_vol")

# Fatores de escala que um erro de `price_decimals` produz. Sao os unicos
# valores que a razao entre duas horas contiguas pode assumir por engano de
# escala; qualquer outro desvio e movimento de mercado.
_SCALE_FACTORS = (0.001, 0.01, 0.1, 10.0, 100.0, 1000.0)


class ScaleError(ValueError):
    """Escala de preco inconsistente. Grava-la seria pior que nao gravar."""


class TickFormatError(ValueError):
    """O conteudo do .bi5 nao corresponde ao formato esperado."""


@dataclass(frozen=True)
class InstrumentSpec:
    """Como converter os inteiros do .bi5 em preco."""

    name: str
    price_decimals: int
    unit: str
    plausible_range: tuple[float, float]

    @property
    def divisor(self) -> float:
        return float(10**self.price_decimals)

    @classmethod
    def load(cls, instrument: str, path: Path = CONFIG_PATH) -> "InstrumentSpec":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        spec = (raw.get("instruments") or {}).get(instrument)
        if spec is None:
            raise KeyError(
                f"instrumento {instrument!r} ausente de {path.name}. "
                "Escala de preco nao se adivinha: declare price_decimals."
            )
        lo, hi = spec["plausible_range"]
        return cls(
            name=instrument,
            price_decimals=int(spec["price_decimals"]),
            unit=str(spec["unit"]),
            plausible_range=(float(lo), float(hi)),
        )


# ------------------------------------------------------------------ decodificacao


def _decompress(payload: bytes) -> bytes:
    """LZMA no formato `alone`, sem tamanho no cabecalho.

    FORMAT_AUTO cobre o caso comum. O fallback existe porque o stream da
    Dukascopy nao declara o tamanho descomprimido, e alguns arquivos so
    decodificam com os filtros explicitos.
    """
    if not payload:
        return b""
    try:
        return lzma.decompress(payload, format=lzma.FORMAT_AUTO)
    except lzma.LZMAError:
        pass
    try:
        # LZMADecompressor aceita stream sem marcador de fim, que e o caso de
        # parte dos arquivos da Dukascopy — `lzma.decompress` recusa esses por
        # considera-los truncados.
        return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(payload)
    except lzma.LZMAError as exc:
        raise TickFormatError(f"nao e um stream LZMA valido: {exc}") from exc


def decode_bi5(payload: bytes, hour: datetime, spec: InstrumentSpec) -> pd.DataFrame:
    """Decodifica uma hora de `.bi5` em DataFrame com as colunas do schema.

    `hour` e a hora UTC do arquivo: os registros guardam apenas o offset em
    milissegundos dentro dela. Sem a hora correta, os timestamps saem
    plausiveis e errados.
    """
    if hour.tzinfo is None:
        raise ValueError("hour precisa ser timezone-aware em UTC")
    base = hour.astimezone(timezone.utc)

    blob = _decompress(payload)
    if not blob:
        return empty_frame()

    if len(blob) % RECORD_SIZE != 0:
        raise TickFormatError(
            f"{len(blob)} bytes nao e multiplo de {RECORD_SIZE}: "
            "arquivo truncado ou formato diferente do esperado"
        )

    n = len(blob) // RECORD_SIZE
    arr = np.frombuffer(blob, dtype=">u4,>u4,>u4,>f4,>f4", count=n)
    ms = arr["f0"].astype("int64")
    ask_i = arr["f1"].astype("float64")
    bid_i = arr["f2"].astype("float64")

    ask = ask_i / spec.divisor
    bid = bid_i / spec.divisor

    # Ordem dos campos. Um punhado de ticks cruzados e ruido de feed; a maioria
    # cruzada significa que ask e bid estao trocados na leitura.
    cruzados = int((ask < bid).sum())
    if n and cruzados > n // 2:
        raise TickFormatError(
            f"{cruzados}/{n} ticks com ask < bid. O .bi5 guarda ASK antes de "
            "BID; a leitura parece invertida."
        )

    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(base, utc=True)
            + pd.to_timedelta(ms, unit="ms"),
            "bid": bid,
            "ask": ask,
            "bid_vol": arr["f4"].astype("float32"),
            "ask_vol": arr["f3"].astype("float32"),
        }
    )
    # O feed entrega ordenado, mas nao promete. Ordenacao instavel aqui
    # produziria barras erradas la na frente, sem sintoma no meio.
    return df.sort_values("ts_utc", kind="stable", ignore_index=True)


def empty_frame() -> pd.DataFrame:
    """Frame vazio com os tipos certos.

    Existe para que hora sem tick nao mude o schema da concatenacao — colunas
    de tipo `object` num Parquet de precos e um bug que so aparece na leitura.
    """
    return pd.DataFrame(
        {
            "ts_utc": pd.Series([], dtype="datetime64[ms, UTC]"),
            "bid": pd.Series([], dtype="float64"),
            "ask": pd.Series([], dtype="float64"),
            "bid_vol": pd.Series([], dtype="float32"),
            "ask_vol": pd.Series([], dtype="float32"),
        }
    )


# -------------------------------------------------------- validacao de escala


@dataclass(frozen=True)
class ScaleCheck:
    ok: bool
    reason: str
    ratio: float | None = None
    factor: float | None = None


def check_scale_continuity(
    first_bid: float,
    prev_last_bid: float | None,
    gap_s: float | None,
    spec: InstrumentSpec,
    *,
    tol: float = 0.05,
    max_gap_s: float = 7200.0,
) -> ScaleCheck:
    """Valida a escala por CONTINUIDADE com a hora anterior.

    Preco nao teleporta. O primeiro tick de uma hora tem de cair muito perto do
    ultimo tick da hora anterior, e essa e uma verificacao ordens de grandeza
    mais apertada que uma faixa absoluta — nao depende de saber quanto valia o
    ouro em 2024, e nao passa por sorte.

    Faixa absoluta larga falha justamente onde deveria pegar: com ouro a 3300,
    um erro de fator 10 da 33000 ou 330, e uma faixa [500, 20000] deixa 330
    passar tao mal quanto 33000 — funciona por acidente do numero escolhido.

    Tres desfechos:

    - razao perto de 1        escala consistente
    - razao perto de 10^k     ERRO DE ESCALA, com o fator diagnosticado
    - qualquer outra coisa    movimento de mercado; nao e problema de escala

    Quando nao ha hora anterior utilizavel — inicio da serie, ou hiato maior
    que `max_gap_s`, que e o caso do fim de semana e da pausa diaria — cai para
    a faixa absoluta como rede secundaria.
    """
    lo, hi = spec.plausible_range

    sem_ancora = (
        prev_last_bid is None
        or prev_last_bid <= 0
        or (gap_s is not None and gap_s > max_gap_s)
    )
    if sem_ancora:
        if lo <= first_bid <= hi:
            return ScaleCheck(True, "faixa absoluta (sem hora anterior contigua)")
        return ScaleCheck(
            False,
            f"{first_bid:.4f} fora da faixa plausivel [{lo}, {hi}] e sem hora "
            "anterior contigua para comparar",
        )

    assert prev_last_bid is not None
    ratio = first_bid / prev_last_bid

    if abs(ratio - 1.0) <= tol:
        return ScaleCheck(True, "continuidade com a hora anterior", ratio=ratio)

    for f in _SCALE_FACTORS:
        if abs(ratio / f - 1.0) <= tol:
            return ScaleCheck(
                False,
                f"razao {ratio:.4g} com a hora anterior indica erro de escala "
                f"por fator {f:g}. Verifique price_decimals="
                f"{spec.price_decimals} para {spec.name}.",
                ratio=ratio,
                factor=f,
            )

    return ScaleCheck(
        True,
        f"descontinuidade de {(ratio - 1) * 100:+.2f}% nao corresponde a "
        "nenhum fator de escala: movimento de mercado, nao erro de leitura",
        ratio=ratio,
    )


# ----------------------------------------------------------------- ingestao


def _hours_of_month(year: int, month: int):
    cur = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    while cur < end:
        yield cur
        cur += timedelta(hours=1)


def parquet_path(instrument: str, year: int, month: int, root: Path | None = None) -> Path:
    """Particionamento hive por ano/mes: pyarrow e duckdb leem sem configuracao."""
    base = root if root is not None else ticks_dir()
    return base / instrument / f"year={year:04d}" / f"month={month:02d}" / "ticks.parquet"


def parse_month(
    instrument: str,
    year: int,
    month: int,
    *,
    spec: InstrumentSpec | None = None,
    raw_root: Path | None = None,
    logger: JsonlLogger | None = None,
    strict: bool = True,
) -> pd.DataFrame:
    """Le todas as horas do mes a partir do bruto e devolve um frame unico.

    A validacao de escala roda hora a hora, encadeada: cada hora e ancorada na
    ultima que trouxe ticks. `strict=True` levanta em vez de devolver dado com
    escala suspeita — dado com escala errada e pior que dado nenhum, porque
    parece utilizavel.
    """
    spec = spec or InstrumentSpec.load(instrument)
    raw_base = raw_root if raw_root is not None else raw_dir(FEED)

    partes: list[pd.DataFrame] = []
    prev_last_bid: float | None = None
    prev_last_ts: pd.Timestamp | None = None
    lidas = ausentes = vazias = 0

    for hora in _hours_of_month(year, month):
        p = raw_path(instrument, hora, raw_base)
        if not p.exists():
            if p.with_name(p.name + ABSENT_SUFFIX).exists():
                ausentes += 1
            continue

        df = decode_bi5(p.read_bytes(), hora, spec)
        lidas += 1
        if df.empty:
            vazias += 1
            continue

        first_bid = float(df["bid"].iloc[0])
        gap_s = (
            float((df["ts_utc"].iloc[0] - prev_last_ts).total_seconds())
            if prev_last_ts is not None
            else None
        )
        check = check_scale_continuity(first_bid, prev_last_bid, gap_s, spec)
        if not check.ok:
            if logger:
                logger.error(
                    "E5003", event="scale_mismatch", instrument=instrument,
                    hora=hora.isoformat(), motivo=check.reason,
                    ratio=check.ratio, factor=check.factor,
                )
            if strict:
                raise ScaleError(f"{instrument} {hora:%Y-%m-%d %Hh}: {check.reason}")
        elif check.ratio is not None and abs(check.ratio - 1.0) > 0.05 and logger:
            logger.warn(
                event="price_gap", instrument=instrument,
                hora=hora.isoformat(), motivo=check.reason, ratio=check.ratio,
            )

        partes.append(df)
        prev_last_bid = float(df["bid"].iloc[-1])
        prev_last_ts = df["ts_utc"].iloc[-1]

    out = (
        pd.concat(partes, ignore_index=True)
        if partes
        else empty_frame()
    )

    if logger:
        logger.info(
            event="parse_month", feed=FEED, instrument=instrument,
            year=year, month=month, horas_lidas=lidas, horas_vazias=vazias,
            horas_ausentes=ausentes, ticks=len(out),
            unit=spec.unit, price_decimals=spec.price_decimals,
        )
    return out


def write_month(
    df: pd.DataFrame,
    instrument: str,
    year: int,
    month: int,
    *,
    root: Path | None = None,
) -> Path:
    """Grava o Parquet do mes, atomicamente.

    Escreve num temporario e renomeia: um Parquet truncado por interrupcao e
    lido como valido em algumas ferramentas e falha em outras, e o modo de
    falha depende de quem le.
    """
    dest = parquet_path(instrument, year, month, root)
    dest.parent.mkdir(parents=True, exist_ok=True)

    table = pa.Table.from_pandas(
        df.astype(
            {
                "bid": "float64", "ask": "float64",
                "bid_vol": "float32", "ask_vol": "float32",
            }
        ),
        preserve_index=False,
    )
    tmp = dest.with_suffix(".parquet.part")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(dest)
    return dest


def read_month(
    instrument: str, year: int, month: int, *, root: Path | None = None
) -> pd.DataFrame:
    """Le o Parquet de um mes. Frame vazio com schema se nao existir."""
    p = parquet_path(instrument, year, month, root)
    if not p.exists():
        return empty_frame()
    return pd.read_parquet(p)


def read_month_with_overlap(
    instrument: str,
    year: int,
    month: int,
    *,
    root: Path | None = None,
    overlap_h: float = 72.0,
) -> pd.DataFrame:
    """O mes, mais as primeiras horas do mes seguinte.

    Existe por causa da regra 2 do agregador: a ultima barra de um lote nunca e
    emitida, porque nenhum tick a fecha. Agregando mes a mes — que e o caminho
    natural, ja que o Parquet e mensal — isso perderia a ultima barra de cada
    mes, sempre. Dois anos de M5 perderiam 24 barras, todas na virada de mes:
    nao e ruido, e um padrao alinhado ao calendario, e qualquer sensor com
    componente sazonal o transforma em vies.

    Bastam alguns ticks do mes seguinte para que a barra de fronteira feche
    corretamente; as barras que caem fora do mes sao descartadas depois.

    72 horas por padrao, e nao uma: a virada de mes cai em fim de semana com
    frequencia, e nesse caso a primeira hora do mes seguinte nao tem tick
    nenhum. Ler pouco demais reintroduziria o mesmo bug, so que de forma
    intermitente — o que e pior, porque parece resolvido na maior parte das
    vezes.
    """
    base = read_month(instrument, year, month, root=root)

    nxt_y, nxt_m = (year + 1, 1) if month == 12 else (year, month + 1)
    p = parquet_path(instrument, nxt_y, nxt_m, root)
    if not p.exists():
        # Sem mes seguinte no disco nao ha o que emprestar. A ultima barra fica
        # de fora, corretamente: nao existe tick que a feche.
        return base

    limite = pd.Timestamp(
        datetime(nxt_y, nxt_m, 1, tzinfo=timezone.utc)
    ) + pd.Timedelta(hours=overlap_h)
    extra = pd.read_parquet(p, filters=[("ts_utc", "<", limite)])
    if extra.empty:
        return base
    return pd.concat([base, extra], ignore_index=True)


def ingest_month(
    instrument: str,
    year: int,
    month: int,
    *,
    raw_root: Path | None = None,
    out_root: Path | None = None,
    logger: JsonlLogger | None = None,
    strict: bool = True,
) -> tuple[Path, int]:
    """Parseia e grava um mes. Devolve (caminho, numero de ticks)."""
    df = parse_month(
        instrument, year, month,
        raw_root=raw_root, logger=logger, strict=strict,
    )
    path = write_month(df, instrument, year, month, root=out_root)
    return path, len(df)


# ------------------------------------------------------------------ resumo


def summarize(df: pd.DataFrame) -> dict[str, object]:
    """Numeros de sanidade de um mes parseado.

    Spread em USD por onca, nunca em pontos: ponto e unidade de corretora e nem
    sequer esta definido para um feed de referencia.
    """
    if df.empty:
        return {"ticks": 0}

    spread = df["ask"] - df["bid"]
    por_hora = df.groupby(df["ts_utc"].dt.hour).size()
    return {
        "ticks": int(len(df)),
        "de": df["ts_utc"].iloc[0].isoformat(),
        "ate": df["ts_utc"].iloc[-1].isoformat(),
        "bid_min": float(df["bid"].min()),
        "bid_max": float(df["bid"].max()),
        "spread_usd_oz": {
            "min": float(spread.min()),
            "p50": float(spread.median()),
            "p95": float(spread.quantile(0.95)),
            "max": float(spread.max()),
            "negativos": int((spread < 0).sum()),
        },
        "ticks_por_hora_utc": {int(h): int(v) for h, v in por_hora.items()},
    }
