"""Testes da entrada de linha de comando do pipeline.

O que se testa aqui nao e o download nem a agregacao — esses tem os seus. O que
se testa e o que uma corrida de dezessete mil horas exige e um script
descartavel nao tem: retomar pelo disco, publicar progresso legivel, parar sem
deixar trabalho pela metade, e nao encadear etapa nenhuma.

Fixtures geradas em codigo, pela ADR 0001.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
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
    etapa_download,
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


# ------------------------------------------------------------------- trava


def test_trava_impede_segunda_corrida(data_root):
    """Duas corridas coexistiram por horas neste projeto e se sabotaram: a
    Dukascopy limita por conexao concorrente, entao as duas passam a falhar
    mais. Nao e precaucao teorica."""
    from riser.data.pipeline import Trava, TravaOcupada

    with Trava("XAUUSD", "download", root=data_root):
        with pytest.raises(TravaOcupada, match="ja ha uma corrida"):
            with Trava("XAUUSD", "download", root=data_root):
                pass


def test_trava_diz_quem_e_o_dono(data_root):
    import os

    from riser.data.pipeline import Trava, TravaOcupada

    with Trava("XAUUSD", "download", root=data_root):
        with pytest.raises(TravaOcupada, match=str(os.getpid())):
            with Trava("XAUUSD", "download", root=data_root):
                pass


def test_trava_e_liberada_na_saida(data_root):
    from riser.data.pipeline import Trava

    with Trava("XAUUSD", "download", root=data_root) as t:
        assert t.path.exists()
    assert not t.path.exists()


def test_trava_liberada_mesmo_com_excecao(data_root):
    from riser.data.pipeline import Trava

    t = Trava("XAUUSD", "parse", root=data_root)
    with pytest.raises(RuntimeError):
        with t:
            raise RuntimeError("estourou")
    assert not t.path.exists(), "trava vazada bloqueia toda corrida futura"


def test_etapas_diferentes_nao_se_travam(data_root):
    from riser.data.pipeline import Trava

    with Trava("XAUUSD", "download", root=data_root):
        with Trava("XAUUSD", "parse", root=data_root):
            pass


# -------------------------------------------------------------- completude


def _hora(ano, mes, dia, h):
    from datetime import datetime, timezone

    return datetime(ano, mes, dia, h, tzinfo=timezone.utc)


def test_completude_separa_os_quatro_estados(data_root, monkeypatch):
    """presente, vazio, ausente e falhou. So o ultimo pede acao."""
    from riser.data.dukascopy import ABSENT_SUFFIX, raw_path
    from riser.data.pipeline import completude

    raiz = data_root / "raw" / "dukascopy"
    monkeypatch.setattr(
        "riser.data.pipeline.raw_path",
        lambda i, h, root=None: raw_path(i, h, raiz),
    )

    p = raw_path("XAUUSD", _hora(2026, 7, 1, 0), raiz)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"dado")                                  # presente
    raw_path("XAUUSD", _hora(2026, 7, 1, 1), raiz).write_bytes(b"")   # vazia
    m = raw_path("XAUUSD", _hora(2026, 7, 1, 2), raiz)
    m.with_name(m.name + ABSENT_SUFFIX).write_bytes(b"")    # ausente
    # a hora 3 fica sem nada -> falhou

    r = completude("XAUUSD", 2026, 7)
    assert r["total"]["presente"] == 1
    assert r["total"]["vazio"] == 1
    assert r["total"]["ausente"] == 1
    assert r["total"]["falhou"] == 744 - 3
    assert r["completo"] is False
    assert r["por_dia"]["2026-07-01"]["presente"] == 1


def test_mes_so_com_ausencia_e_vazia_conta_como_completo(data_root, monkeypatch):
    """Fim de semana nunca tem dado. Somar 'vazio' e 'ausente' em 'faltando'
    faria a corrida de dois anos nunca convergir."""
    from riser.data.dukascopy import ABSENT_SUFFIX, raw_path
    from riser.data.pipeline import completude

    raiz = data_root / "raw" / "dukascopy"
    monkeypatch.setattr(
        "riser.data.pipeline.raw_path",
        lambda i, h, root=None: raw_path(i, h, raiz),
    )
    from datetime import datetime, timedelta, timezone

    h = datetime(2026, 2, 1, tzinfo=timezone.utc)
    while h < datetime(2026, 3, 1, tzinfo=timezone.utc):
        p = raw_path("XAUUSD", h, raiz)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.with_name(p.name + ABSENT_SUFFIX).write_bytes(b"")
        h += timedelta(hours=1)

    r = completude("XAUUSD", 2026, 2)
    assert r["total"]["falhou"] == 0
    assert r["completo"] is True
    assert r["arquivos_resolvidos"] == r["arquivos_horarios_no_mes"]


# ------------------------------------------------------- laco de convergencia


def _preencher(raiz, ano, mes, quantas, *, desde=0):
    """Materializa `quantas` horas do mes, a partir da hora `desde`."""
    from datetime import datetime, timedelta, timezone

    from riser.data.dukascopy import raw_path as rp

    h = datetime(ano, mes, 1, tzinfo=timezone.utc) + timedelta(hours=desde)
    for _ in range(quantas):
        p = rp("XAUUSD", h, raiz)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
        h += timedelta(hours=1)


@pytest.fixture()
def raiz_bruta(data_root, monkeypatch):
    from riser.data.dukascopy import raw_path as rp

    raiz = data_root / "raw" / "dukascopy"
    monkeypatch.setattr(
        "riser.data.pipeline.raw_path", lambda i, h, root=None: rp(i, h, raiz)
    )
    return raiz


def test_convergiu_quando_nao_sobra_hora_falhando(raiz_bruta, monkeypatch):
    passadas = []

    def fake(instr, ano, mes, **k):
        passadas.append(1)
        _preencher(raiz_bruta, ano, mes, 672)  # fevereiro inteiro
        return {"ok": 672, "empty": 0, "absent": 0, "ja_tinha": 0,
                "ja_ausente": 0, "erro": 0}

    monkeypatch.setattr("riser.data.pipeline.download_month", fake)
    r = etapa_download("XAUUSD", 2026, 2, LogFalso(), force=False)

    assert r["estado"] == "convergiu"
    assert r["completo"] is True
    assert len(passadas) == 1, "convergiu na primeira; nao devia haver segunda"


def test_estagnou_para_cedo_em_vez_de_insistir(raiz_bruta, monkeypatch):
    """Passada que nao resolve NADA e ainda tem falha: o problema esta na rede,
    nao na quantidade de tentativas. Insistir so repete o mesmo erro."""
    passadas = []

    def fake(instr, ano, mes, **k):
        passadas.append(1)
        return {"ok": 0, "empty": 0, "absent": 0, "ja_tinha": 0,
                "ja_ausente": 0, "erro": 672}

    monkeypatch.setattr("riser.data.pipeline.download_month", fake)
    r = etapa_download("XAUUSD", 2026, 2, LogFalso(), force=False)

    assert r["estado"] == "estagnou"
    assert r["completo"] is False
    assert len(passadas) == 1, "estagnou: nao devia gastar as passadas restantes"


def test_teto_encerra_progresso_lento_e_reporta_incompleto(raiz_bruta, monkeypatch):
    """Laco sem teto e um jeito elegante de nunca terminar."""
    from riser.data.pipeline import MAX_PASSADAS

    passadas = []

    def fake(instr, ano, mes, **k):
        # Resolve um punhado por passada: sempre progride, nunca fecha.
        _preencher(raiz_bruta, ano, mes, 10, desde=len(passadas) * 10)
        passadas.append(1)
        return {"ok": 10, "empty": 0, "absent": 0, "ja_tinha": 0,
                "ja_ausente": 0, "erro": 662}

    monkeypatch.setattr("riser.data.pipeline.download_month", fake)
    r = etapa_download("XAUUSD", 2026, 2, LogFalso(), force=False)

    assert r["estado"] == "teto"
    assert r["completo"] is False
    assert len(passadas) == MAX_PASSADAS


def test_download_grava_relatorio_de_completude(raiz_bruta, monkeypatch):
    """Nos 2 anos, 'quais meses estao incompletos' nao pode exigir reprocessar."""
    monkeypatch.setattr(
        "riser.data.pipeline.download_month",
        lambda instr, ano, mes, **k: (
            _preencher(raiz_bruta, ano, mes, 672),
            {"ok": 672, "empty": 0, "absent": 0, "ja_tinha": 0, "ja_ausente": 0, "erro": 0},
        )[1],
    )
    r = etapa_download("XAUUSD", 2026, 2, LogFalso(), force=False)

    doc = json.loads(Path(r["relatorio"]).read_text(encoding="utf-8"))
    assert doc["run_id"] and doc["build_hash"], "relatorio sem envelope"
    assert doc["motivo_parada"] == "convergiu"
    assert doc["total"]["presente"] == 672
    assert doc["por_dia"]["2026-02-01"]["presente"] == 24


# ------------------------------------------------------------- trava orfa


def test_pid_morto_e_detectado_sem_matar_ninguem():
    """`os.kill(pid, 0)` e o idioma de POSIX; no Windows a mesma chamada pode
    TERMINAR o processo. A verificacao aqui nao toca no processo."""
    import subprocess
    import sys as _sys
    import time as _time

    from riser.data.pipeline import _processo_vivo

    cobaia = subprocess.Popen([_sys.executable, "-c", "import time; time.sleep(20)"])
    _time.sleep(0.8)
    try:
        assert _processo_vivo(cobaia.pid) is True
        assert cobaia.poll() is None, "a verificacao matou a cobaia"
    finally:
        cobaia.kill()
        cobaia.wait()
    _time.sleep(0.3)
    assert _processo_vivo(cobaia.pid) is False
    assert _processo_vivo(999999) is False
    assert _processo_vivo(os.getpid()) is True


def test_trava_de_processo_morto_e_assumida(data_root, capsys):
    """Corte de energia no meio da corrida de 2 anos nao pode bloquear a
    retomada. PID morto e fato."""
    from riser.data.pipeline import Trava

    t = Trava("XAUUSD", "download", root=data_root)
    t.path.write_text(json.dumps({
        "pid": 999999, "etapa": "download",
        "inicio_utc": datetime.now(timezone.utc).isoformat(),
    }), encoding="utf-8")

    with Trava("XAUUSD", "download", root=data_root) as nova:
        assert nova.orfa_assumida is not None
        assert "999999" in nova.orfa_assumida
        dono = json.loads(nova.path.read_text(encoding="utf-8"))
        assert dono["pid"] == os.getpid()
    assert "orfa assumida" in capsys.readouterr().out


def test_trava_de_processo_vivo_continua_recusando(data_root):
    """A sobrevivencia a processo morto nao pode virar porta para corrida dupla."""
    from riser.data.pipeline import Trava, TravaOcupada

    with Trava("XAUUSD", "download", root=data_root):
        with pytest.raises(TravaOcupada, match="VIVA"):
            with Trava("XAUUSD", "download", root=data_root):
                pass


def test_pid_reaproveitado_apos_boot_e_orfao(data_root):
    """PID vivo pode ser OUTRO processo que herdou o numero depois do reinicio.
    Um processo nao pode ter comecado antes da maquina ligar - isso e fato, nao
    heuristica de idade."""
    from riser.data.pipeline import Trava, _boot_utc

    if _boot_utc() is None:
        pytest.skip("boot desconhecido nesta plataforma")

    t = Trava("XAUUSD", "download", root=data_root)
    t.path.write_text(json.dumps({
        "pid": os.getpid(),                       # vivo, mas...
        "etapa": "download",
        "inicio_utc": "2001-01-01T00:00:00+00:00",  # ...de antes de qualquer boot
    }), encoding="utf-8")

    with Trava("XAUUSD", "download", root=data_root) as nova:
        assert nova.orfa_assumida is not None
        assert "boot" in nova.orfa_assumida


def test_trava_sem_pid_e_orfa(data_root):
    from riser.data.pipeline import Trava

    t = Trava("XAUUSD", "parse", root=data_root)
    t.path.write_text("{}", encoding="utf-8")
    with Trava("XAUUSD", "parse", root=data_root) as nova:
        assert nova.orfa_assumida is not None


# ------------------------------------------------------------- vocabulario


def test_relatorio_nomeia_a_unidade_sem_ambiguidade():
    """Num relatorio que tambem fala de tempo de execucao, "faltam 385 horas" se
    le como duracao. A unidade de particionamento e ARQUIVO HORARIO; "hora" fica
    reservada para duracao."""
    from riser.data.pipeline import completude

    r = completude("XAUUSD", 2026, 2)
    assert r["unidade"] == "arquivo_horario"
    assert "arquivos_horarios_no_mes" in r
    assert "arquivos_resolvidos" in r
    assert "arquivos_faltando_total" in r
    proibidas = {"horas_no_mes", "resolvidas", "horas_faltando", "horas_faltando_total"}
    assert not (proibidas & set(r)), "chave com vocabulario ambiguo voltou"
    assert set(r["total"]) == {"presente", "vazio", "ausente", "falhou"}


def test_formatar_duracao_cobre_as_escalas_da_corrida():
    from riser.data.dukascopy import formatar_duracao as f

    assert f(45) == "45s"
    assert f(0) == "0s"
    assert f(26 * 60) == "26min"
    assert f(2 * 3600 + 10 * 60) == "2h10"
    assert f(3 * 86400 + 4 * 3600) == "3d04h"
    assert f(-5) == "0s", "duracao negativa nao existe"


def test_estimativa_usa_o_intervalo_configurado():
    """385 arquivos x 4s = 1540s ~ 26min. E o exemplo que motivou a estimativa."""
    from riser.data.dukascopy import estimativa_restante

    assert estimativa_restante(385, 4.0) == "26min"
    assert estimativa_restante(0, 4.0) == "0s"
    # Dois anos de arquivos horarios, no piso do intervalo configurado.
    assert estimativa_restante(17544, 4.0) == "19h30"
