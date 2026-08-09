"""Testes dos componentes de janela rolante do SVC.

Dois testes carregam o peso do modulo:

    test_vetorizada_bate_com_incremental  — as duas formas nao podem divergir
    test_sem_lookahead                    — invariante 4, provado por truncagem

O resto verifica os casos de fronteira da janela, que erram em silencio: um
tick a mais ou a menos na borda muda `rg` sem levantar nada.

Fixtures gerados em codigo (ADR 0001).
"""

from __future__ import annotations

import numpy as np
import pytest

from riser.sensors.svc import (
    SVCIncremental,
    componentes,
    incremental_sobre,
)

S = 1_000_000_000  # 1 segundo em ns


def serie(segundos, precos):
    return (np.array([int(s * S) for s in segundos], dtype="int64"),
            np.array(precos, dtype="float64"))


def caminhada(n: int, *, semente: int = 7, passo: float = 0.05, hz: float = 5.0):
    """Serie sintetica com espacamento irregular, como tick real."""
    rng = np.random.default_rng(semente)
    dt = rng.exponential(1.0 / hz, n).cumsum()
    px = 4000.0 + np.cumsum(rng.normal(0.0, passo, n))
    return (dt * S).astype("int64"), px


# ------------------------------------------------- as duas formas concordam


@pytest.mark.parametrize("w", [10.0, 20.0, 30.0, 60.0])
def test_vetorizada_bate_com_incremental(w):
    """Duas implementacoes sao aceitaveis so enquanto derem o mesmo numero.

    A incremental e a que vira MQL5; a vetorizada existe porque a Etapa A sao
    20 variantes sobre milhoes de ticks. Se divergirem, ha duas verdades — que
    e o que o invariante 5 existe para impedir.
    """
    ts, px = caminhada(4000)
    v = componentes(ts, px, w)
    i = incremental_sobre(ts, px, w)

    assert np.array_equal(v.tr, i.tr)
    for campo in ("rg", "dsp", "caminho", "er"):
        a, b = getattr(v, campo), getattr(i, campo)
        assert np.allclose(a, b, atol=1e-12), (
            f"{campo}: divergencia maxima {np.max(np.abs(a - b)):.3e}"
        )


def test_paridade_tambem_com_ticks_no_mesmo_instante():
    """Milissegundo repetido acontece em divulgacao e quebra `searchsorted`
    ingenuo."""
    ts, px = serie([0, 0, 0, 1, 1, 2, 3, 3, 3, 4], [10, 11, 12, 11, 10, 13, 12, 12, 14, 13])
    v, i = componentes(ts, px, 2.0), incremental_sobre(ts, px, 2.0)
    assert np.array_equal(v.tr, i.tr)
    assert np.allclose(v.rg, i.rg)
    assert np.allclose(v.er, i.er)


# ------------------------------------------------------------- causalidade


def test_sem_lookahead():
    """Invariante 4, provado por truncagem em vez de por leitura de codigo.

    A saida em T tem de ser identica quando tudo depois de T e removido.
    Repintura nao e detetavel a olho.
    """
    ts, px = caminhada(1500)
    completo = componentes(ts, px, 30.0)
    for corte in (100, 500, 1000, 1499):
        truncado = componentes(ts[: corte + 1], px[: corte + 1], 30.0)
        for campo in ("tr", "rg", "dsp", "er", "caminho"):
            assert getattr(truncado, campo)[corte] == pytest.approx(
                getattr(completo, campo)[corte], abs=1e-12
            ), f"{campo} em T={corte} mudou quando o futuro foi removido"


def test_incremental_nunca_ve_a_serie():
    """A incremental so recebe o tick corrente — lookahead e impossivel nela."""
    ts, px = caminhada(300)
    s = SVCIncremental(20.0)
    saidas = [s.atualizar(int(t), float(p)) for t, p in zip(ts, px)]
    ref = incremental_sobre(ts, px, 20.0)
    assert saidas[-1]["rg"] == pytest.approx(ref.rg[-1])


# --------------------------------------------------------- janela: fronteira


def test_janela_e_aberta_a_esquerda_e_fechada_a_direita():
    """(T-W, T]. O tick exatamente em T-W esta FORA.

    Um a mais ou a menos aqui muda `rg` sem levantar erro nenhum.
    """
    ts, px = serie([0, 10, 20], [100.0, 105.0, 110.0])
    r = componentes(ts, px, 10.0)
    assert r.tr.tolist() == [1, 1, 1], "o tick em T-W nao pode entrar"

    ts2, px2 = serie([0, 9.999, 10], [100.0, 105.0, 110.0])
    assert componentes(ts2, px2, 10.0).tr.tolist() == [1, 2, 2]


def test_tick_unico_tem_range_zero_e_er_zero():
    ts, px = serie([0], [4000.0])
    r = componentes(ts, px, 30.0)
    assert r.tr.tolist() == [1]
    assert r.rg[0] == 0.0 and r.dsp[0] == 0.0 and r.er[0] == 0.0


def test_serie_vazia_nao_levanta():
    r = componentes(np.array([], dtype="int64"), np.array([]), 30.0)
    assert len(r) == 0


# ---------------------------------------------------------------- semantica


def test_rg_e_max_menos_min_em_usd_por_onca():
    ts, px = serie([0, 1, 2, 3], [4000.0, 4010.0, 3995.0, 4002.0])
    r = componentes(ts, px, 30.0)
    assert r.rg[-1] == pytest.approx(15.0)


def test_rg_acompanha_o_maximo_a_sair_da_janela():
    """O pico expira e `rg` tem de cair. E o caso que soma corrente nao cobre."""
    ts, px = serie([0, 1, 2, 12, 13], [100.0, 130.0, 100.0, 101.0, 102.0])
    r = componentes(ts, px, 10.0)
    assert r.rg[1] == pytest.approx(30.0)
    assert r.rg[-1] == pytest.approx(1.0), "o pico de 130 ja saiu da janela"


def test_dsp_e_deslocamento_liquido_com_sinal_absoluto():
    ts, px = serie([0, 1, 2], [4000.0, 4050.0, 3990.0])
    r = componentes(ts, px, 30.0)
    assert r.dsp[-1] == pytest.approx(10.0)


def test_er_separa_direcional_de_serrado():
    """E o componente que distingue 'volatil indo a algum lado' de 'serrando'."""
    ts_d, px_d = serie(range(5), [100.0, 101.0, 102.0, 103.0, 104.0])
    assert componentes(ts_d, px_d, 30.0).er[-1] == pytest.approx(1.0)

    ts_s, px_s = serie(range(5), [100.0, 101.0, 100.0, 101.0, 100.0])
    assert componentes(ts_s, px_s, 30.0).er[-1] == pytest.approx(0.0, abs=1e-12)


def test_er_fica_entre_zero_e_um():
    ts, px = caminhada(2000)
    er = componentes(ts, px, 20.0).er
    assert er.min() >= 0.0 and er.max() <= 1.0 + 1e-12


def test_caminho_ignora_o_salto_de_entrada_na_janela():
    """O delta que liga um tick de FORA ao primeiro de dentro nao e visivel."""
    ts, px = serie([0, 5, 6, 7], [100.0, 200.0, 201.0, 202.0])
    r = componentes(ts, px, 3.0)
    assert r.tr[-1] == 3
    assert r.caminho[-1] == pytest.approx(2.0), "o salto 100->200 esta fora"


# ------------------------------------------------------------------ recusas


def test_ticks_fora_de_ordem_sao_recusados():
    ts, px = serie([0, 2, 1], [100.0, 101.0, 102.0])
    with pytest.raises(ValueError, match="fora de ordem"):
        componentes(ts, px, 10.0)


def test_janela_nao_positiva_e_recusada():
    ts, px = caminhada(10)
    with pytest.raises(ValueError, match="positiva"):
        componentes(ts, px, 0.0)


def test_unidade_nao_muda_com_a_janela():
    """`rg` sai em USD/oz bruto: e o insumo de `base_rg` (ADR 0004)."""
    ts, px = caminhada(500)
    a = componentes(ts, px, 10.0).rg
    b = componentes(ts, px, 60.0).rg
    assert (b >= a - 1e-12).all(), "janela maior nao pode dar range menor"
