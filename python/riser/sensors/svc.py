"""Sensor de Volatilidade Curta — componentes de janela rolante.

Especificacao: `docs/sensors/sensor-volatilidade-curta-v1.md`.

Este modulo implementa a parte que a Etapa A do protocolo precisa: os
componentes micro sobre janela deslizante de W segundos. Normalizacao por
horario e composicao micro x macro entram depois, quando a Etapa A tiver
escolhido W e a composicao.

--------------------------------------------------------------- duas formas

O documento exige atualizacao incremental tick a tick, com teto de 200 us. Isso
descreve o que vai correr no MT5. Mas a Etapa A sao 20 variantes sobre milhoes
de ticks, e tick a tick em Python levaria horas por variante.

Ha portanto duas implementacoes, e isso so e aceitavel sob uma condicao:

    incremental   `SVCIncremental`  — a referencia. E esta que vira MQL5.
    vetorizada    `componentes()`   — pesquisa. Milhoes de ticks em segundos.

`test_svc.py` prova que as duas dao o MESMO valor sobre a mesma amostra. Sem
esse teste seriam duas verdades, que e precisamente o que o invariante 5 existe
para impedir. Se divergirem, a incremental manda.

------------------------------------------------------------------- unidades

USD por onca em tudo. Nao ha ponto nem lote aqui, e nao pode haver.

`rg` e `dsp` saem em USD/onca BRUTOS, antes de qualquer normalizacao — e a
mediana de `rg` bruto por minuto-do-dia e o `base_rg` de que o Motor de
Trailing depende (ADR 0004). Normalizar antes de guardar destruiria a unidade,
que foi o erro corrigido durante a redacao daquela ADR.

---------------------------------------------------------------- causalidade

A janela em T cobre `(T - W, T]`. Fechada a direita, aberta a esquerda: o tick
do instante T entra, o de T-W nao. Nenhum componente le um unico tick posterior
a T — o invariante 4 nao admite excecao, e `test_svc.py` prova isso truncando a
serie e verificando saida identica.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np
import pandas as pd

# Componentes que este modulo produz. `spr` e `atr` entram com a composicao.
COMPONENTES = ("tr", "rg", "dsp", "er")


@dataclass(frozen=True)
class Janela:
    """Resultado bruto da janela rolante, um valor por tick.

    Todos em USD por onca, menos `tr` que e contagem e `er` que e razao.
    """

    ts_ns: np.ndarray      # instante de cada leitura
    tr: np.ndarray         # ticks na janela
    rg: np.ndarray         # max - min, USD/oz
    dsp: np.ndarray        # |ultimo - primeiro|, USD/oz
    er: np.ndarray         # dsp / soma|delta|, 0..1
    caminho: np.ndarray    # soma|delta| na janela, USD/oz

    def __len__(self) -> int:
        return len(self.ts_ns)

    def frame(self) -> pd.DataFrame:
        return pd.DataFrame({
            "ts_utc": pd.to_datetime(self.ts_ns, utc=True),
            "tr": self.tr, "rg": self.rg, "dsp": self.dsp,
            "er": self.er, "caminho": self.caminho,
        })


def _validar(ts_ns: np.ndarray, preco: np.ndarray, w_s: float) -> None:
    if w_s <= 0:
        raise ValueError(f"janela precisa ser positiva: {w_s}")
    if len(ts_ns) != len(preco):
        raise ValueError(f"ts e preco com tamanhos diferentes: {len(ts_ns)} vs {len(preco)}")
    if len(ts_ns) and not np.all(np.diff(ts_ns) >= 0):
        raise ValueError(
            "ticks fora de ordem cronologica. Ordenar aqui esconderia um "
            "problema a montante e produziria uma janela que mistura futuro."
        )


def componentes(ts_ns: np.ndarray, preco: np.ndarray, w_s: float) -> Janela:
    """Componentes micro sobre janela deslizante de `w_s` segundos. VETORIZADO.

    Para cada tick i, a janela cobre `(t_i - W, t_i]`.

    `rg` usa maximo e minimo da janela, e isso nao tem forma de soma corrente —
    e calculado com um deque monotonico em O(n) amortizado, nao com max() sobre
    a fatia, que seria O(n*W) e inviavel em milhoes de ticks.
    """
    ts_ns = np.asarray(ts_ns, dtype="int64")
    preco = np.asarray(preco, dtype="float64")
    _validar(ts_ns, preco, w_s)
    n = len(ts_ns)
    if n == 0:
        z = np.zeros(0)
        return Janela(ts_ns, z.astype("int64"), z, z, z, z)

    w_ns = int(round(w_s * 1e9))
    # Primeiro indice DENTRO da janela: aberta a esquerda, entao o limiar e
    # exclusivo e usa 'right'.
    ini = np.searchsorted(ts_ns, ts_ns - w_ns, side="right")

    idx = np.arange(n)
    tr = (idx - ini + 1).astype("int64")

    # dsp e caminho saem de somas cumulativas: O(n).
    delta = np.empty(n, dtype="float64")
    delta[0] = 0.0
    np.abs(np.diff(preco), out=delta[1:])
    cam_cum = np.concatenate(([0.0], np.cumsum(delta)))
    # Caminho dentro da janela: exclui o salto de entrada em `ini`, que ligou o
    # tick anterior — fora da janela — ao primeiro de dentro. Inclui-lo faria o
    # `er` depender de um tick que a janela nao ve.
    caminho = cam_cum[idx + 1] - cam_cum[ini + 1]
    dsp = np.abs(preco - preco[ini])

    er = np.zeros(n, dtype="float64")
    vivo = caminho > 0
    er[vivo] = dsp[vivo] / caminho[vivo]

    rg = _range_movel(preco, ini)
    return Janela(ts_ns, tr, rg, dsp, er, caminho)


def _range_movel(preco: np.ndarray, ini: np.ndarray) -> np.ndarray:
    """max - min de cada janela, com deques monotonicos. O(n) amortizado.

    O maximo de uma janela deslizante nao se atualiza por soma e subtracao como
    uma media: quando o maximo sai pela esquerda, e preciso saber qual e o
    segundo maior. O deque monotonico guarda exatamente os candidatos que ainda
    podem vir a ser maximo, e nada mais.
    """
    n = len(preco)
    rg = np.empty(n, dtype="float64")
    altos: deque[int] = deque()
    baixos: deque[int] = deque()

    for i in range(n):
        p = preco[i]
        while altos and preco[altos[-1]] <= p:
            altos.pop()
        altos.append(i)
        while baixos and preco[baixos[-1]] >= p:
            baixos.pop()
        baixos.append(i)

        limite = ini[i]
        while altos[0] < limite:
            altos.popleft()
        while baixos[0] < limite:
            baixos.popleft()

        rg[i] = preco[altos[0]] - preco[baixos[0]]
    return rg


class SVCIncremental:
    """Referencia incremental, um tick por vez. E esta que vira MQL5.

    Mantem os mesmos deques monotonicos e as mesmas somas correntes, sem
    lookahead possivel por construcao: o metodo so recebe o tick corrente e
    nunca ve a serie inteira.

    Nao e a via de pesquisa — 20 variantes sobre milhoes de ticks nao cabem
    aqui. Existe para ser a fonte de verdade que a vetorizada tem de reproduzir.
    """

    def __init__(self, w_s: float) -> None:
        if w_s <= 0:
            raise ValueError(f"janela precisa ser positiva: {w_s}")
        self.w_ns = int(round(w_s * 1e9))
        self._ts: deque[int] = deque()
        self._px: deque[float] = deque()
        self._altos: deque[tuple[int, float]] = deque()
        self._baixos: deque[tuple[int, float]] = deque()
        self._caminho = 0.0
        self._deltas: deque[float] = deque()
        self._ultimo_ts: int | None = None
        self._ultimo_px: float | None = None

    def atualizar(self, ts_ns: int, preco: float) -> dict[str, float]:
        if self._ultimo_ts is not None and ts_ns < self._ultimo_ts:
            raise ValueError("tick fora de ordem cronologica")

        # Delta que liga o tick anterior a este. Entra no caminho junto com o
        # tick, e sai junto com ele.
        d = 0.0 if self._ultimo_px is None else abs(preco - self._ultimo_px)
        self._ts.append(ts_ns)
        self._px.append(preco)
        self._deltas.append(d)
        self._caminho += d

        while self._altos and self._altos[-1][1] <= preco:
            self._altos.pop()
        self._altos.append((ts_ns, preco))
        while self._baixos and self._baixos[-1][1] >= preco:
            self._baixos.pop()
        self._baixos.append((ts_ns, preco))

        # Expira o que saiu da janela. Aberta a esquerda: t == ts - W sai.
        corte = ts_ns - self.w_ns
        while self._ts and self._ts[0] <= corte:
            self._ts.popleft()
            self._px.popleft()
            self._caminho -= self._deltas.popleft()
        while self._altos and self._altos[0][0] <= corte:
            self._altos.popleft()
        while self._baixos and self._baixos[0][0] <= corte:
            self._baixos.popleft()

        # O primeiro delta da janela ligou um tick que ja saiu: nao pertence ao
        # caminho visivel. Descontado aqui, nao na expiracao, porque so agora se
        # sabe qual delta ficou na frente.
        caminho = self._caminho - (self._deltas[0] if self._deltas else 0.0)
        caminho = max(caminho, 0.0)

        dsp = abs(preco - self._px[0])
        self._ultimo_ts, self._ultimo_px = ts_ns, preco
        return {
            "tr": float(len(self._ts)),
            "rg": self._altos[0][1] - self._baixos[0][1],
            "dsp": dsp,
            "er": (dsp / caminho) if caminho > 0 else 0.0,
            "caminho": caminho,
        }


def incremental_sobre(ts_ns: np.ndarray, preco: np.ndarray, w_s: float) -> Janela:
    """Roda `SVCIncremental` sobre a serie inteira. So para o teste de paridade."""
    ts_ns = np.asarray(ts_ns, dtype="int64")
    preco = np.asarray(preco, dtype="float64")
    _validar(ts_ns, preco, w_s)
    s = SVCIncremental(w_s)
    n = len(ts_ns)
    out = {k: np.empty(n, dtype="float64") for k in ("tr", "rg", "dsp", "er", "caminho")}
    for i in range(n):
        r = s.atualizar(int(ts_ns[i]), float(preco[i]))
        for k, v in out.items():
            v[i] = r[k]
    return Janela(ts_ns, out["tr"].astype("int64"), out["rg"],
                  out["dsp"], out["er"], out["caminho"])
