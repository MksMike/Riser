"""Testes do parser de ticks.

Todos os `.bi5` sao gerados em codigo (ADR 0001). Nenhum arquivo binario no
repositorio: o que o teste exercita fica legivel no diff, e nada congela uma
suposicao que ainda nao foi medida.
"""

from __future__ import annotations

import lzma
import struct
from datetime import datetime, timezone

import pandas as pd
import pytest

from riser.data.ticks import (
    RECORD_SIZE,
    InstrumentSpec,
    ScaleError,
    TickFormatError,
    check_scale_continuity,
    decode_bi5,
    empty_frame,
    ingest_month,
    parse_month,
    parquet_path,
    read_month,
    read_month_with_overlap,
    summarize,
)
from riser.data.dukascopy import raw_path

H = datetime(2026, 7, 15, 13, tzinfo=timezone.utc)


@pytest.fixture()
def spec() -> InstrumentSpec:
    """Le a config real: mudanca de forma nela tem de quebrar o teste."""
    return InstrumentSpec.load("XAUUSD")


def make_bi5(ticks, *, compress: bool = True) -> bytes:
    """Monta um .bi5 sintetico.

    `ticks` e uma lista de (ms, ask_int, bid_int, ask_vol, bid_vol). Os inteiros
    sao os do formato: ASK primeiro, BID depois — a ordem que o parser tem de
    respeitar.
    """
    blob = b"".join(struct.pack(">IIIff", *t) for t in ticks)
    if not compress:
        return blob
    return lzma.compress(blob, format=lzma.FORMAT_ALONE)


def ticks_em(preco_bid: float, spec: InstrumentSpec, *, n: int = 3, spread: float = 0.30):
    """n ticks a partir de um preco em USD/oz, com spread em USD/oz."""
    b = int(round(preco_bid * spec.divisor))
    a = int(round((preco_bid + spread) * spec.divisor))
    return [(i * 1000, a, b, 1.5, 2.5) for i in range(n)]


# ------------------------------------------------------------- decodificacao


def test_decodifica_precos_em_usd_por_onca(spec):
    df = decode_bi5(make_bi5(ticks_em(3312.45, spec, n=2)), H, spec)
    assert len(df) == 2
    assert df["bid"].iloc[0] == pytest.approx(3312.45, abs=1e-6)
    assert df["ask"].iloc[0] == pytest.approx(3312.75, abs=1e-6)
    assert spec.unit == "usd_per_oz"


def test_guarda_o_ask(spec):
    """Sem ask nao se reconstroi custo nem preco de compra."""
    df = decode_bi5(make_bi5(ticks_em(3300.0, spec, spread=0.42)), H, spec)
    assert "ask" in df.columns
    assert (df["ask"] > df["bid"]).all()
    assert (df["ask"] - df["bid"]).iloc[0] == pytest.approx(0.42, abs=1e-6)


def test_colunas_e_ordem_do_schema(spec):
    df = decode_bi5(make_bi5(ticks_em(3300.0, spec)), H, spec)
    assert list(df.columns) == ["ts_utc", "bid", "ask", "bid_vol", "ask_vol"]


def test_timestamp_e_utc_explicito(spec):
    df = decode_bi5(make_bi5([(0, 3300300, 3300000, 1.0, 1.0)]), H, spec)
    assert str(df["ts_utc"].dt.tz) == "UTC"
    assert df["ts_utc"].iloc[0] == datetime(2026, 7, 15, 13, 0, 0, tzinfo=timezone.utc)


def test_offset_em_ms_soma_a_hora_do_arquivo(spec):
    df = decode_bi5(make_bi5([(3_599_999, 3300300, 3300000, 1.0, 1.0)]), H, spec)
    esperado = datetime(2026, 7, 15, 13, 59, 59, 999_000, tzinfo=timezone.utc)
    assert df["ts_utc"].iloc[0] == esperado


def test_volumes_separados_por_lado(spec):
    df = decode_bi5(make_bi5([(0, 3300300, 3300000, 7.5, 2.25)]), H, spec)
    assert df["ask_vol"].iloc[0] == pytest.approx(7.5)
    assert df["bid_vol"].iloc[0] == pytest.approx(2.25)


def test_hora_vazia_devolve_frame_vazio_com_tipos(spec):
    df = decode_bi5(b"", H, spec)
    assert df.empty
    assert list(df.columns) == list(empty_frame().columns)
    assert df["bid"].dtype == "float64"


def test_ordena_por_tempo(spec):
    fora_de_ordem = [(2000, 3300300, 3300000, 1.0, 1.0),
                     (1000, 3300200, 3299900, 1.0, 1.0)]
    df = decode_bi5(make_bi5(fora_de_ordem), H, spec)
    assert df["ts_utc"].is_monotonic_increasing


def test_hora_naive_e_recusada(spec):
    with pytest.raises(ValueError, match="timezone-aware"):
        decode_bi5(make_bi5(ticks_em(3300.0, spec)), datetime(2026, 7, 15, 13), spec)


# ------------------------------------------------- ordem ask/bid no registro


def test_ask_antes_de_bid_no_registro(spec):
    """Trocar os dois daria spread negativo em todo tick, sem erro de parse."""
    df = decode_bi5(make_bi5([(0, 3300500, 3300000, 1.0, 1.0)]), H, spec)
    assert df["ask"].iloc[0] > df["bid"].iloc[0]


def test_maioria_cruzada_e_recusada(spec):
    invertido = [(i * 100, 3300000, 3300500, 1.0, 1.0) for i in range(10)]
    with pytest.raises(TickFormatError, match="ask < bid"):
        decode_bi5(make_bi5(invertido), H, spec)


def test_poucos_cruzados_passam(spec):
    """Um punhado de ticks cruzados e ruido de feed, nao erro de leitura."""
    ticks = [(i * 100, 3300500, 3300000, 1.0, 1.0) for i in range(9)]
    ticks.append((900, 3300000, 3300500, 1.0, 1.0))
    assert len(decode_bi5(make_bi5(ticks), H, spec)) == 10


def test_tamanho_nao_multiplo_e_recusado(spec):
    lixo = lzma.compress(b"\x00" * (RECORD_SIZE + 7), format=lzma.FORMAT_ALONE)
    with pytest.raises(TickFormatError, match="multiplo"):
        decode_bi5(lixo, H, spec)


def test_conteudo_nao_lzma_e_recusado(spec):
    with pytest.raises(TickFormatError, match="LZMA"):
        decode_bi5(b"isto nao e lzma nem por acaso", H, spec)


# ------------------------------------------------------- escala: continuidade


def test_continuidade_aceita_hora_seguinte(spec):
    c = check_scale_continuity(3301.0, 3300.0, gap_s=1.0, spec=spec)
    assert c.ok and c.ratio == pytest.approx(3301 / 3300)


@pytest.mark.parametrize("fator", [0.001, 0.01, 0.1, 10.0, 100.0, 1000.0])
def test_continuidade_diagnostica_o_fator_do_erro(spec, fator):
    c = check_scale_continuity(3300.0 * fator, 3300.0, gap_s=1.0, spec=spec)
    assert not c.ok
    assert c.factor == pytest.approx(fator)
    assert "price_decimals" in c.reason


def test_erro_de_fator_10_e_pego_onde_a_faixa_absoluta_falharia(spec):
    """Ouro a 3300 com fator 0.1 da 330 — dentro de [500,20000]? nao, mas 3300
    com fator 10 da 33000, que tambem sai. O ponto e nao depender disso."""
    lo, hi = spec.plausible_range
    errado = 3300.0 / 10
    c = check_scale_continuity(errado, 3300.0, gap_s=1.0, spec=spec)
    assert not c.ok and c.factor == pytest.approx(0.1)
    # A continuidade pega mesmo quando o valor errado cai DENTRO da faixa.
    dentro = 3300.0 / 10 * 2  # 660, dentro de [500, 20000]
    assert lo <= dentro <= hi
    c2 = check_scale_continuity(660.0, 6600.0, gap_s=1.0, spec=spec)
    assert not c2.ok and c2.factor == pytest.approx(0.1)


def test_variacao_dentro_da_tolerancia_e_continuidade(spec):
    """3% entre horas contiguas e mercado normal, nao merece nota."""
    c = check_scale_continuity(3400.0, 3300.0, gap_s=1.0, spec=spec)
    assert c.ok and c.factor is None
    assert "continuidade" in c.reason


def test_movimento_de_mercado_nao_e_erro_de_escala(spec):
    """Descontinuidade grande que nao corresponde a potencia de 10 e o mercado.

    O ramo existe para nao classificar um salto de noticia como escala errada:
    recusar o mes inteiro por causa disso custaria dado real.
    """
    c = check_scale_continuity(3600.0, 3300.0, gap_s=1.0, spec=spec)
    assert c.ok
    assert c.factor is None
    assert "movimento de mercado" in c.reason


def test_hiato_grande_cai_para_faixa_absoluta(spec):
    """Fim de semana e a pausa diaria quebram a continuidade legitimamente."""
    c = check_scale_continuity(3300.0, 3300.0, gap_s=60 * 60 * 60, spec=spec)
    assert c.ok and "faixa absoluta" in c.reason


def test_sem_hora_anterior_usa_faixa_absoluta(spec):
    assert check_scale_continuity(3300.0, None, None, spec).ok
    ruim = check_scale_continuity(3.3, None, None, spec)
    assert not ruim.ok and "faixa plausivel" in ruim.reason


def test_faixa_absoluta_e_so_rede_secundaria(spec):
    """Com ancora, um valor fora da faixa mas continuo nao e erro de escala."""
    c = check_scale_continuity(300.0, 299.0, gap_s=1.0, spec=spec)
    assert c.ok, "continuidade manda; faixa so vale sem ancora"


# --------------------------------------------------------------- mes inteiro


def _grava_hora(root, hora, payload):
    p = raw_path("XAUUSD", hora, root)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(payload)


def test_parse_month_encadeia_horas(tmp_path, spec):
    for i, preco in enumerate([3300.0, 3301.0, 3302.5]):
        _grava_hora(tmp_path, datetime(2026, 7, 1, i, tzinfo=timezone.utc),
                    make_bi5(ticks_em(preco, spec, n=2)))

    df = parse_month("XAUUSD", 2026, 7, spec=spec, raw_root=tmp_path)
    assert len(df) == 6
    assert df["ts_utc"].is_monotonic_increasing


def test_parse_month_recusa_escala_inconsistente(tmp_path, spec):
    _grava_hora(tmp_path, datetime(2026, 7, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=2)))
    # Hora seguinte com preco dez vezes maior: so pode ser escala.
    _grava_hora(tmp_path, datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
                make_bi5(ticks_em(33000.0, spec, n=2)))

    with pytest.raises(ScaleError, match="fator 10"):
        parse_month("XAUUSD", 2026, 7, spec=spec, raw_root=tmp_path)


def test_strict_false_deixa_passar_mas_e_explicito(tmp_path, spec):
    _grava_hora(tmp_path, datetime(2026, 7, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=2)))
    _grava_hora(tmp_path, datetime(2026, 7, 1, 1, tzinfo=timezone.utc),
                make_bi5(ticks_em(33000.0, spec, n=2)))
    df = parse_month("XAUUSD", 2026, 7, spec=spec, raw_root=tmp_path, strict=False)
    assert len(df) == 4


def test_mes_sem_bruto_devolve_vazio_com_schema(tmp_path, spec):
    df = parse_month("XAUUSD", 2026, 7, spec=spec, raw_root=tmp_path)
    assert df.empty
    assert list(df.columns) == list(empty_frame().columns)


def test_ingest_grava_parquet_particionado(tmp_path, spec):
    raw = tmp_path / "raw"
    out = tmp_path / "ticks"
    _grava_hora(raw, datetime(2026, 7, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=4)))

    path, n = ingest_month("XAUUSD", 2026, 7, raw_root=raw, out_root=out)
    assert n == 4
    assert path == parquet_path("XAUUSD", 2026, 7, out)
    assert path.relative_to(out).as_posix() == "XAUUSD/year=2026/month=07/ticks.parquet"
    assert list(path.parent.glob("*.part")) == []


def test_parquet_preserva_ask_e_tipos(tmp_path, spec):
    import pandas as pd

    raw, out = tmp_path / "raw", tmp_path / "ticks"
    _grava_hora(raw, datetime(2026, 7, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=3, spread=0.25)))
    path, _ = ingest_month("XAUUSD", 2026, 7, raw_root=raw, out_root=out)

    lido = pd.read_parquet(path)
    assert list(lido.columns) == ["ts_utc", "bid", "ask", "bid_vol", "ask_vol"]
    assert lido["bid"].dtype == "float64" and lido["ask"].dtype == "float64"
    assert (lido["ask"] - lido["bid"]).round(6).eq(0.25).all()
    assert str(lido["ts_utc"].dt.tz) == "UTC"


def test_overlap_traz_ticks_do_mes_seguinte(tmp_path, spec):
    """Sem eles a ultima barra do mes nunca fecha (regra 2 do agregador)."""
    raw, out = tmp_path / "raw", tmp_path / "ticks"
    _grava_hora(raw, datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=3)))
    _grava_hora(raw, datetime(2026, 8, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.5, spec, n=3)))
    ingest_month("XAUUSD", 2026, 7, raw_root=raw, out_root=out)
    ingest_month("XAUUSD", 2026, 8, raw_root=raw, out_root=out)

    so_julho = read_month("XAUUSD", 2026, 7, root=out)
    assert (so_julho["ts_utc"] < pd.Timestamp("2026-08-01", tz="UTC")).all()

    com = read_month_with_overlap("XAUUSD", 2026, 7, root=out)
    assert len(com) > len(so_julho)
    assert (com["ts_utc"] >= pd.Timestamp("2026-08-01", tz="UTC")).any()


def test_overlap_sem_mes_seguinte_nao_falha(tmp_path, spec):
    """Mes mais recente da serie: nao ha o que emprestar, e esta correto."""
    raw, out = tmp_path / "raw", tmp_path / "ticks"
    _grava_hora(raw, datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=3)))
    ingest_month("XAUUSD", 2026, 7, raw_root=raw, out_root=out)

    com = read_month_with_overlap("XAUUSD", 2026, 7, root=out)
    assert len(com) == 3


def test_overlap_atravessa_a_virada_de_ano(tmp_path, spec):
    raw, out = tmp_path / "raw", tmp_path / "ticks"
    _grava_hora(raw, datetime(2025, 12, 31, 23, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=2)))
    _grava_hora(raw, datetime(2026, 1, 1, 0, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.5, spec, n=2)))
    ingest_month("XAUUSD", 2025, 12, raw_root=raw, out_root=out)
    ingest_month("XAUUSD", 2026, 1, raw_root=raw, out_root=out)

    com = read_month_with_overlap("XAUUSD", 2025, 12, root=out)
    assert len(com) == 4


def test_overlap_cobre_fim_de_semana(tmp_path, spec):
    """Uma hora de overlap falharia quando a virada cai em fim de semana."""
    raw, out = tmp_path / "raw", tmp_path / "ticks"
    _grava_hora(raw, datetime(2026, 7, 31, 23, tzinfo=timezone.utc),
                make_bi5(ticks_em(3300.0, spec, n=2)))
    # Primeiro tick do mes seguinte so 50h depois: mercado fechado no meio.
    _grava_hora(raw, datetime(2026, 8, 3, 1, tzinfo=timezone.utc),
                make_bi5(ticks_em(3301.0, spec, n=2)))
    ingest_month("XAUUSD", 2026, 7, raw_root=raw, out_root=out)
    ingest_month("XAUUSD", 2026, 8, raw_root=raw, out_root=out)

    assert len(read_month_with_overlap("XAUUSD", 2026, 7, root=out, overlap_h=1.0)) == 2
    assert len(read_month_with_overlap("XAUUSD", 2026, 7, root=out)) == 4


def test_instrumento_desconhecido_nao_e_adivinhado():
    with pytest.raises(KeyError, match="price_decimals"):
        InstrumentSpec.load("NAOEXISTE")


# ------------------------------------------------------------------- resumo


def test_resumo_reporta_spread_em_usd_por_onca(spec):
    df = decode_bi5(make_bi5(ticks_em(3300.0, spec, n=5, spread=0.30)), H, spec)
    s = summarize(df)
    assert s["ticks"] == 5
    assert s["spread_usd_oz"]["p50"] == pytest.approx(0.30, abs=1e-6)
    assert s["spread_usd_oz"]["negativos"] == 0
    assert s["ticks_por_hora_utc"] == {13: 5}


def test_resumo_de_frame_vazio():
    assert summarize(empty_frame()) == {"ticks": 0}
