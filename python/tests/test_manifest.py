"""Testes do verificador de manifesto de corretora.

O verificador existe para transformar um VERIFICAR em numero conferido. Se ele
deixar passar uma divergencia, produz o pior desfecho possivel: um manifesto que
parece conferido e nao esta, e todo calculo a jusante herda o erro achando que
foi validado.

Por isso os testes sao de caso POSITIVO — cada um planta uma divergencia
conhecida e exige que ela seja acusada. Verde contra um par que ja bate nao
distingue "nao ha divergencia" de "nao consigo achar divergencia" (invariante
10). Fixtures geradas em codigo, pela ADR 0001.
"""

from __future__ import annotations

import copy
import json

import pytest
import yaml

from riser.brokers.manifest import (
    ERRO,
    INFO,
    PENDENTE,
    achar_specs,
    carregar_manifesto,
    carregar_specs,
    verificar,
)

# Par coerente de referencia: manifesto e medicao que batem em tudo. Cada teste
# estraga UM campo a partir daqui, para que a causa do achado seja o campo e
# nao a fixture.
MANIFESTO = {
    "id": "corretora-de-teste",
    "is_demo": False,
    "symbol": {
        "resolve": ["OURO_A", "OURO_B"],
        "contract_oz": 100,
        "digits": 3,
        "volume_min": 0.01,
        "volume_max": 200,
        "volume_step": 0.01,
        "stops_level_points": 0,
    },
    "execution": {"filling": ["FOK", "IOC"], "account_mode": "hedging"},
    "account": {"currency": "JPY", "profit_currency": "USD"},
    "swap": {"buy_points": -523.8, "sell_points": 0.0, "triple_day": "wednesday"},
}

SPECS = {
    "schema": "symbol-specs/1",
    "run_id": "20260808T120000-1",
    "lido_em_utc": "2026.08.08 12:00:00",
    "terminal_build": 4620,
    "symbol": "OURO_A",
    "account": {
        "hash": "abcdef012345",
        "source": "live",
        "company": "Corretora de Teste",
        "server": "Teste-Real",
        "currency": "JPY",
        "margin_mode": "hedging",
        "margin_mode_raw": 2,
    },
    "specs": {
        "digits": 3,
        "point": 0.001,
        "trade_contract_size": 100.0,
        "trade_tick_value": 0.1,
        "trade_tick_value_profit": 0.1,
        "trade_tick_value_loss": 0.1,
        "trade_tick_size": 0.001,
        "trade_stops_level": 0,
        "trade_freeze_level": 0,
        "trade_mode": "full",
        "trade_calc_mode": "cfdleverage",
        "volume_min": 0.01,
        "volume_max": 200.0,
        "volume_step": 0.01,
        "filling_mode": ["FOK", "IOC"],
        "filling_mode_raw": 3,
        "currency_base": "XAU",
        "currency_profit": "USD",
        "currency_margin": "USD",
    },
    "swap": {
        "long": -523.8,
        "short": 0.0,
        "mode": "POINTS",
        "mode_raw": 1,
        "rollover3days": "wednesday",
        "rollover3days_raw": 3,
    },
    "instantaneo": {
        "bid": 3300.0,
        "ask": 3300.24,
        "spread_points": 240,
        "spread_float": True,
        "spread_derivado_por_digits": 0.24,
    },
}


def _man(**mudancas):
    m = copy.deepcopy(MANIFESTO)
    for caminho, valor in mudancas.items():
        alvo = m
        partes = caminho.split("__")
        for p in partes[:-1]:
            alvo = alvo[p]
        alvo[partes[-1]] = valor
    return m


def _spec(**mudancas):
    s = copy.deepcopy(SPECS)
    for caminho, valor in mudancas.items():
        alvo = s
        partes = caminho.split("__")
        for p in partes[:-1]:
            alvo = alvo[p]
        alvo[partes[-1]] = valor
    return s


def _erros(achados):
    return {a.campo for a in achados if a.severidade == ERRO}


def _pendentes(achados):
    return {a.campo for a in achados if a.severidade == PENDENTE}


# ------------------------------------------------------------ caso coerente


def test_par_coerente_nao_gera_erro():
    achados = verificar(MANIFESTO, SPECS)
    assert _erros(achados) == set()
    assert _pendentes(achados) == set()


# --------------------------------------------------------------- o de sempre


def test_digits_divergente_e_erro():
    """O achado que motiva o verificador inteiro.

    Manifesto diz 3, servidor diz 2: toda conversao do projeto sai dez vezes
    errada, e nenhum outro sintoma aparece.
    """
    achados = verificar(MANIFESTO, _spec(specs__digits=2, specs__point=0.01,
                                         specs__trade_tick_size=0.01))
    assert "symbol.digits" in _erros(achados)


def test_point_incoerente_com_digits_e_erro_mesmo_com_manifesto_certo():
    """Coerencia interna da medicao, sem olhar o manifesto.

    Se o servidor devolve digits=3 e point=0.01, o problema nao e o manifesto
    estar errado — e o instrumento nao ser o que se pensava.
    """
    achados = verificar(MANIFESTO, _spec(specs__point=0.01))
    assert "specs.point" in _erros(achados)


# ----------------------------------------------------------------- swap


def test_swap_em_outra_unidade_suspende_a_comparacao_numerica():
    """`buy_points` so significa pontos se o servidor cobrar em pontos.

    Em CURRENCY_DEPOSIT o numero e dinheiro. Comparar os dois daria "confere"
    ou "diverge" por acidente da magnitude.
    """
    achados = verificar(MANIFESTO, _spec(swap__mode="CURRENCY_DEPOSIT", swap__mode_raw=4))
    assert "swap.mode" in _erros(achados)
    # A comparacao numerica NAO acontece: acusa-la aqui seria dizer que os
    # numeros divergem quando o que se sabe e que nao sao comparaveis.
    assert "swap.buy_points" not in _erros(achados)


def test_swap_divergente_e_erro():
    achados = verificar(MANIFESTO, _spec(swap__long=-52.38))
    assert "swap.buy_points" in _erros(achados)


def test_dia_triplo_divergente_e_erro():
    achados = verificar(MANIFESTO, _spec(swap__rollover3days="friday"))
    assert "swap.triple_day" in _erros(achados)


# ------------------------------------------------------------- VERIFICAR


def test_verificar_e_pendente_nao_erro():
    """Campo por preencher nao e divergencia. Pede acao diferente."""
    achados = verificar(_man(symbol__digits="VERIFICAR"), SPECS)
    assert "symbol.digits" in _pendentes(achados)
    assert "symbol.digits" not in _erros(achados)


def test_verificar_em_swap_tambem_e_pendente():
    achados = verificar(_man(swap__buy_points="VERIFICAR"), SPECS)
    assert "swap.buy_points" in _pendentes(achados)
    assert "swap.buy_points" not in _erros(achados)


# ------------------------------------------------------------- simbolo/conta


def test_simbolo_fora_da_lista_resolve_e_erro():
    achados = verificar(MANIFESTO, _spec(symbol="OUTRA_COISA"))
    assert "symbol.resolve" in _erros(achados)


def test_conta_demo_contra_manifesto_real_e_erro():
    """O mesmo terminal serve as duas. Quem decide e o login, nao o terminal."""
    achados = verificar(MANIFESTO, _spec(account__source="demo"))
    assert "is_demo" in _erros(achados)


def test_netting_contra_hedging_e_erro():
    achados = verificar(MANIFESTO, _spec(account__margin_mode="netting"))
    assert "execution.account_mode" in _erros(achados)


# ------------------------------------------------------------------ filling


def test_filling_faltando_no_servidor_e_erro():
    """Prometer um modo que a corretora nao oferece so falha ao enviar ordem."""
    achados = verificar(MANIFESTO, _spec(specs__filling_mode=["FOK"]))
    assert "execution.filling" in _erros(achados)


def test_filling_sobrando_no_servidor_e_informacao():
    """Manifesto incompleto nao e manifesto errado."""
    achados = verificar(MANIFESTO, _spec(specs__filling_mode=["FOK", "IOC", "BOC"]))
    assert "execution.filling" not in _erros(achados)
    assert any(a.campo == "execution.filling" and a.severidade == INFO for a in achados)


# ----------------------------------------------------------------- ausencias


def test_campo_ausente_do_json_e_erro():
    s = copy.deepcopy(SPECS)
    del s["specs"]["digits"]
    assert "symbol.digits" in _erros(verificar(MANIFESTO, s))


def test_schema_desconhecido_recusa(tmp_path):
    p = tmp_path / "x.json"
    p.write_text(json.dumps({"schema": "symbol-specs/99"}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema"):
        carregar_specs(p)


# ------------------------------------------------------------------- caminhos


def test_carrega_manifesto_do_repositorio(tmp_path):
    raiz = tmp_path / "repo"
    (raiz / "config" / "brokers").mkdir(parents=True)
    (raiz / "config" / "brokers" / "x.yaml").write_text(
        yaml.safe_dump(MANIFESTO, allow_unicode=True), encoding="utf-8"
    )
    assert carregar_manifesto("x", root=raiz)["symbol"]["digits"] == 3


def test_duas_contas_com_specs_recusa_escolher(tmp_path):
    """Escolher em silencio compararia o manifesto de uma corretora com a
    medicao de outra."""
    for conta in ("aaa", "bbb"):
        d = tmp_path / "mt5" / "alias" / conta / "symbol-specs"
        d.mkdir(parents=True)
        (d / "OURO_A-latest.json").write_text(json.dumps(SPECS), encoding="utf-8")
    with pytest.raises(ValueError, match="mais de uma conta"):
        achar_specs(root=tmp_path)


def test_sem_specs_no_disco_diz_o_que_fazer(tmp_path):
    with pytest.raises(FileNotFoundError, match="ReadSymbolSpecs"):
        achar_specs(root=tmp_path)
