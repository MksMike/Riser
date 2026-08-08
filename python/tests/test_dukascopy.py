"""Testes do downloader. Nenhum toca a rede.

O que se testa aqui e a parte que erra em silencio: a conversao de mes com base
zero, a ordem reversa, a fronteira semiaberta e a retomada. Um erro em qualquer
uma delas nao levanta excecao — devolve o dado errado, ou baixa tudo de novo.
"""

from __future__ import annotations

import urllib.error
from datetime import datetime, timezone

import pytest

from riser.data import dukascopy as dk


@pytest.fixture()
def cfg() -> dk.FeedConfig:
    """Le a config real: se ela mudar de forma, o teste tem de perceber."""
    return dk.FeedConfig.load()


# --------------------------------------------------------- mes com base zero


@pytest.mark.parametrize(
    ("mes", "esperado_no_path"),
    [(1, "00"), (2, "01"), (7, "06"), (8, "07"), (12, "11")],
)
def test_url_usa_mes_base_zero(cfg: dk.FeedConfig, mes: int, esperado_no_path: str):
    hora = datetime(2026, mes, 15, 9, tzinfo=timezone.utc)
    url = dk.hour_url(cfg, "XAUUSD", hora)
    assert f"/2026/{esperado_no_path}/15/09h_ticks.bi5" in url


def test_url_completa(cfg: dk.FeedConfig):
    hora = datetime(2026, 7, 4, 23, tzinfo=timezone.utc)
    assert dk.hour_url(cfg, "XAUUSD", hora) == (
        "https://datafeed.dukascopy.com/datafeed/XAUUSD/2026/06/04/23h_ticks.bi5"
    )


def test_caminho_local_usa_mes_base_um(tmp_path):
    """Local e base 1. Se URL e disco usassem a mesma base, a troca passaria."""
    hora = datetime(2026, 7, 4, 23, tzinfo=timezone.utc)
    p = dk.raw_path("XAUUSD", hora, root=tmp_path)
    assert p.relative_to(tmp_path).as_posix() == "XAUUSD/2026/07/04/23h_ticks.bi5"


def test_url_e_disco_discordam_do_mes_de_proposito(cfg: dk.FeedConfig, tmp_path):
    hora = datetime(2026, 3, 1, 0, tzinfo=timezone.utc)
    assert "/2026/02/01/00h" in dk.hour_url(cfg, "XAUUSD", hora)
    assert "2026/03/01" in dk.raw_path("XAUUSD", hora, root=tmp_path).as_posix()


def test_hora_precisa_ser_aware(cfg: dk.FeedConfig):
    with pytest.raises(ValueError, match="timezone-aware"):
        dk.hour_url(cfg, "XAUUSD", datetime(2026, 7, 4, 23))


# ------------------------------------------------------------- ordem reversa


def test_ordem_e_cronologica_reversa():
    start = datetime(2026, 7, 1, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, 5, tzinfo=timezone.utc)
    horas = list(dk.hours_reverse(start, end))
    assert [h.hour for h in horas] == [4, 3, 2, 1, 0]
    assert horas == sorted(horas, reverse=True)


def test_fim_e_exclusivo():
    """A hora corrente ainda esta a ser escrita: baixa-la daria arquivo parcial."""
    start = datetime(2026, 7, 1, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, 3, tzinfo=timezone.utc)
    assert [h.hour for h in dk.hours_reverse(start, end)] == [2, 1, 0]


def test_intervalo_de_uma_hora():
    start = datetime(2026, 7, 1, 0, tzinfo=timezone.utc)
    end = datetime(2026, 7, 1, 1, tzinfo=timezone.utc)
    assert [h.hour for h in dk.hours_reverse(start, end)] == [0]


def test_intervalo_vazio():
    t = datetime(2026, 7, 1, 0, tzinfo=timezone.utc)
    assert list(dk.hours_reverse(t, t)) == []


def test_atravessa_fronteira_de_mes_e_ano():
    start = datetime(2025, 12, 31, 22, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, 2, tzinfo=timezone.utc)
    horas = list(dk.hours_reverse(start, end))
    assert horas[0] == datetime(2026, 1, 1, 1, tzinfo=timezone.utc)
    assert horas[-1] == datetime(2025, 12, 31, 22, tzinfo=timezone.utc)
    assert len(horas) == 4


def test_mes_completo_ignora_o_corrente():
    agora = datetime(2026, 8, 8, 13, 45, tzinfo=timezone.utc)
    start, end = dk.last_complete_month(agora)
    assert start == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 8, 1, tzinfo=timezone.utc)


def test_mes_completo_atravessa_o_ano():
    agora = datetime(2026, 1, 3, 0, tzinfo=timezone.utc)
    start, end = dk.last_complete_month(agora)
    assert start == datetime(2025, 12, 1, tzinfo=timezone.utc)
    assert end == datetime(2026, 1, 1, tzinfo=timezone.utc)


# ----------------------------------------------------------------- retomada


def _limiter() -> dk.RateLimiter:
    return dk.RateLimiter(0.0)


def test_pula_bruto_existente_sem_tocar_a_rede(cfg, tmp_path, monkeypatch):
    hora = datetime(2026, 7, 4, 23, tzinfo=timezone.utc)
    dest = dk.raw_path("XAUUSD", hora, root=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"bruto")

    def explode(*_a, **_k):
        raise AssertionError("retomada nao pode fazer requisicao")

    monkeypatch.setattr(dk, "fetch", explode)
    assert dk.download_hour(cfg, "XAUUSD", hora, _limiter(), root=tmp_path) == "ja_tinha"
    assert dest.read_bytes() == b"bruto"


def test_pula_marcador_de_ausencia(cfg, tmp_path, monkeypatch):
    """Sem o marcador, todo fim de semana e repedido em cada retomada.

    E o desfecho e `ja_ausente`, nao `ja_tinha`: hora que o servidor nega nao e
    hora baixada, e somar as duas produz um contador de progresso que mente.
    """
    hora = datetime(2026, 7, 5, 3, tzinfo=timezone.utc)
    dest = dk.raw_path("XAUUSD", hora, root=tmp_path)
    dest.parent.mkdir(parents=True)
    dest.with_name(dest.name + dk.ABSENT_SUFFIX).write_bytes(b"")

    monkeypatch.setattr(
        dk, "fetch", lambda *a, **k: (_ for _ in ()).throw(AssertionError("nao"))
    )
    assert dk.download_hour(cfg, "XAUUSD", hora, _limiter(), root=tmp_path) == "ja_ausente"


def test_404_cria_marcador_e_nao_cria_bi5(cfg, tmp_path, monkeypatch):
    hora = datetime(2026, 7, 5, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(dk, "fetch", lambda *a, **k: None)

    assert dk.download_hour(cfg, "XAUUSD", hora, _limiter(), root=tmp_path) == "absent"
    dest = dk.raw_path("XAUUSD", hora, root=tmp_path)
    assert not dest.exists()
    assert dest.with_name(dest.name + dk.ABSENT_SUFFIX).exists()


def test_resposta_vazia_grava_bi5_vazio(cfg, tmp_path, monkeypatch):
    """200 com zero bytes e dado — hora sem tick — e nao ausencia."""
    hora = datetime(2026, 7, 5, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(dk, "fetch", lambda *a, **k: b"")

    assert dk.download_hour(cfg, "XAUUSD", hora, _limiter(), root=tmp_path) == "empty"
    dest = dk.raw_path("XAUUSD", hora, root=tmp_path)
    assert dest.exists() and dest.stat().st_size == 0
    assert not dest.with_name(dest.name + dk.ABSENT_SUFFIX).exists()


def test_grava_conteudo_e_nao_deixa_part(cfg, tmp_path, monkeypatch):
    hora = datetime(2026, 7, 4, 23, tzinfo=timezone.utc)
    monkeypatch.setattr(dk, "fetch", lambda *a, **k: b"\x5d\x00payload")

    assert dk.download_hour(cfg, "XAUUSD", hora, _limiter(), root=tmp_path) == "ok"
    dest = dk.raw_path("XAUUSD", hora, root=tmp_path)
    assert dest.read_bytes() == b"\x5d\x00payload"
    assert list(dest.parent.glob("*.part")) == []


def test_escrita_atomica_nao_deixa_arquivo_truncado(tmp_path, monkeypatch):
    """Falha no meio da escrita nao pode produzir bruto que a retomada aceite."""
    dest = tmp_path / "a" / "b.bi5"
    original = dk.Path.replace

    def falha(self, target):  # noqa: ARG001
        raise OSError("disco cheio")

    monkeypatch.setattr(dk.Path, "replace", falha)
    with pytest.raises(OSError):
        dk.write_atomic(dest, b"conteudo")
    assert not dest.exists()
    monkeypatch.setattr(dk.Path, "replace", original)


# ------------------------------------------------------------------- config


def test_config_carrega_e_e_conservadora(cfg: dk.FeedConfig):
    assert cfg.base_url.startswith("https://")
    assert cfg.max_retries >= 1
    assert cfg.backoff_max_s >= cfg.backoff_base_s
    # 4s foi medido, nao escolhido: abaixo disso o servidor devolve 503 e o
    # backoff custa mais tempo do que a pausa teria custado. Ver a config.
    assert cfg.min_interval_s >= 4.0, (
        "intervalo abaixo do medido: o servidor passa a recusar pedidos"
    )


# ---------------------------------------------------------- retry observavel


class _HTTP503(urllib.error.HTTPError):
    def __init__(self) -> None:
        super().__init__("http://x", 503, "Service Unavailable", {}, None)  # type: ignore[arg-type]


def test_retry_e_sempre_logado(cfg, monkeypatch):
    """Retry silencioso torna 503 em serie indistinguivel de rede lenta.

    Foi assim que uma primeira execucao real passou 25 minutos a 24s por
    arquivo sem nada no log que explicasse o motivo.
    """
    registros: list[dict] = []

    class LoggerFalso:
        def warn(self, **f):
            registros.append(f)

    def sempre_503(*_a, **_k):
        raise _HTTP503()

    monkeypatch.setattr(dk.urllib.request, "urlopen", sempre_503)
    monkeypatch.setattr(dk.time, "sleep", lambda _s: None)

    with pytest.raises(dk.DukascopyError):
        dk.fetch(cfg, "http://x", dk.RateLimiter(0.0), logger=LoggerFalso())

    assert len(registros) == cfg.max_retries
    assert all(r["event"] == "retry" for r in registros)
    assert registros[0]["http"] == 503
    assert [r["tentativa"] for r in registros] == list(range(1, cfg.max_retries + 1))
    # Backoff crescente: sem isto o retry seria uma rajada disfarçada de espera.
    esperas = [r["espera_s"] for r in registros]
    assert esperas[-1] > esperas[0]


def test_404_nao_gera_retry_nem_ruido(cfg, monkeypatch):
    registros: list[dict] = []

    class LoggerFalso:
        def warn(self, **f):
            registros.append(f)

    def erro_404(*_a, **_k):
        raise urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)  # type: ignore[arg-type]

    monkeypatch.setattr(dk.urllib.request, "urlopen", erro_404)
    assert dk.fetch(cfg, "http://x", dk.RateLimiter(0.0), logger=LoggerFalso()) is None
    assert registros == [], "404 e ausencia esperada, nao merece aviso"


def test_piso_de_historico_declarado(cfg: dk.FeedConfig):
    """Pedir antes do inicio do historico devolveria 404 em massa."""
    assert "XAUUSD" in cfg.history_starts
    assert cfg.history_starts["XAUUSD"].year >= 2003
