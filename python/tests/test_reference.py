"""Testes do comparador de barras contra a referencia da Dukascopy.

Estes testes existem por um motivo especifico e vale escreve-lo: se o passo de
validacao sair com **divergencia zero**, isso precisa ser distinguivel de "o
comparador nao esta comparando". Verde num repositorio limpo nao distingue "nao
ha achado" de "nao consigo achar" — invariante 10.

Por isso a maior parte do que esta aqui injeta divergencia CONHECIDA e verifica
que ela e reportada com o valor exato.

Fixtures gerados em codigo (ADR 0001). Nenhum .bi5 no repositorio.
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from riser.data.ticks import InstrumentSpec
from riser.harness.dukascopy_reference import (
    CANDLE_SIZE,
    ReferenceFormatError,
    compare_ohlc,
    decode_candles,
    reference_path,
    reference_url,
    top_divergences,
)
from riser.data.dukascopy import FeedConfig

M_INI = datetime(2026, 7, 1, tzinfo=timezone.utc)


@pytest.fixture()
def spec() -> InstrumentSpec:
    return InstrumentSpec.load("XAUUSD")


@pytest.fixture()
def cfg() -> FeedConfig:
    return FeedConfig.load()


def barras(n: int = 6, base: float = 3300.0) -> pd.DataFrame:
    """Serie M1 sintetica, no schema que `bars.aggregate` produz."""
    linhas = []
    for i in range(n):
        o = base + i
        linhas.append(
            {
                "ts_utc": M_INI + timedelta(minutes=i),
                "open": o,
                "high": o + 0.80,
                "low": o - 0.40,
                "close": o + 0.20,
            }
        )
    df = pd.DataFrame(linhas)
    df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
    return df


def make_candles(bars: pd.DataFrame, spec: InstrumentSpec, *, ordem_ohlc: bool = False) -> bytes:
    """Monta o arquivo mensal de candles.

    Ordem real do formato: time, open, CLOSE, LOW, HIGH, volume.
    `ordem_ohlc=True` grava na ordem errada de proposito, para provar que o
    decodificador a recusa em vez de aceitar barras invertidas.
    """
    d = spec.divisor
    blob = b""
    for _, r in bars.iterrows():
        segs = int((r["ts_utc"] - pd.Timestamp(M_INI)).total_seconds())
        campos = (
            (r["open"], r["high"], r["low"], r["close"])
            if ordem_ohlc
            else (r["open"], r["close"], r["low"], r["high"])
        )
        blob += struct.pack(
            ">5if", segs, *[int(round(x * d)) for x in campos], 1.0
        )
    return lzma.compress(blob, format=lzma.FORMAT_ALONE)


# ------------------------------------------------------------- decodificacao


def test_decodifica_na_ordem_do_formato(spec):
    b = barras(3)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    assert len(ref) == 3
    for col in ("open", "high", "low", "close"):
        assert ref[col].round(6).tolist() == b[col].round(6).tolist()


def test_ordem_ohlc_errada_e_recusada(spec):
    """Ler como OHLC acusaria o AGREGADOR por um erro do leitor."""
    with pytest.raises(ReferenceFormatError, match="CLOSE, LOW, HIGH"):
        decode_candles(make_candles(barras(20), spec, ordem_ohlc=True), 2026, 7, 1, spec)


def test_timestamp_e_utc_a_partir_do_inicio_do_mes(spec):
    ref = decode_candles(make_candles(barras(3), spec), 2026, 7, 1, spec)
    assert ref["ts_utc"].iloc[0] == M_INI
    assert ref["ts_utc"].iloc[2] == M_INI + timedelta(minutes=2)
    assert str(ref["ts_utc"].dt.tz) == "UTC"


def test_tamanho_invalido_e_recusado(spec):
    lixo = lzma.compress(b"\x00" * (CANDLE_SIZE + 3), format=lzma.FORMAT_ALONE)
    with pytest.raises(ReferenceFormatError, match="multiplo"):
        decode_candles(lixo, 2026, 7, 1, spec)


def test_url_de_referencia_usa_mes_base_zero(cfg):
    assert reference_url(cfg, "XAUUSD", 2026, 7, 4).endswith(
        "/XAUUSD/2026/06/04/BID_candles_min_1.bi5"
    )


def test_referencia_guardada_longe_do_bruto(tmp_path):
    """Quem procurar fonte de dados nao pode tropecar na referencia."""
    p = reference_path("XAUUSD", 2026, 7, 4, tmp_path)
    assert p.relative_to(tmp_path).as_posix() == "XAUUSD/2026/07/04_BID_candles_min_1.bi5"


# ------------------------------------------- ACUSACAO: divergencia conhecida


def test_series_identicas_dao_divergencia_zero(spec):
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    d = compare_ohlc(b, ref)
    assert d.comuns == 6
    assert d.so_nossas == 0 and d.so_referencia == 0
    assert d.max_geral == pytest.approx(0.0, abs=1e-9)


def test_acusa_divergencia_de_preco_injetada(spec):
    """0,50 USD/oz no high de UMA barra: valor e local tem de sair certos."""
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)

    nosso = b.copy()
    nosso.loc[3, "high"] = nosso.loc[3, "high"] + 0.50

    d = compare_ohlc(nosso, ref)
    assert d.comuns == 6, "deslocar preco nao pode mudar o alinhamento temporal"
    assert d.max_abs["high"] == pytest.approx(0.50, abs=1e-9)
    assert d.max_abs["open"] == pytest.approx(0.0, abs=1e-9)
    assert d.max_abs["low"] == pytest.approx(0.0, abs=1e-9)
    assert d.max_abs["close"] == pytest.approx(0.0, abs=1e-9)
    assert d.pior["campo"] == "high"
    assert d.pior["ts_utc"] == (M_INI + timedelta(minutes=3)).isoformat()
    assert d.pior["delta"] == pytest.approx(0.50, abs=1e-9)


def test_acusa_deslocamento_temporal_injetado(spec):
    """Uma barra deslocada em um intervalo: some dos comuns e aparece nos dois lados."""
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)

    nosso = b.copy()
    nosso.loc[4, "ts_utc"] = nosso.loc[4, "ts_utc"] + timedelta(minutes=10)

    d = compare_ohlc(nosso, ref)
    assert d.comuns == 5, "a barra deslocada nao pode continuar a casar"
    assert d.so_nossas == 1, "o rotulo novo so existe do nosso lado"
    assert d.so_referencia == 1, "o rotulo original so existe na referencia"


def test_acusa_as_duas_divergencias_ao_mesmo_tempo(spec):
    """Injecao combinada: preco e tempo tem de ser reportados em separado."""
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)

    nosso = b.copy()
    nosso.loc[3, "high"] = nosso.loc[3, "high"] + 0.50
    nosso.loc[4, "ts_utc"] = nosso.loc[4, "ts_utc"] + timedelta(minutes=10)

    d = compare_ohlc(nosso, ref)
    assert d.max_abs["high"] == pytest.approx(0.50, abs=1e-9)
    assert d.pior["campo"] == "high"
    assert d.so_nossas == 1 and d.so_referencia == 1
    assert d.comuns == 5


def test_deslocamento_temporal_nao_polui_a_divergencia_de_preco(spec):
    """So o tempo desloca: a divergencia de OHLC tem de continuar zero.

    Sem isto, um erro de rotulo apareceria como erro de calculo e a
    investigacao comecaria no lugar errado.
    """
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)

    nosso = b.copy()
    nosso.loc[4, "ts_utc"] = nosso.loc[4, "ts_utc"] + timedelta(minutes=10)

    d = compare_ohlc(nosso, ref)
    assert d.max_geral == pytest.approx(0.0, abs=1e-9)
    assert d.so_nossas == 1


def test_rotulo_deslocado_em_bloco_zera_os_comuns(spec):
    """O caso que a convencao de rotulo produziria: tudo deslocado um intervalo."""
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)

    nosso = b.copy()
    nosso["ts_utc"] = nosso["ts_utc"] + timedelta(minutes=1)

    d = compare_ohlc(nosso, ref)
    assert d.comuns == 5, "so as sobrepostas casam"
    assert d.so_nossas == 1 and d.so_referencia == 1
    # E o OHLC das que casam fica todo errado, porque comparam barras vizinhas.
    assert d.max_geral > 0.5


@pytest.mark.parametrize("campo", ["open", "high", "low", "close"])
def test_acusa_em_qualquer_campo(spec, campo):
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    nosso = b.copy()
    nosso.loc[2, campo] = nosso.loc[2, campo] + 0.25

    d = compare_ohlc(nosso, ref)
    assert d.max_abs[campo] == pytest.approx(0.25, abs=1e-9)
    assert d.pior["campo"] == campo


def test_top_divergences_ordena_pelas_piores(spec):
    b = barras(8)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    nosso = b.copy()
    nosso.loc[1, "high"] += 0.10
    nosso.loc[5, "low"] -= 0.75
    nosso.loc[6, "close"] += 0.30

    top = top_divergences(nosso, ref, n=3)
    assert len(top) == 3
    assert top["pior"].iloc[0] == pytest.approx(0.75, abs=1e-9)
    assert top.index[0] == M_INI + timedelta(minutes=5)
    assert top["pior"].is_monotonic_decreasing


def test_sem_rotulos_comuns_zero_nao_e_aprovacao(spec):
    """`max_geral == 0.0` sem comparacao nenhuma pareceria resultado perfeito."""
    b = barras(3)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    nosso = b.copy()
    nosso["ts_utc"] = nosso["ts_utc"] + timedelta(days=10)

    d = compare_ohlc(nosso, ref)
    assert d.comuns == 0
    assert d.max_geral == 0.0
    assert not d.comparou, "zero aqui e ausencia de comparacao, nao ausencia de erro"
    assert d.so_nossas == 3 and d.so_referencia == 3
    with pytest.raises(ValueError, match="nenhum rotulo em comum"):
        d.exigir_comparacao()


def test_comparacao_real_passa_no_portao(spec):
    b = barras(6)
    ref = decode_candles(make_candles(b, spec), 2026, 7, 1, spec)
    d = compare_ohlc(b, ref)
    assert d.comparou
    d.exigir_comparacao()  # nao levanta
