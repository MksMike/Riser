"""Testes de fronteira do agregador.

Este e o modulo que a versao MQL5 vai reimplementar, e a divergencia de paridade
mais provavel nao esta no calculo de OHLC — esta na convencao de rotulo e no
tratamento das pontas. Sao esses os casos testados aqui.

Fixtures gerados em codigo (ADR 0001). O caso da pausa diaria le a duracao do
manifesto da corretora em vez de a escrever: `server.timezone_offset` ainda esta
marcado VERIFICAR, e um fixture em arquivo congelaria essa suposicao.
"""

from __future__ import annotations

import ast
import math
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import yaml

from riser.core.paths import repo_root
from riser.data.bars import (
    RULES,
    TIMEFRAMES,
    aggregate,
    aggregate_all,
    aggregate_month,
    aggregate_tf,
    bars_path,
    empty_bars,
    write_bars,
)

T0 = datetime(2026, 7, 15, 14, 0, 0, tzinfo=timezone.utc)


def codigo_executavel(path) -> str:
    """Fonte sem docstrings nem comentarios.

    Os testes de acoplamento precisam distinguir 'o codigo depende disto' de 'o
    comentario explica isto'. Citar o SVC ao justificar uma regra de fronteira e
    exatamente o que a documentacao deve fazer; importar o SVC e que seria
    acoplamento. Ler o arquivo cru confundiria os dois e transformaria o teste
    numa proibicao de escrever comentario util.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        corpo = getattr(node, "body", None)
        if not isinstance(corpo, list) or not corpo:
            continue
        primeiro = corpo[0]
        if (
            isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and isinstance(primeiro, ast.Expr)
            and isinstance(primeiro.value, ast.Constant)
            and isinstance(primeiro.value.value, str)
        ):
            corpo.pop(0)
    return ast.unparse(tree)  # unparse descarta comentarios


def ticks(pares, *, spread: float = 0.30) -> pd.DataFrame:
    """(offset_segundos, bid) -> frame de ticks no schema do ticks.py."""
    ts = [T0 + timedelta(seconds=s) for s, _ in pares]
    bid = [b for _, b in pares]
    return pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(pd.Series(ts), utc=True),
            "bid": pd.Series(bid, dtype="float64"),
            "ask": pd.Series([b + spread for b in bid], dtype="float64"),
            "bid_vol": pd.Series([1.0] * len(bid), dtype="float32"),
            "ask_vol": pd.Series([1.0] * len(bid), dtype="float32"),
        }
    )


def linear(n: int, *, step_s: int = 1, start: float = 3300.0, inc: float = 0.01):
    return [(i * step_s, start + i * inc) for i in range(n)]


# ------------------------------------------------- regra 1: rotulo no inicio


def test_rotulo_e_o_inicio_do_intervalo():
    """iTime() no MT5 devolve a abertura. Rotular pelo fecho divergiria por um
    intervalo inteiro e o sintoma seria procurado como erro de calculo."""
    df = ticks([(0, 3300.0), (60, 3301.0), (120, 3302.0), (301, 3303.0)])
    bars = aggregate(df, 300)
    assert len(bars) == 1
    assert bars["ts_utc"].iloc[0] == T0
    assert RULES.label == "inicio do intervalo"


def test_grade_ancorada_na_epoca_como_no_mt5():
    """M5 cai em :00 :05 :10; M30 em :00 e :30, qualquer que seja o 1o tick."""
    base = datetime(2026, 7, 15, 14, 7, 30, tzinfo=timezone.utc)
    instantes = [base, base + timedelta(minutes=10), base + timedelta(minutes=40)]
    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(pd.Series(instantes), utc=True),
            "bid": pd.Series([3300.0, 3301.0, 3302.0], dtype="float64"),
        }
    )
    assert aggregate(df, 300)["ts_utc"].iloc[0] == datetime(
        2026, 7, 15, 14, 5, tzinfo=timezone.utc
    )
    assert aggregate(df, 1800)["ts_utc"].iloc[0] == datetime(
        2026, 7, 15, 14, 0, tzinfo=timezone.utc
    )


def test_tick_exatamente_na_fronteira_abre_a_barra_seguinte():
    """[14:00, 14:05) e semiaberto: 14:05:00.000 pertence a barra das 14:05."""
    df = ticks([(0, 3300.0), (299, 3301.0), (300, 3302.0), (600, 3303.0)])
    bars = aggregate(df, 300)
    assert list(bars["ts_utc"]) == [T0, T0 + timedelta(minutes=5)]
    assert bars["close"].iloc[0] == 3301.0, "tick de :00 do proximo nao entra"
    assert bars["open"].iloc[1] == 3302.0


# --------------------------------------- regra 2: barra em formacao nao sai


def test_barra_em_formacao_nao_e_emitida():
    """Enquanto nao chega tick do proximo intervalo, ela ainda pode mudar."""
    df = ticks([(0, 3300.0), (60, 3301.0), (120, 3302.0)])
    assert aggregate(df, 300).empty


def test_barra_sai_quando_chega_o_tick_seguinte():
    incompleta = ticks([(0, 3300.0), (60, 3305.0)])
    assert aggregate(incompleta, 300).empty

    completa = ticks([(0, 3300.0), (60, 3305.0), (300, 3306.0)])
    bars = aggregate(completa, 300)
    assert len(bars) == 1
    assert bars["high"].iloc[0] == 3305.0


def test_high_nao_pode_ser_lido_antes_de_existir():
    """O high final so e conhecido no fim do intervalo: emitir antes e lookahead."""
    parcial = ticks([(0, 3300.0), (10, 3301.0)])
    assert aggregate(parcial, 300).empty

    com_pico = ticks([(0, 3300.0), (10, 3301.0), (200, 3350.0), (300, 3302.0)])
    assert aggregate(com_pico, 300)["high"].iloc[0] == 3350.0


def test_virada_de_mes_nao_perde_a_ultima_barra():
    """Sem overlap seria uma barra perdida por mes, sempre no mesmo ponto do
    calendario — vies sazonal, nao ruido."""
    fim_do_mes = datetime(2026, 7, 31, 23, 40, tzinfo=timezone.utc)
    instantes = [fim_do_mes + timedelta(minutes=i) for i in range(20)]  # ate 23:59
    overlap = [datetime(2026, 8, 1, 0, 2, tzinfo=timezone.utc)]
    px = [3300.0 + i * 0.1 for i in range(len(instantes) + len(overlap))]
    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(pd.Series(instantes + overlap), utc=True),
            "bid": pd.Series(px, dtype="float64"),
        }
    )

    # Sem overlap a barra das 23:55 seria descartada por estar em formacao.
    sem = aggregate(df.iloc[: len(instantes)], 300)
    assert datetime(2026, 7, 31, 23, 55, tzinfo=timezone.utc) not in list(sem["ts_utc"])

    com = aggregate_month(df, 300, 2026, 7)
    assert com["ts_utc"].iloc[-1] == datetime(2026, 7, 31, 23, 55, tzinfo=timezone.utc)


def test_aggregate_month_descarta_barras_de_fora():
    fim_do_mes = datetime(2026, 7, 31, 23, 50, tzinfo=timezone.utc)
    instantes = [fim_do_mes + timedelta(minutes=i) for i in range(30)]  # entra agosto
    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(pd.Series(instantes), utc=True),
            "bid": pd.Series([3300.0 + i * 0.1 for i in range(30)], dtype="float64"),
        }
    )
    bars = aggregate_month(df, 300, 2026, 7)
    assert (bars["ts_utc"] < pd.Timestamp("2026-08-01", tz="UTC")).all()
    assert bars["ts_utc"].iloc[-1] == datetime(2026, 7, 31, 23, 55, tzinfo=timezone.utc)


def test_aggregate_month_atravessa_a_virada_de_ano():
    base = datetime(2025, 12, 31, 23, 50, tzinfo=timezone.utc)
    instantes = [base + timedelta(minutes=i) for i in range(30)]
    df = pd.DataFrame(
        {
            "ts_utc": pd.to_datetime(pd.Series(instantes), utc=True),
            "bid": pd.Series([3300.0] * 30, dtype="float64"),
        }
    )
    bars = aggregate_month(df, 300, 2025, 12)
    assert bars["ts_utc"].iloc[-1] == datetime(2025, 12, 31, 23, 55, tzinfo=timezone.utc)
    assert (bars["ts_utc"] < pd.Timestamp("2026-01-01", tz="UTC")).all()


def test_ultima_barra_do_lote_fica_sempre_de_fora():
    df = ticks(linear(1000, step_s=1))  # ~16.6 min de ticks
    bars = aggregate(df, 300)
    ultimo = df["ts_utc"].iloc[-1]
    assert bars["ts_utc"].iloc[-1] + timedelta(seconds=300) <= ultimo


def test_nao_ha_como_desligar_a_regra_2():
    """A conveniencia de ter a ultima barra e o que torna o lookahead atraente."""
    import inspect

    assinatura = inspect.signature(aggregate)
    proibidos = {"include_forming", "flush", "partial", "allow_incomplete"}
    assert not (proibidos & set(assinatura.parameters))


# --------------------------------------- regra 3: intervalo sem tick nao gera


def test_intervalo_sem_tick_nao_gera_barra():
    """Barra sintetica de range zero envenenaria a baseline por horario."""
    df = ticks([(0, 3300.0), (60, 3301.0), (900, 3302.0), (1200, 3303.0)])
    bars = aggregate(df, 300)
    assert list(bars["ts_utc"]) == [T0, T0 + timedelta(minutes=15)]
    assert (bars["n_ticks"] > 0).all()
    assert RULES.empty == "intervalo sem tick nao gera barra"


def test_buraco_nao_vira_barra_por_preenchimento():
    """Entre 14:05 e 14:30 nao ha tick: nenhuma barra deve aparecer ali."""
    df = ticks([(0, 3300.0), (60, 3301.0), (1800, 3302.0), (2100, 3303.0)])
    bars = aggregate(df, 300)
    rotulos = list(bars["ts_utc"])
    assert rotulos == [T0, T0 + timedelta(minutes=30)]
    for vazio in range(5, 30, 5):
        assert T0 + timedelta(minutes=vazio) not in rotulos


def test_barra_de_tick_unico_tem_range_zero_legitimo():
    """Range zero por um unico tick e real; por preenchimento, e fabricado."""
    df = ticks([(0, 3300.0), (600, 3301.0), (900, 3302.0)])
    bars = aggregate(df, 300)
    assert bars["n_ticks"].iloc[0] == 1
    assert bars["range_usd_oz"].iloc[0] == 0.0


# ----------------------------------------------------------- pausa diaria


def _daily_break_minutes() -> float:
    """Duracao da pausa, lida do manifesto.

    Le da config em vez de fixar: `server.timezone_offset` esta VERIFICAR, e o
    instante da pausa depende dele. A duracao nao depende — e e a duracao que
    este teste precisa. Se o manifesto mudar, o teste acompanha.
    """
    p = repo_root() / "config" / "brokers" / "exness-standard.yaml"
    janela = yaml.safe_load(p.read_text(encoding="utf-8"))["server"]["daily_break"]
    ini, fim = janela.split("-")
    h1, m1 = (int(x) for x in ini.split(":"))
    h2, m2 = (int(x) for x in fim.split(":"))
    return (h2 * 60 + m2) - (h1 * 60 + m1)


def test_pausa_diaria_nao_gera_barras():
    """Sem tick, sem barra. A pausa emerge da regra 3, sem regra propria."""
    pausa_min = _daily_break_minutes()
    assert pausa_min > 0

    tf_s = 300
    antes = linear(30, step_s=10)                        # 5 min de ticks
    inicio_pausa = T0 + timedelta(minutes=5)
    # Reabertura alinhada a grade, para que a comparacao seja sobre buckets
    # inteiros: um bucket que comeca dentro da pausa e termina depois dela
    # contem ticks legitimos e nao deve contar como violacao.
    salto = math.ceil((pausa_min * 60 + 300) / tf_s) * tf_s
    depois = [(salto + i * 10, 3305.0 + i * 0.01) for i in range(60)]
    bars = aggregate(ticks(antes + depois), tf_s)

    reabertura = T0 + timedelta(seconds=salto)
    dentro = bars[(bars["ts_utc"] >= inicio_pausa) & (bars["ts_utc"] < reabertura)]
    assert dentro.empty, f"a pausa produziu {len(dentro)} barra(s)"
    assert (bars["ts_utc"] < inicio_pausa).any(), "faltam as barras de antes"
    assert (bars["ts_utc"] >= reabertura).any(), "faltam as barras de depois"


def test_nenhum_horario_de_pausa_no_codigo_do_agregador():
    """Codificar a pausa congelaria uma suposicao nao medida e quebraria no BTC.

    Olha so o codigo executavel: citar a pausa ao justificar a regra 3 e o que
    a documentacao deve fazer.
    """
    codigo = codigo_executavel(repo_root() / "python" / "riser" / "data" / "bars.py")
    for horario in ("20:58", "22:00", "22:01", "daily_break", "timezone_offset"):
        assert horario not in codigo, f"{horario!r} hardcoded no agregador"


def test_barra_que_atravessa_o_inicio_da_pausa_e_emitida():
    """M30 de 20:30 cobre [20:30, 21:00) mesmo com a sessao parando as 20:58."""
    antes = [(i * 60, 3300.0 + i * 0.1) for i in range(28)]     # 0..27 min
    reabertura = [(int(_daily_break_minutes() * 60) + 1800, 3310.0)]
    bars = aggregate(ticks(antes + reabertura), 1800)
    assert bars["ts_utc"].iloc[0] == T0
    assert bars["n_ticks"].iloc[0] == 28


# ------------------------------------------------------------ OHLC e campos


def test_ohlc_do_bid_por_padrao():
    """A Exness plota em bid; barra em mid nunca casaria com o grafico."""
    df = ticks([(0, 3300.0), (60, 3310.0), (120, 3295.0), (180, 3302.0), (300, 3303.0)])
    b = aggregate(df, 300).iloc[0]
    assert (b["open"], b["high"], b["low"], b["close"]) == (
        3300.0, 3310.0, 3295.0, 3302.0
    )


def test_range_em_usd_por_onca():
    df = ticks([(0, 3300.0), (60, 3310.5), (120, 3295.5), (300, 3303.0)])
    assert aggregate(df, 300)["range_usd_oz"].iloc[0] == pytest.approx(15.0)


def test_colunas_e_ordem():
    bars = aggregate(ticks(linear(400)), 60)
    assert list(bars.columns) == list(empty_bars().columns)


def test_ask_e_opcional_e_explicito():
    df = ticks([(0, 3300.0), (60, 3301.0), (300, 3302.0)], spread=0.50)
    assert aggregate(df, 300, price="ask")["open"].iloc[0] == pytest.approx(3300.50)
    with pytest.raises(ValueError, match="bid.*ask"):
        aggregate(df, 300, price="mid")


# ---------------------------------------------------- consistencia e limites


def test_cada_timeframe_agrega_dos_ticks_nao_do_anterior():
    """Encadear agregacoes propagaria a barra em formacao de um nivel ao seguinte."""
    df = ticks(linear(4000, step_s=1))
    todos = aggregate_all(df)
    assert set(todos) == set(TIMEFRAMES)

    m1, m5 = todos["M1"], todos["M5"]
    primeira = m5.iloc[0]
    dentro = m1[
        (m1["ts_utc"] >= primeira["ts_utc"])
        & (m1["ts_utc"] < primeira["ts_utc"] + timedelta(minutes=5))
    ]
    assert primeira["high"] == dentro["high"].max()
    assert primeira["low"] == dentro["low"].min()
    assert primeira["n_ticks"] == dentro["n_ticks"].sum()


def test_soma_de_ticks_bate_com_a_entrada():
    df = ticks(linear(1000, step_s=1))
    bars = aggregate(df, 60)
    ultimo_fim = bars["ts_utc"].iloc[-1] + timedelta(seconds=60)
    assert bars["n_ticks"].sum() == int((df["ts_utc"] < ultimo_fim).sum())


def test_ticks_vazios_devolvem_frame_vazio_com_schema():
    bars = aggregate(empty_ticks(), 300)
    assert bars.empty
    assert list(bars.columns) == list(empty_bars().columns)


def empty_ticks() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts_utc": pd.Series([], dtype="datetime64[ms, UTC]"),
            "bid": pd.Series([], dtype="float64"),
        }
    )


def test_ticks_fora_de_ordem_sao_recusados():
    """Ordenar aqui esconderia problema a montante."""
    df = ticks([(0, 3300.0), (120, 3301.0), (60, 3302.0), (300, 3303.0)])
    with pytest.raises(ValueError, match="fora de ordem"):
        aggregate(df, 300)


def test_timeframe_desconhecido_e_recusado():
    with pytest.raises(KeyError, match="M7"):
        aggregate_tf(ticks(linear(400)), "M7")


def test_intervalo_invalido_e_recusado():
    with pytest.raises(ValueError, match="positivo"):
        aggregate(ticks(linear(10)), 0)


def test_agregador_nao_depende_de_sensor_nenhum():
    """ADR 0007: infraestrutura que conhece um consumidor esta na camada errada.

    Inspeciona o codigo executavel, nao a fonte crua. O agregador PODE explicar
    nos comentarios por que a regra 3 existe citando o SVC — o que nao pode e
    importar, nomear ou parametrizar algo dele.
    """
    codigo = codigo_executavel(repo_root() / "python" / "riser" / "data" / "bars.py")
    for termo in ("base_rg", "SVC", "svc", "baseline", "sensors", "sensor"):
        assert termo not in codigo, f"{termo!r} acopla o agregador a um sensor"


def test_bars_nao_importa_nada_de_sensores():
    arvore = ast.parse(
        (repo_root() / "python" / "riser" / "data" / "bars.py").read_text(
            encoding="utf-8"
        )
    )
    for node in ast.walk(arvore):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "sensors" not in node.module
        elif isinstance(node, ast.Import):
            for a in node.names:
                assert "sensors" not in a.name


# ------------------------------------------------------------ armazenamento


def test_grava_particionado_e_sem_part(tmp_path):
    bars = aggregate(ticks(linear(2000, step_s=1)), 60)
    p = write_bars(bars, "XAUUSD", "M1", 2026, 7, root=tmp_path)
    assert p == bars_path("XAUUSD", "M1", 2026, 7, tmp_path)
    assert p.relative_to(tmp_path).as_posix() == (
        "XAUUSD/M1/year=2026/month=07/bars.parquet"
    )
    assert list(p.parent.glob("*.part")) == []

    lido = pd.read_parquet(p)
    assert list(lido.columns) == list(empty_bars().columns)
    assert str(lido["ts_utc"].dt.tz) == "UTC"
