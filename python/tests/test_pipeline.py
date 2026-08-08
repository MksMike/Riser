"""Testes da entrada de linha de comando do pipeline.

O que se testa aqui nao e o download nem a agregacao — esses tem os seus. O que
se testa e o que uma corrida de dezessete mil horas exige e um script
descartavel nao tem: retomar pelo disco, publicar progresso legivel, parar sem
deixar trabalho pela metade, e nao encadear etapa nenhuma.

Fixtures geradas em codigo, pela ADR 0001.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from riser.data.bars import TIMEFRAMES, bars_path
from riser.data.pipeline import (
    ETAPAS,
    Progresso,
    analisar_lacunas,
    etapa_aggregate,
    etapa_parse,
    meses_no_intervalo,
    rodar,
)
from riser.data.ticks import parquet_path, write_month


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RISER_DATA_ROOT", str(tmp_path))
    return tmp_path


class LogFalso:
    """Nao escreve nada. Os testes do envelope vivem em test_log.py."""

    def __init__(self):
        self.linhas = []

    def _reg(self, lvl, **f):
        self.linhas.append((lvl, f))

    def info(self, **f):
        self._reg("info", **f)

    def warn(self, **f):
        self._reg("warn", **f)

    def error(self, code, **f):
        self._reg("error", code=code, **f)

    def boot(self, **f):
        self._reg("info", **f)

    def close(self):
        pass

    def envelope(self, lvl="info"):
        return {"ts": "2026-08-08T00:00:00.000Z", "run_id": "r", "build_hash": "b",
                "config_hash": "c", "src": "py", "comp": "teste", "lvl": lvl,
                "account_hash": None, "feed_id": "dukascopy", "broker_id": None}


def _ticks(inicio: str, n: int, passo_s: int = 5) -> pd.DataFrame:
    ts = pd.Timestamp(inicio, tz="UTC") + pd.to_timedelta(np.arange(n) * passo_s, unit="s")
    bid = 3300.0 + np.cumsum(np.full(n, 0.001))
    return pd.DataFrame({
        "ts_utc": ts, "bid": bid, "ask": bid + 0.2,
        "bid_vol": np.ones(n, dtype="float32"), "ask_vol": np.ones(n, dtype="float32"),
    })


# ------------------------------------------------------------------- meses


def test_intervalo_e_inclusivo_nas_duas_pontas():
    assert meses_no_intervalo("2024-08", "2024-10") == [(2024, 8), (2024, 9), (2024, 10)]


def test_intervalo_atravessa_a_virada_de_ano():
    assert meses_no_intervalo("2025-11", "2026-02") == [
        (2025, 11), (2025, 12), (2026, 1), (2026, 2)
    ]


def test_mes_unico():
    assert meses_no_intervalo("2026-07", "2026-07") == [(2026, 7)]


def test_intervalo_invertido_recusa():
    with pytest.raises(SystemExit, match="anterior"):
        meses_no_intervalo("2026-07", "2026-01")


def test_formato_invalido_recusa():
    with pytest.raises(SystemExit, match="AAAA-MM"):
        meses_no_intervalo("julho de 2026", "2026-07")


# ------------------------------------------------------- retomada pelo disco


def test_parse_pula_mes_cujo_parquet_ja_existe(data_root):
    """A retomada olha o ARTEFATO. Sem isto, uma corrida reiniciada refaz tudo."""
    write_month(_ticks("2026-07-01", 100), "XAUUSD", 2026, 7)
    r = etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=False)
    assert r["estado"] == "ja_existe"


def test_force_refaz_mesmo_com_artefato_presente(data_root, monkeypatch):
    write_month(_ticks("2026-07-01", 100), "XAUUSD", 2026, 7)
    chamou = []
    monkeypatch.setattr(
        "riser.data.pipeline.ingest_month",
        lambda *a, **k: (chamou.append(1), (parquet_path("XAUUSD", 2026, 7), 100))[1],
    )
    etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=True)
    assert chamou, "com --force o parse tem de rodar de novo"


def test_apagar_o_artefato_faz_a_retomada_refazer(data_root, monkeypatch):
    """O disco e a verdade: sem o Parquet, o mes volta a ser trabalho pendente.

    E o caso que um arquivo de estado erraria — ele diria 'feito' e o mes
    ficaria faltando em silencio.
    """
    p = write_month(_ticks("2026-07-01", 100), "XAUUSD", 2026, 7)
    assert etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=False)["estado"] == "ja_existe"
    p.unlink()
    chamou = []
    monkeypatch.setattr(
        "riser.data.pipeline.ingest_month",
        lambda *a, **k: (chamou.append(1), (p, 0))[1],
    )
    etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=False)
    assert chamou


def test_aggregate_pula_quando_todos_os_timeframes_existem(data_root):
    write_month(_ticks("2026-07-01", 5000), "XAUUSD", 2026, 7)
    assert etapa_aggregate("XAUUSD", 2026, 7, LogFalso(), force=False)["estado"] == "agregado"
    assert etapa_aggregate("XAUUSD", 2026, 7, LogFalso(), force=False)["estado"] == "ja_existe"


def test_aggregate_refaz_se_faltar_um_unico_timeframe(data_root):
    """Um timeframe ausente e trabalho pendente, nao um mes concluido."""
    write_month(_ticks("2026-07-01", 5000), "XAUUSD", 2026, 7)
    etapa_aggregate("XAUUSD", 2026, 7, LogFalso(), force=False)
    bars_path("XAUUSD", "M15", 2026, 7).unlink()
    assert etapa_aggregate("XAUUSD", 2026, 7, LogFalso(), force=False)["estado"] == "agregado"
    assert all(bars_path("XAUUSD", tf, 2026, 7).exists() for tf in TIMEFRAMES)


# ---------------------------------------------------------------- progresso


def test_progresso_e_json_valido_a_cada_passo(data_root):
    """Legivel de fora ENQUANTO roda: temporario + rename, nunca meio arquivo."""
    p = Progresso("XAUUSD", "parse", 3, root=data_root)
    assert json.loads(p.path.read_text(encoding="utf-8"))["total_meses"] == 3

    p.comecou(2026, 7)
    d = json.loads(p.path.read_text(encoding="utf-8"))
    assert d["mes_atual"] == "2026-07" and d["concluidos"] == 0

    p.terminou(2026, 7, {"estado": "parseado", "ticks": 10})
    d = json.loads(p.path.read_text(encoding="utf-8"))
    assert d["concluidos"] == 1
    assert d["mes_atual"] is None
    assert d["meses"]["2026-07"]["ticks"] == 10

    p.fim(interrompido=False)
    assert json.loads(p.path.read_text(encoding="utf-8"))["encerrado"] is True


def test_progresso_nao_deixa_arquivo_part_para_tras(data_root):
    p = Progresso("XAUUSD", "download", 1, root=data_root)
    p.terminou(2026, 7, {"estado": "ok"})
    assert not list(p.path.parent.glob("*.part"))


def test_progresso_registra_interrupcao(data_root):
    p = Progresso("XAUUSD", "download", 5, root=data_root)
    p.fim(interrompido=True)
    assert json.loads(p.path.read_text(encoding="utf-8"))["interrompido"] is True


# -------------------------------------------------------------- interrupcao


class SinalNoSegundo:
    """Bandeira que sobe depois da primeira consulta.

    `rodar` le `pedida` uma vez por mes, no topo do laco. Assim o primeiro mes
    roda inteiro e o segundo nem comeca — que e exatamente o contrato: termina a
    unidade em curso, nao comeca a proxima.
    """

    def __init__(self):
        self.leituras = 0

    @property
    def pedida(self) -> bool:
        self.leituras += 1
        return self.leituras > 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None


def test_interrupcao_para_entre_meses_sem_comecar_o_seguinte(data_root, monkeypatch):
    vistos = []
    monkeypatch.setitem(
        __import__("riser.data.pipeline", fromlist=["ETAPA_FN"]).ETAPA_FN,
        "parse",
        lambda instr, ano, mes, log, *, force: (vistos.append((ano, mes)), {"estado": "ok"})[1],
    )
    monkeypatch.setattr("riser.data.pipeline.Interrupcao", SinalNoSegundo)

    r = rodar("parse", "XAUUSD", [(2026, 7), (2026, 8)], log=LogFalso())

    assert vistos == [(2026, 7)], "o segundo mes nao devia ter comecado"
    assert r["interrompido"] is True
    assert r["concluidos"] == 1


def test_interrupcao_deixa_o_progresso_marcado(data_root, monkeypatch):
    """Quem ler o arquivo depois de uma queda precisa saber que foi interrompido,
    e nao que terminou."""
    monkeypatch.setitem(
        __import__("riser.data.pipeline", fromlist=["ETAPA_FN"]).ETAPA_FN,
        "parse",
        lambda *a, **k: {"estado": "ok"},
    )
    monkeypatch.setattr("riser.data.pipeline.Interrupcao", SinalNoSegundo)
    rodar("parse", "XAUUSD", [(2026, 7), (2026, 8)], log=LogFalso())

    d = json.loads((data_root / "state" / "XAUUSD" / "parse.json").read_text(encoding="utf-8"))
    assert d["interrompido"] is True
    assert d["encerrado"] is True
    assert d["concluidos"] == 1
    assert "2026-08" not in d["meses"]


def test_mes_que_falha_nao_custa_os_outros(data_root):
    import riser.data.pipeline as pipe

    def fake(instr, ano, mes, log, *, force):
        if mes == 7:
            raise RuntimeError("estourou")
        return {"estado": "ok"}

    original = pipe.ETAPA_FN["parse"]
    pipe.ETAPA_FN["parse"] = fake
    try:
        r = rodar("parse", "XAUUSD", [(2026, 7), (2026, 8)], log=LogFalso())
    finally:
        pipe.ETAPA_FN["parse"] = original

    assert r["concluidos"] == 2
    assert r["erros"] == ["2026-07"]
    assert r["meses"]["2026-08"]["estado"] == "ok"


# ------------------------------------------------------------------ lacunas


def test_lacunas_medem_o_silencio_entre_ticks():
    a = _ticks("2026-07-01 00:00", 60, passo_s=1)
    b = _ticks("2026-07-01 06:00", 60, passo_s=1)
    df = pd.concat([a, b], ignore_index=True)
    r = analisar_lacunas(df)
    assert r["n_ticks"] == 120
    assert r["intervalo_s"]["p50"] == pytest.approx(1.0)
    # Uma unica lacuna, de quase seis horas, acima de todos os limiares.
    assert r["acima_de"]["14400s"] == 1
    assert r["acima_de"]["60s"] == 1
    assert r["maiores"][0]["duracao_h"] == pytest.approx(5.98, abs=0.05)


def test_lacunas_com_frame_curto_nao_estoura():
    assert analisar_lacunas(_ticks("2026-07-01", 1))["sem_dados"] is True


# ------------------------------------------------------------- relatorio


def test_parse_grava_relatorio_com_envelope_de_log(data_root, monkeypatch):
    """Console e efemero. O relatorio tem de sobreviver e se atribuir a algo."""
    p = parquet_path("XAUUSD", 2026, 7)
    monkeypatch.setattr(
        "riser.data.pipeline.ingest_month",
        lambda *a, **k: (write_month(_ticks("2026-07-01", 500), "XAUUSD", 2026, 7), 500),
    )
    r = etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=True)
    assert p.exists()

    doc = json.loads(Path(r["relatorio"]).read_text(encoding="utf-8"))
    for campo in ("run_id", "build_hash", "config_hash", "src", "comp", "feed_id"):
        assert campo in doc, f"envelope sem {campo}"
    assert doc["descritivas"]["ticks"] == 500
    assert "lacunas" in doc["descritivas"]
    assert "spread_usd_oz" in doc["descritivas"]


# ------------------------------------------------------------------ etapas


def test_as_etapas_nao_se_encadeiam(data_root, monkeypatch):
    """Rodar parse nao pode produzir barras.

    Baixar dois anos e so depois decidir o que parsear e um fluxo legitimo, e
    agregacao escondida dentro do parse o quebraria — alem de fazer uma falha de
    agregacao interromper trabalho que ja estava pronto.
    """
    monkeypatch.setattr(
        "riser.data.pipeline.ingest_month",
        lambda *a, **k: (write_month(_ticks("2026-07-01", 2000), "XAUUSD", 2026, 7), 2000),
    )
    etapa_parse("XAUUSD", 2026, 7, LogFalso(), force=True)

    assert parquet_path("XAUUSD", 2026, 7).exists()
    assert not any(bars_path("XAUUSD", tf, 2026, 7).exists() for tf in TIMEFRAMES), (
        "parse produziu barras: as etapas se encadearam"
    )


def test_todas_as_etapas_tem_implementacao():
    """Etapa listada no CLI e nao implementada seria um --help que mente."""
    from riser.data.pipeline import ETAPA_FN

    assert set(ETAPA_FN) == set(ETAPAS)
