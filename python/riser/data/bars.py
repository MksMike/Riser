"""Agregador de barras. Barra e DERIVADA de tick, nunca baixada.

O que este modulo produz e **cache descartavel**: apagar `RISER-data\\bars\\`
inteiro nao perde nada, porque tudo se reconstroi dos ticks. O que nao se
reconstroi e o tick, e por isso ele e que e guardado com cuidado.

Este arquivo e a REFERENCIA da futura versao MQL5. A paridade Python-MQL5 e o
criterio de saida da Camada 1, e ela so e alcancavel se houver uma unica funcao
de agregacao, pequena o bastante para ser reimplementada linha a linha. Toda a
logica vive em `aggregate`; M1, M5, M15 e M30 sao chamadas dela.

Este modulo nao conhece nenhum sensor (ADR 0007). Ele expoe `range_usd_oz` como
materia-prima; quem precisar de mediana, percentil ou baseline calcula a sua, a
partir dos ticks, na janela que for sua.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from riser.core.paths import bars_dir

# Timeframes suportados, em segundos. O nome e o do MT5 para que a comparacao
# de paridade nao precise de tradutor.
TIMEFRAMES: dict[str, int] = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800}

COLUMNS = (
    "ts_utc", "open", "high", "low", "close", "range_usd_oz", "n_ticks",
)


@dataclass(frozen=True)
class BarRules:
    """As tres regras de fronteira, num lugar so, para serem citadas nos testes.

    Estao aqui como dados e nao so como comentario porque a versao MQL5 vai ser
    escrita a partir deste arquivo, e uma regra de fronteira que existe apenas
    na cabeca de quem escreveu nao sobrevive a reimplementacao.
    """

    label = "inicio do intervalo"
    incomplete = "barra em formacao nao e emitida"
    empty = "intervalo sem tick nao gera barra"


RULES = BarRules()


def empty_bars() -> pd.DataFrame:
    """Frame vazio com os tipos certos, para nao mudar schema na concatenacao."""
    return pd.DataFrame(
        {
            "ts_utc": pd.Series([], dtype="datetime64[ms, UTC]"),
            "open": pd.Series([], dtype="float64"),
            "high": pd.Series([], dtype="float64"),
            "low": pd.Series([], dtype="float64"),
            "close": pd.Series([], dtype="float64"),
            "range_usd_oz": pd.Series([], dtype="float64"),
            "n_ticks": pd.Series([], dtype="int64"),
        }
    )


def aggregate(ticks: pd.DataFrame, seconds: int, *, price: str = "bid") -> pd.DataFrame:
    """Agrega ticks em barras de `seconds`. UNICA funcao de agregacao.

    Preco a partir do **BID** por padrao: a Exness plota em bid, e uma barra
    Python em mid nunca casaria com o que aparece no grafico nem com o que o
    MT5 devolve. O parametro existe para diagnostico, nao para uso corrente.

    Unidades: USD por onca em todas as colunas de preco. Nao ha ponto nem lote
    aqui, e nao pode haver.

    ------------------------------------------------------------------ FRONTEIRA

    **1. Rotulo e o INICIO do intervalo.**  A barra M5 rotulada 14:00 cobre
    [14:00, 14:05). A convencao nao e preferencia: `iTime()` no MT5 devolve a
    abertura da barra. Rotular pelo fecho faria toda comparacao de paridade
    divergir por exatamente um intervalo, e o sintoma seria procurado como erro
    de calculo durante dias, num lugar onde ha apenas convencao de rotulo.

    A grade e ancorada na epoca Unix, entao os limites caem em multiplos exatos
    do intervalo — M5 em :00, :05, :10; M30 em :00 e :30 — igual ao MT5.

    **2. Barra em formacao NAO e emitida.**  Uma M5 rotulada 14:00 so sai
    quando ja existe tick em 14:05 ou depois. Enquanto nao existe, ela ainda
    pode mudar, e entregar uma barra que ainda vai mudar e lookahead disfarcado
    de conveniencia — o consumidor leria um `high` que nao e o `high` final.
    Isto e o invariante 4.

    Consequencia pratica: a ultima barra de qualquer lote fica sempre de fora.
    Agregar mes a mes de forma independente perde uma barra em cada fronteira
    de mes. Quem precisa de serie continua agrega sobre o intervalo continuo,
    ou inclui os primeiros ticks do periodo seguinte. Nao ha parametro para
    desligar isto — a conveniencia de ter a ultima barra e exatamente o que
    torna o lookahead atraente.

    **3. Intervalo sem tick NAO gera barra.**  Nada de repetir o fecho
    anterior. Na sessao asiatica isso e comum, e uma barra sintetica de range
    zero envenenaria a baseline por horario do SVC precisamente nas horas em
    que ela mais importa — a normalizacao passaria a comparar o mercado real
    com um mercado inventado. Ausencia de barra e informacao; barra preenchida
    e dado fabricado.

    **A pausa diaria nao precisa de regra propria.**  Durante a pausa nao ha
    tick, e pela regra 3 nao ha barra. Nenhum horario de pausa aparece neste
    modulo, e e assim que tem de ser: a pausa e propriedade do servidor da
    corretora, o seu instante depende de `server.timezone_offset` — que ainda
    esta marcado VERIFICAR nos manifestos — e o agregador opera em UTC puro.
    Codificar a pausa aqui congelaria uma suposicao nao medida e quebraria em
    qualquer feed com sessao diferente, incluindo o BTCUSD, que nao tem pausa.

    A barra que atravessa o inicio da pausa e emitida normalmente, com os ticks
    que existiram ate ela: uma M30 de 20:30 contem [20:30, 21:00) inteiro, e
    que a sessao tenha parado as 20:58 nao muda o intervalo. Ela so sai quando
    chega o primeiro tick da reabertura, pela regra 2 — o fecho e dirigido por
    evento, nao por relogio.
    """
    if seconds <= 0:
        raise ValueError(f"seconds precisa ser positivo: {seconds}")
    if price not in ("bid", "ask"):
        raise ValueError(f"price precisa ser 'bid' ou 'ask': {price!r}")
    if price not in ticks.columns:
        raise KeyError(f"coluna {price!r} ausente dos ticks")

    if ticks.empty:
        return empty_bars()

    ts = pd.to_datetime(ticks["ts_utc"], utc=True)
    if not ts.is_monotonic_increasing:
        raise ValueError(
            "ticks fora de ordem cronologica. Ordenar aqui esconderia um "
            "problema a montante, no parser ou na concatenacao."
        )

    # Regra 1: inicio do intervalo, grade ancorada na epoca.
    bucket = ts.dt.floor(f"{seconds}s")

    px = ticks[price].astype("float64")
    g = pd.DataFrame({"bucket": bucket, "px": px}).groupby("bucket", sort=True)["px"]

    bars = pd.DataFrame(
        {
            "open": g.first(),
            "high": g.max(),
            "low": g.min(),
            "close": g.last(),
            "n_ticks": g.size().astype("int64"),
        }
    )
    # Regra 3 e implicita no groupby: bucket sem tick nao existe como grupo,
    # logo nao vira linha. Nao ha reindex, nao ha ffill, nao ha preenchimento.

    # Regra 2: o ultimo bucket com ticks nao tem nenhum tick posterior que o
    # feche, portanto ainda esta em formacao.
    if len(bars):
        bars = bars.iloc[:-1]

    bars = bars.reset_index().rename(columns={"bucket": "ts_utc"})
    bars["range_usd_oz"] = bars["high"] - bars["low"]
    return bars[list(COLUMNS)].reset_index(drop=True)


def aggregate_tf(ticks: pd.DataFrame, timeframe: str, **kwargs) -> pd.DataFrame:
    """`aggregate` pelo nome do timeframe do MT5."""
    if timeframe not in TIMEFRAMES:
        raise KeyError(
            f"timeframe {timeframe!r} desconhecido; esperado um de "
            f"{sorted(TIMEFRAMES)}"
        )
    return aggregate(ticks, TIMEFRAMES[timeframe], **kwargs)


def aggregate_all(ticks: pd.DataFrame, **kwargs) -> dict[str, pd.DataFrame]:
    """Todos os timeframes, cada um agregado dos TICKS.

    Nao se agrega M5 a partir de M1. Encadear agregacoes propaga a barra em
    formacao de um nivel para o seguinte e faz a fronteira depender da ordem em
    que os niveis foram calculados — um erro que nao aparece no meio da serie,
    so nas pontas, que e onde ninguem olha.
    """
    return {tf: aggregate_tf(ticks, tf, **kwargs) for tf in TIMEFRAMES}


# ------------------------------------------------------------ armazenamento


def bars_path(
    instrument: str, timeframe: str, year: int, month: int, root: Path | None = None
) -> Path:
    base = root if root is not None else bars_dir()
    return (
        base / instrument / timeframe / f"year={year:04d}" / f"month={month:02d}"
        / "bars.parquet"
    )


def write_bars(
    bars: pd.DataFrame,
    instrument: str,
    timeframe: str,
    year: int,
    month: int,
    *,
    root: Path | None = None,
) -> Path:
    """Grava o cache de barras, atomicamente.

    Cache descartavel: se este arquivo sumir, `aggregate` o reconstroi dos
    ticks. Por isso nao ha aqui nenhuma validacao de continuidade — o que
    precisava ser validado foi validado em `ticks.py`, sobre o dado que nao se
    reconstroi.
    """
    dest = bars_path(instrument, timeframe, year, month, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(bars, preserve_index=False)
    tmp = dest.with_suffix(".parquet.part")
    pq.write_table(table, tmp, compression="zstd")
    tmp.replace(dest)
    return dest
