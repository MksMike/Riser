"""Baseline aleatorio casado: as entradas reais contra sinteticas equivalentes.

EXPLORACAO. Nao ha criterio de aceitacao aqui, e nao deve haver: o objetivo e
descobrir se existe edge de TIMING nas entradas manuais, nao aprovar nada.

-------------------------------------------------------------------- a pergunta

Sob "segurar ate virar positivo" a taxa de acerto e ~100% por construcao. O
resultado em dinheiro nao distingue entrada boa de entrada qualquer, porque
quase toda entrada acaba fechando no verde se houver capital e paciencia
bastante. O que distingue e o PRECO da espera:

    MAE               quanto o mercado andou contra, em USD por onca
    tempo-ate-verde   quanto tempo levou para virar
    nao virou         fracao que nao virou dentro de 24h, 72h, 1 semana

Se as entradas reais tiverem MAE menor e tempo-ate-verde mais curto que
sinteticas comparaveis, ha edge de timing. Se forem indistinguiveis, o
resultado vem de capital e paciencia — e isso o EA nao escala.

Nenhuma dessas tres metricas depende de stop, de lote ou de saldo. E por isso
que elas existem no historico que ja esta gravado: nao e preciso ter operado
com stop para medi-las depois.

-------------------------------------------------- o casamento e parametro livre

Como as sinteticas sao sorteadas determina o resultado. Sortear no dia inteiro
compara a entrada dele com a media do dia — inclusive as horas mortas em que
ele nunca opera — e nesse desenho qualquer trader pareceria bom. Escolher UM
casamento seria escolher a resposta.

Por isso os tres rodam sempre, lado a lado, e nenhum e eleito:

    aleatorio       qualquer instante com cobertura suficiente
    horario         mesmo dia da semana e mesma hora UTC
    volatilidade    o do horario, mais o mesmo decil de range PRE-entrada

Divergencia entre os tres E o resultado, nao ruido a resolver. Se o edge some
ao casar por horario, o edge era horario. Se some ao casar por volatilidade, o
edge era saber escolher o regime — o que continua sendo edge, mas de outro
tipo, e mora em outro sensor.

------------------------------------------------------------------- causalidade

O range pre-entrada usado no casamento por volatilidade termina na barra
ANTERIOR a da entrada. Nao e detalhe de implementacao: casar por uma janela que
inclui o minuto da entrada usaria o proprio movimento que se quer explicar, e o
resultado sairia plausivel e errado. E o invariante 4 valendo tambem no
laboratorio.

------------------------------------------------------------------------ swap

O custo de carregar posicao entra aqui porque, no estilo que se esta medindo,
ele domina. Uma unica noite comprada custa mais que o spread pago na entrada.
"Tempo-ate-verde" bruto e "tempo-ate-verde liquido de swap" sao numeros
diferentes, e a diferenca cresce com exatamente o que este estudo mede.

O ponto morre em `CustoNoturno.from_manifest`, que e a borda de execucao deste
modulo. Daqui para dentro so existe USD por onca.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# lab/ nao e pacote instalado. O repositorio e a raiz de import.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))

from riser.core.paths import data_root, repo_root  # noqa: E402
from riser.data.bars import aggregate  # noqa: E402
from riser.data.ticks import read_month  # noqa: E402

HORIZONTES_H: tuple[float, ...] = (24.0, 72.0, 168.0)

# Um minuto. A barra M1 da o MAE EXATO — o minimo de um minuto e o minimo, nao
# uma aproximacao dele. So o instante de virada perde resolucao, e um estudo
# cujos horizontes sao 24h e 1 semana nao tem uso para o segundo exato.
GRADE_S = 60

_DIAS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _ns(idx: pd.DatetimeIndex) -> np.ndarray:
    """Eixo de tempo em nanossegundos inteiros, sempre.

    `.asi8` devolve o inteiro na unidade do proprio indice, e o pandas 2 tem
    datetime de milissegundo tanto quanto de nanossegundo — o `empty_frame` dos
    ticks declara `datetime64[ms]` e o frame parseado sai em ns. Misturar os
    dois nao levanta erro: produz horizontes um milhao de vezes menores, e o
    estudo inteiro responde sobre uma janela que nao existe.
    """
    return idx.tz_convert("UTC").as_unit("ns").asi8


# ------------------------------------------------------------------- entradas


@dataclass(frozen=True)
class Entrada:
    """Uma abertura de posicao. Lote nao aparece: MAE em USD por onca nao depende dele."""

    ts_utc: pd.Timestamp
    side: str

    @property
    def sinal(self) -> int:
        return 1 if self.side == "buy" else -1


GAPS_DIAGNOSTICO_MIN: tuple[int, ...] = (5, 15, 60, 240)


def agrupar_episodios(entradas: list[Entrada], gap_min: float) -> np.ndarray:
    """Junta entradas do mesmo episodio. Devolve o id do episodio de cada uma.

    Empilhar em posicao aberta produz varias entradas que NAO sao observacoes
    independentes: sao pedacos de uma decisao so, tomada uma vez, contra o mesmo
    movimento. Trata-las como independentes infla o N e estreita o intervalo de
    confianca — o estudo pareceria mais conclusivo quanto mais ele empilhasse,
    que e exatamente ao contrario.

    Mesmo episodio quando a direcao e a mesma E o intervalo desde a entrada
    anterior nao passa de `gap_min`. Direcao oposta abre episodio novo mesmo
    colada no tempo: inverter nao e empilhar, e outra decisao.

    O `gap_min` certo nao se sabe de antemao — depende de como ele opera, e
    ninguem precisa lembrar. Por isso o relatorio traz a contagem de episodios
    em varios gaps: a diferenca entre N nominal e N de episodios mostra se ha
    empilhamento, e como ela varia com o gap mostra em que escala de tempo.
    """
    if not entradas:
        return np.zeros(0, dtype="int64")

    limite = pd.Timedelta(minutes=gap_min)
    ids = np.zeros(len(entradas), dtype="int64")
    atual = 0
    for i in range(1, len(entradas)):
        anterior, agora = entradas[i - 1], entradas[i]
        mesmo = agora.side == anterior.side and (agora.ts_utc - anterior.ts_utc) <= limite
        if not mesmo:
            atual += 1
        ids[i] = atual
    return ids


def ler_entradas(path: Path) -> list[Entrada]:
    """Le o CSV exportado do historico de deals.

    Formato minimo, duas colunas obrigatorias:

        ts_utc,side
        2026-03-04T12:31:07Z,buy

    Colunas a mais sao ignoradas de proposito — o export do MT5 traz lote,
    comissao e swap realizado, e nada disso entra no calculo.

    O arquivo vive em RISER-data, nunca no repositorio (invariante 7).
    """
    df = pd.read_csv(path, encoding="utf-8")
    faltando = {"ts_utc", "side"} - set(df.columns)
    if faltando:
        raise ValueError(
            f"{path.name}: colunas obrigatorias ausentes: {sorted(faltando)}. "
            "Adivinhar o nome da coluna produziria um estudo sobre a coluna errada."
        )

    ts = pd.to_datetime(df["ts_utc"], utc=True, errors="raise")
    side = df["side"].astype(str).str.strip().str.lower()

    invalidos = sorted(set(side) - {"buy", "sell"})
    if invalidos:
        raise ValueError(
            f"{path.name}: valores de 'side' nao reconhecidos: {invalidos}. "
            "Esperado 'buy' ou 'sell'."
        )

    pares = sorted(zip(ts, side), key=lambda p: p[0])
    return [Entrada(ts_utc=t, side=s) for t, s in pares]


# ------------------------------------------------------------ custo de carrego


@dataclass(frozen=True)
class CustoNoturno:
    """Swap em USD por onca, por rollover. Positivo = custo.

    Esta classe e a borda de execucao deste modulo: e o unico lugar em que
    ponto existe. A conversao depende de `symbol.digits` do manifesto, que
    ainda esta sujeito a VERIFICAR contra o servidor — com 2 digitos em vez de
    3 todo numero de swap deste estudo sai 10x errado, e sai plausivel.
    """

    buy_usd_oz: float
    sell_usd_oz: float
    triple_weekday: int | None
    rollover_hour_utc: int
    broker_id: str
    digits: int

    @classmethod
    def from_manifest(
        cls, broker_id: str, rollover_hour_utc: int, *, root: Path | None = None
    ) -> "CustoNoturno":
        p = (root or repo_root()) / "config" / "brokers" / f"{broker_id}.yaml"
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))

        digits = raw["symbol"]["digits"]
        if not isinstance(digits, int):
            raise ValueError(
                f"{p.name}: symbol.digits e {digits!r}, nao um inteiro. "
                "Sem digits nao ha conversao de ponto para USD por onca."
            )
        escala = 10.0 ** (-digits)

        swap = raw.get("swap")
        if not swap:
            raise ValueError(
                f"{p.name}: bloco 'swap' ausente. Avaliar retencao longa sem "
                "custo de financiamento mede so a metade barata do custo."
            )

        triplo = swap.get("triple_day")
        if isinstance(triplo, str) and triplo.strip().lower() in _DIAS:
            triple_weekday = _DIAS[triplo.strip().lower()]
        elif triplo in (None, False):
            triple_weekday = None
        else:
            raise ValueError(f"{p.name}: triple_day {triplo!r} nao e um dia da semana.")

        return cls(
            # Sinal invertido: o manifesto guarda o que a corretora credita
            # (negativo = debito). Aqui custo e positivo.
            buy_usd_oz=-float(swap["buy_points"]) * escala,
            sell_usd_oz=-float(swap["sell_points"]) * escala,
            triple_weekday=triple_weekday,
            rollover_hour_utc=int(rollover_hour_utc),
            broker_id=broker_id,
            digits=digits,
        )

    def por_lado(self, side: str) -> float:
        return self.buy_usd_oz if side == "buy" else self.sell_usd_oz


def acumulado_noturno(ts_ns: np.ndarray, custo: CustoNoturno, side: str) -> np.ndarray:
    """Custo de swap acumulado do inicio da serie ate cada barra, em USD/onca.

    Depende so do instante absoluto, nunca da entrada: por isso e calculado uma
    vez para a serie inteira, e o custo de uma posicao aberta em `j` e fechada
    em `i` sai como `acum[i] - acum[j]`.

    Duas convencoes, ambas marcadas VERIFICAR contra a corretora:

    - Rollover que cai em sabado ou domingo nao e cobrado. E o mercado fechado;
      e tambem a razao de existir dia triplo.
    - O rollover que cai NO `triple_day` vale por tres.

    A hora do rollover e obrigatoria e nao tem padrao. Para posicao de dias, a
    hora quase nao muda a conta; para posicao de horas, ela decide se ha
    cobranca ou nenhuma — e a maioria das operacoes esta nesse regime.
    """
    if ts_ns.size == 0:
        return np.zeros(0, dtype="float64")

    inicio = pd.Timestamp(ts_ns[0], unit="ns", tz="UTC")
    fim = pd.Timestamp(ts_ns[-1], unit="ns", tz="UTC")

    dias = pd.date_range(
        inicio.normalize(), fim.normalize() + pd.Timedelta(days=1), freq="D", tz="UTC"
    )
    instantes = dias + pd.Timedelta(hours=custo.rollover_hour_utc)

    peso = np.ones(len(instantes), dtype="float64")
    wd = instantes.weekday.to_numpy()
    peso[(wd == 5) | (wd == 6)] = 0.0
    if custo.triple_weekday is not None:
        peso[wd == custo.triple_weekday] *= 3.0

    acum_por_rollover = np.concatenate([[0.0], np.cumsum(peso)])
    idx = np.searchsorted(_ns(instantes), ts_ns, side="right")
    return acum_por_rollover[idx] * custo.por_lado(side)


# ---------------------------------------------------------------------- serie


@dataclass(frozen=True)
class Serie:
    """Barras M1 de bid e ask sobre o mesmo eixo de tempo.

    Bid e ask separados porque a conta muda de lado com a direcao: comprado
    entra no ask e sai no bid; vendido entra no bid e sai no ask. Usar mid para
    os dois esconderia o spread exatamente onde ele e pago.
    """

    ts_ns: np.ndarray
    bid_low: np.ndarray
    bid_high: np.ndarray
    ask_low: np.ndarray
    ask_high: np.ndarray
    pre_range: np.ndarray

    def __len__(self) -> int:
        return int(self.ts_ns.size)


def _meses(inicio: pd.Timestamp, fim: pd.Timestamp) -> list[tuple[int, int]]:
    out: list[tuple[int, int]] = []
    cur = pd.Timestamp(year=inicio.year, month=inicio.month, day=1, tz="UTC")
    while cur <= fim:
        out.append((cur.year, cur.month))
        cur = cur + pd.offsets.MonthBegin(1)
    return out


def montar_serie(ticks: pd.DataFrame, *, pre_janela: str = "15min") -> Serie:
    """Agrega ticks em M1 de bid e de ask e calcula o range pre-entrada.

    Reusa `bars.aggregate`, que e a referencia de paridade com o MQL5 e a unica
    funcao de agregacao do projeto. Reimplementar a agregacao aqui produziria
    um segundo conjunto de regras de fronteira que ninguem compara com o
    primeiro.

    O range pre-entrada e deslocado em uma barra de proposito: a janela tem de
    terminar ANTES do minuto da entrada.
    """
    bid = aggregate(ticks, GRADE_S, price="bid")
    ask = aggregate(ticks, GRADE_S, price="ask")

    if len(bid) != len(ask) or not (bid["ts_utc"].to_numpy() == ask["ts_utc"].to_numpy()).all():
        # Mesmo lote de ticks nos dois: um eixo diferente do outro so pode vir
        # de bug na agregacao, e alinhar por merge aqui esconderia esse bug.
        raise ValueError("eixos de bid e ask divergem; a agregacao esta inconsistente")

    idx = pd.DatetimeIndex(bid["ts_utc"])
    alto = pd.Series(bid["high"].to_numpy(), index=idx).rolling(pre_janela).max()
    baixo = pd.Series(bid["low"].to_numpy(), index=idx).rolling(pre_janela).min()
    pre = (alto - baixo).shift(1)

    return Serie(
        ts_ns=_ns(idx).copy(),
        bid_low=bid["low"].to_numpy(dtype="float64"),
        bid_high=bid["high"].to_numpy(dtype="float64"),
        ask_low=ask["low"].to_numpy(dtype="float64"),
        ask_high=ask["high"].to_numpy(dtype="float64"),
        pre_range=pre.to_numpy(dtype="float64"),
    )


def carregar(
    instrumento: str, inicio: pd.Timestamp, fim: pd.Timestamp, *, root: Path | None = None
) -> tuple[Serie, pd.DataFrame]:
    """Le os meses que cobrem [inicio, fim] e devolve a serie M1 e os ticks."""
    partes = [read_month(instrumento, y, m, root=root) for y, m in _meses(inicio, fim)]
    partes = [p for p in partes if not p.empty]
    if not partes:
        raise FileNotFoundError(
            f"nenhum tick de {instrumento} entre {inicio.date()} e {fim.date()}. "
            "Rode a ingestao antes; este estudo nao inventa preco."
        )
    ticks = pd.concat(partes, ignore_index=True).sort_values(
        "ts_utc", kind="stable", ignore_index=True
    )
    return montar_serie(ticks), ticks


def precos_de_entrada(ticks: pd.DataFrame, quando_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Bid e ask do primeiro tick em ou apos cada instante.

    Real e sintetica recebem o mesmo tratamento — as duas entram a preco de
    tick, no segundo. Dar precisao de tick a uma e de barra a outra criaria uma
    assimetria dentro da propria metrica que se quer comparar.
    """
    eixo = _ns(pd.DatetimeIndex(ticks["ts_utc"]))
    pos = np.searchsorted(eixo, quando_ns, side="left")
    pos = np.clip(pos, 0, eixo.size - 1)
    return (
        ticks["bid"].to_numpy(dtype="float64")[pos],
        ticks["ask"].to_numpy(dtype="float64")[pos],
    )


# ------------------------------------------------------------------ avaliacao


@dataclass(frozen=True)
class Resultado:
    mae_usd_oz: float
    horas_ate_verde: float | None
    horas_ate_verde_liq: float | None
    swap_usd_oz: float
    verde_em: dict[float, bool | None]


def avaliar(
    serie: Serie,
    idx0: int,
    entrada: float,
    sinal: int,
    swap_acum: np.ndarray,
    *,
    cobertura_ns: int,
    horizontes: tuple[float, ...] = HORIZONTES_H,
) -> Resultado:
    """MAE, tempo-ate-verde e nao-virou de uma posicao aberta na barra `idx0`.

    `sinal` e +1 comprado, -1 vendido. Comprado fica verde quando o BID sobe
    acima do ask de entrada; vendido, quando o ASK cai abaixo do bid de
    entrada. As duas pontas do spread entram, cada uma no seu lado.

    Censura e explicita. Uma entrada perto do fim do historico nao tem uma
    semana pela frente para observar, e conta-la como "virou" ou como "nao
    virou" enviesaria a fracao nos dois sentidos. Ela sai do denominador
    daquele horizonte, e o numero de censuradas e reportado — silenciar isso
    faria a fracao parecer medida quando esta truncada.
    """
    teto_ns = int(serie.ts_ns[idx0] + max(horizontes) * 3.6e12)
    fim = int(np.searchsorted(serie.ts_ns, teto_ns, side="right"))
    if fim <= idx0:
        fim = idx0 + 1

    if sinal > 0:
        favor = serie.bid_high[idx0:fim]
        contra = serie.bid_low[idx0:fim]
    else:
        favor = serie.ask_low[idx0:fim]
        contra = serie.ask_high[idx0:fim]

    pnl_favor = sinal * (favor - entrada)
    pnl_contra = sinal * (contra - entrada)

    swap = swap_acum[idx0:fim] - swap_acum[idx0]
    ts = serie.ts_ns[idx0:fim]

    def _primeiro(mask: np.ndarray) -> int | None:
        pos = int(np.argmax(mask)) if mask.any() else None
        return pos

    i_bruto = _primeiro(pnl_favor > 0.0)
    i_liq = _primeiro((pnl_favor - swap) > 0.0)

    ate = fim - idx0 if i_bruto is None else i_bruto + 1
    mae = float(max(0.0, -np.min(pnl_contra[:ate])))

    def _horas(i: int | None) -> float | None:
        if i is None:
            return None
        return float((ts[i] - ts[0]) / 3.6e12)

    # Custo de carrego efetivamente pago ate virar — em USD por onca, nao em
    # numero de noites. Noite e contagem; o que entra na comparacao e dinheiro
    # por onca, que e a unidade interna do projeto.
    swap_pago = float(swap[i_bruto] if i_bruto is not None else swap[-1])

    verde_em: dict[float, bool | None] = {}
    for h in horizontes:
        limite_ns = int(ts[0] + h * 3.6e12)
        if limite_ns > cobertura_ns:
            verde_em[h] = None
            continue
        dentro = ts <= limite_ns
        verde_em[h] = bool((pnl_favor[dentro] > 0.0).any())

    return Resultado(
        mae_usd_oz=mae,
        horas_ate_verde=_horas(i_bruto),
        horas_ate_verde_liq=_horas(i_liq),
        swap_usd_oz=swap_pago,
        verde_em=verde_em,
    )


# ---------------------------------------------------------------- casamentos


def _pool_valido(serie: Serie, cobertura_ns: int) -> np.ndarray:
    """Barras que tem horizonte maximo inteiro pela frente."""
    limite = cobertura_ns - int(max(HORIZONTES_H) * 3.6e12)
    return np.flatnonzero(serie.ts_ns <= limite)


def sortear(
    esquema: str,
    rng: np.random.Generator,
    serie: Serie,
    alvo_idx: int,
    pool: np.ndarray,
    k: int,
) -> tuple[np.ndarray, bool]:
    """Sorteia `k` indices sinteticos para uma entrada real. Devolve (indices, houve_recuo).

    `houve_recuo` marca que o casamento pedido nao tinha candidatos bastantes e
    o sorteio caiu para o esquema mais frouxo. Recuo silencioso transformaria
    "casado por volatilidade" em "casado por horario" sem que o relatorio
    mudasse de nome.
    """
    if esquema == "aleatorio":
        return rng.choice(pool, size=k, replace=True), False

    ts = pd.DatetimeIndex(serie.ts_ns[pool], tz="UTC")
    alvo = pd.Timestamp(serie.ts_ns[alvo_idx], unit="ns", tz="UTC")
    mesma_hora = pool[(ts.weekday == alvo.weekday()) & (ts.hour == alvo.hour)]

    if esquema == "horario":
        if mesma_hora.size == 0:
            return rng.choice(pool, size=k, replace=True), True
        return rng.choice(mesma_hora, size=k, replace=True), False

    if esquema != "volatilidade":
        raise ValueError(f"esquema desconhecido: {esquema!r}")

    base = mesma_hora if mesma_hora.size else pool
    recuo = mesma_hora.size == 0

    alvo_pre = serie.pre_range[alvo_idx]
    cand_pre = serie.pre_range[base]
    ok = np.isfinite(cand_pre)
    if not np.isfinite(alvo_pre) or ok.sum() < 10:
        return rng.choice(base, size=k, replace=True), True

    base, cand_pre = base[ok], cand_pre[ok]
    # Decil dentro do proprio pool do horario: o que se quer casar e "regime
    # parecido para AQUELA hora", nao "regime parecido no ano inteiro". Asia
    # calma e Londres calma sao coisas diferentes.
    cortes = np.quantile(cand_pre, np.linspace(0, 1, 11)[1:-1])
    balde_alvo = int(np.searchsorted(cortes, alvo_pre, side="right"))
    balde_cand = np.searchsorted(cortes, cand_pre, side="right")
    mesmo = base[balde_cand == balde_alvo]

    if mesmo.size < 5:
        return rng.choice(base, size=k, replace=True), True
    return rng.choice(mesmo, size=k, replace=True), recuo


# ---------------------------------------------------------------- estatistica


def prob_superioridade(a: np.ndarray, b: np.ndarray) -> float:
    """P(a < b) + 0.5 P(a == b). Estatistica U normalizada, sem scipy.

    0,50 e indistinguivel. Acima de 0,50 as reais tem valor menor — que para
    MAE e para tempo-ate-verde significa melhor.

    Rank e nao media de proposito: MAE tem cauda longa, e uma unica operacao
    catastrofica moveria a media sem dizer nada sobre a mediana do
    comportamento.
    """
    a = a[np.isfinite(a)]
    b = np.sort(b[np.isfinite(b)])
    if a.size == 0 or b.size == 0:
        return float("nan")
    menor = np.searchsorted(b, a, side="left")
    menor_ou_igual = np.searchsorted(b, a, side="right")
    empates = menor_ou_igual - menor
    maiores = b.size - menor_ou_igual
    return float((maiores + 0.5 * empates).sum() / (a.size * b.size))


def ic_bootstrap(
    a: np.ndarray,
    b: np.ndarray,
    grupos_a: np.ndarray,
    grupos_b: np.ndarray,
    rng: np.random.Generator,
    *,
    n: int = 1000,
) -> tuple[float, float]:
    """IC95 reamostrando EPISODIOS, nao entradas.

    Reamostrar entrada a entrada supoe independencia. Se ele empilha, as
    entradas de um episodio andam juntas, e o bootstrap por entrada devolve um
    intervalo estreito demais — falsa precisao, que e pior que precisao nenhuma
    porque convida a concluir.

    Reamostrar o episodio inteiro, com todas as suas entradas e todas as
    sinteticas que elas geraram, preserva a dependencia de dentro do episodio.
    Quando nao ha empilhamento, cada episodio tem uma entrada so e isto degenera
    no bootstrap comum — nao ha custo em usar sempre.
    """
    ids = np.unique(grupos_a)
    if ids.size < 3:
        return (float("nan"), float("nan"))

    por_ep_a = [a[grupos_a == g] for g in ids]
    por_ep_b = [b[grupos_b == g] for g in ids]

    amostras = np.empty(n, dtype="float64")
    for i in range(n):
        sorteados = rng.integers(0, ids.size, size=ids.size)
        amostras[i] = prob_superioridade(
            np.concatenate([por_ep_a[j] for j in sorteados]),
            np.concatenate([por_ep_b[j] for j in sorteados]),
        )
    ok = amostras[np.isfinite(amostras)]
    if ok.size < 10:
        return (float("nan"), float("nan"))
    return (float(np.quantile(ok, 0.025)), float(np.quantile(ok, 0.975)))


def _resumo(vals: list[float | None]) -> dict[str, float | int]:
    arr = np.array([v for v in vals if v is not None], dtype="float64")
    if arr.size == 0:
        return {"n": 0}
    return {
        "n": int(arr.size),
        "p50": float(np.quantile(arr, 0.50)),
        "p90": float(np.quantile(arr, 0.90)),
        "max": float(arr.max()),
    }


def _fracao_nao_virou(res: list[Resultado], h: float) -> dict[str, float | int]:
    obs = [r.verde_em[h] for r in res]
    censuradas = sum(1 for v in obs if v is None)
    validas = [v for v in obs if v is not None]
    if not validas:
        return {"n": 0, "censuradas": censuradas}
    return {
        "n": len(validas),
        "censuradas": censuradas,
        "nao_virou": float(sum(1 for v in validas if not v) / len(validas)),
    }


# ------------------------------------------------------------------- execucao


def rodar(
    entradas: list[Entrada],
    serie: Serie,
    ticks: pd.DataFrame,
    custo: CustoNoturno,
    *,
    k: int,
    seed: int,
    gap_min: float = 60.0,
) -> dict:
    """Roda os tres casamentos. Nenhum e eleito; os tres saem no relatorio."""
    rng = np.random.default_rng(seed)
    cobertura_ns = int(serie.ts_ns[-1])

    swap_acum = {
        "buy": acumulado_noturno(serie.ts_ns, custo, "buy"),
        "sell": acumulado_noturno(serie.ts_ns, custo, "sell"),
    }
    pool = _pool_valido(serie, cobertura_ns)
    if pool.size == 0:
        raise ValueError(
            "nenhuma barra tem o horizonte maximo inteiro pela frente. "
            "O historico de ticks e mais curto que a janela do estudo."
        )

    quando = np.array([e.ts_utc.value for e in entradas], dtype="int64")
    idx_real = np.searchsorted(serie.ts_ns, quando, side="right") - 1
    dentro = (idx_real >= 0) & (quando <= cobertura_ns)
    fora = int((~dentro).sum())

    entradas = [e for e, d in zip(entradas, dentro) if d]
    idx_real = idx_real[dentro]
    if not entradas:
        raise ValueError("nenhuma entrada cai dentro do historico de ticks carregado.")

    bid_r, ask_r = precos_de_entrada(ticks, np.array([e.ts_utc.value for e in entradas]))

    reais = [
        avaliar(
            serie, int(i), ask_r[n] if e.sinal > 0 else bid_r[n], e.sinal,
            swap_acum[e.side], cobertura_ns=cobertura_ns,
        )
        for n, (e, i) in enumerate(zip(entradas, idx_real))
    ]

    grupos = agrupar_episodios(entradas, gap_min)
    grupos_sint = np.repeat(grupos, k)
    tamanhos = np.bincount(grupos)

    saida: dict = {
        "entradas_reais": len(entradas),
        "entradas_fora_do_historico": fora,
        "k_por_entrada": k,
        "seed": seed,
        "episodios": {
            "gap_min": gap_min,
            "n_nominal": len(entradas),
            "n_efetivo": int(np.unique(grupos).size),
            "maior_episodio": int(tamanhos.max()),
            "entradas_por_episodio_p50": float(np.quantile(tamanhos, 0.5)),
            # A contagem em varios gaps mostra a ESCALA do empilhamento, e nao
            # depende de ninguem lembrar como opera.
            "n_efetivo_por_gap_min": {
                str(g): int(np.unique(agrupar_episodios(entradas, g)).size)
                for g in GAPS_DIAGNOSTICO_MIN
            },
        },
        "swap": {
            "broker_id": custo.broker_id,
            "digits": custo.digits,
            "buy_usd_oz_por_noite": custo.buy_usd_oz,
            "sell_usd_oz_por_noite": custo.sell_usd_oz,
            "rollover_hour_utc": custo.rollover_hour_utc,
            "triple_weekday": custo.triple_weekday,
        },
        "real": _bloco(reais),
        "casamentos": {},
    }

    for esquema in ("aleatorio", "horario", "volatilidade"):
        sint: list[Resultado] = []
        recuos = 0
        for e, i in zip(entradas, idx_real):
            escolhidos, recuo = sortear(esquema, rng, serie, int(i), pool, k)
            recuos += int(recuo)
            jitter = rng.integers(0, GRADE_S, size=escolhidos.size) * 1_000_000_000
            quando_s = serie.ts_ns[escolhidos] + jitter
            bid_s, ask_s = precos_de_entrada(ticks, quando_s)
            for m, j in enumerate(escolhidos):
                sint.append(
                    avaliar(
                        serie, int(j), ask_s[m] if e.sinal > 0 else bid_s[m], e.sinal,
                        swap_acum[e.side], cobertura_ns=cobertura_ns,
                    )
                )

        bloco = _bloco(sint)
        bloco["entradas_com_recuo_de_casamento"] = recuos
        bloco["comparacao"] = {
            "p_real_mae_menor": prob_superioridade(
                np.array([r.mae_usd_oz for r in reais]),
                np.array([r.mae_usd_oz for r in sint]),
            ),
            "p_real_mae_menor_ic95": ic_bootstrap(
                np.array([r.mae_usd_oz for r in reais]),
                np.array([r.mae_usd_oz for r in sint]),
                grupos,
                grupos_sint,
                rng,
            ),
            "p_real_verde_mais_rapido": prob_superioridade(
                np.array([r.horas_ate_verde if r.horas_ate_verde is not None else np.inf
                          for r in reais]),
                np.array([r.horas_ate_verde if r.horas_ate_verde is not None else np.inf
                          for r in sint]),
            ),
        }
        saida["casamentos"][esquema] = bloco

    return saida


def _bloco(res: list[Resultado]) -> dict:
    return {
        "n": len(res),
        "mae_usd_oz": _resumo([r.mae_usd_oz for r in res]),
        "horas_ate_verde": _resumo([r.horas_ate_verde for r in res]),
        "horas_ate_verde_liq_swap": _resumo([r.horas_ate_verde_liq for r in res]),
        "swap_pago_usd_oz": _resumo([r.swap_usd_oz for r in res]),
        "nao_virou": {f"{h:g}h": _fracao_nao_virou(res, h) for h in HORIZONTES_H},
    }


def imprimir(rel: dict) -> None:
    sw = rel["swap"]
    print()
    ep = rel["episodios"]
    print(f"entradas reais ............ {rel['entradas_reais']} nominais")
    if rel["entradas_fora_do_historico"]:
        print(f"  fora do historico ....... {rel['entradas_fora_do_historico']} (descartadas)")
    print(
        f"episodios (N efetivo) ..... {ep['n_efetivo']}   gap {ep['gap_min']:g} min"
        f"   maior episodio {ep['maior_episodio']} entrada(s)"
    )
    if ep["n_efetivo"] < ep["n_nominal"]:
        # A diferenca E a medida do empilhamento. Sem ela, o IC95 sairia
        # estreito demais e o estudo pareceria mais conclusivo do que e.
        por_gap = "  ".join(
            f"{g}min={n}" for g, n in ep["n_efetivo_por_gap_min"].items()
        )
        print(f"  ha empilhamento ......... {por_gap}   (o IC95 reamostra episodio)")
    print(f"sinteticas por entrada .... {rel['k_por_entrada']}   seed {rel['seed']}")
    print(
        f"swap ...................... buy {sw['buy_usd_oz_por_noite']:+.4f} USD/oz por noite"
        f"   rollover {sw['rollover_hour_utc']:02d}:00 UTC"
    )
    print()

    linhas = [("real", rel["real"])] + [
        (f"sint/{k}", v) for k, v in rel["casamentos"].items()
    ]
    # Sem caractere fora do ASCII no que e IMPRESSO: o console do Windows abre
    # em cp1252 e um travessao ou uma seta derrubam o relatorio inteiro com
    # UnicodeEncodeError, depois de todo o calculo ja ter rodado.
    cab = f"{'':<18}{'MAE p50':>9}{'MAE p90':>9}{'h ate verde p50':>17}{'p90':>8}"
    print(cab)
    print("-" * len(cab))
    for nome, b in linhas:
        m, v = b["mae_usd_oz"], b["horas_ate_verde"]
        print(
            f"{nome:<18}{m.get('p50', float('nan')):>9.2f}{m.get('p90', float('nan')):>9.2f}"
            f"{v.get('p50', float('nan')):>17.1f}{v.get('p90', float('nan')):>8.1f}"
        )

    print()
    for h in HORIZONTES_H:
        chave = f"{h:g}h"
        print(f"nao virou em {chave:>5}:", end="")
        for nome, b in linhas:
            f = b["nao_virou"][chave]
            txt = "  s/dado" if not f.get("n") else f"  {nome}={f['nao_virou']:.1%}"
            print(txt, end="")
        cens = rel["real"]["nao_virou"][chave].get("censuradas", 0)
        print(f"   (reais censuradas: {cens})")

    print()
    print("P(real melhor que sintetica). 0,50 e indistinguivel")
    for k, b in rel["casamentos"].items():
        c = b["comparacao"]
        lo, hi = c["p_real_mae_menor_ic95"]
        print(
            f"  {k:<14} MAE {c['p_real_mae_menor']:.3f}  IC95 [{lo:.3f}, {hi:.3f}]"
            f"   verde {c['p_real_verde_mais_rapido']:.3f}"
        )
        if b["entradas_com_recuo_de_casamento"]:
            print(
                f"  {'':<14} ATENCAO: {b['entradas_com_recuo_de_casamento']} entrada(s) "
                "cairam para casamento mais frouxo por falta de candidatos"
            )
    print()


# ------------------------------------------------------------------ autoteste


def _serie_sintetica(bid: np.ndarray, spread: float, inicio: pd.Timestamp) -> pd.DataFrame:
    ts = inicio + pd.to_timedelta(np.arange(bid.size), unit="s")
    return pd.DataFrame(
        {
            "ts_utc": ts,
            "bid": bid,
            "ask": bid + spread,
            "bid_vol": np.ones(bid.size, dtype="float32"),
            "ask_vol": np.ones(bid.size, dtype="float32"),
        }
    )


def autoteste() -> int:
    """Caso POSITIVO conhecido. Verde num repositorio vazio nao prova nada.

    Uma ferramenta de medicao que so foi testada contra dado que ela nao
    entende responde "nada encontrado" tanto quando nao ha achado quanto quando
    esta quebrada. As tres formas de serie abaixo tem resposta calculavel a
    mao, e e contra elas que o calculo se defende. Fixture gerada em codigo,
    pela ADR 0001.
    """
    falhas = 0
    inicio = pd.Timestamp("2026-03-02 00:00:00", tz="UTC")  # segunda-feira
    spread = 0.20
    custo = CustoNoturno(
        buy_usd_oz=0.5238, sell_usd_oz=0.0, triple_weekday=2,
        rollover_hour_utc=21, broker_id="autoteste", digits=3,
    )

    def checa(nome: str, obtido: float | None, esperado: float | None, tol: float) -> None:
        nonlocal falhas
        if esperado is None or obtido is None:
            ok = obtido is esperado
            detalhe = f"obtido={obtido} esperado={esperado}"
        else:
            ok = abs(obtido - esperado) <= tol
            detalhe = f"obtido={obtido:.4f} esperado={esperado:.4f} tol={tol}"
        print(f"  [{'ok ' if ok else 'FALHA'}] {nome}: {detalhe}")
        if not ok:
            falhas += 1

    def checa_min(nome: str, obtido: float, minimo: float) -> None:
        nonlocal falhas
        ok = obtido >= minimo
        print(f"  [{'ok ' if ok else 'FALHA'}] {nome}: obtido={obtido:.4f} minimo={minimo}")
        if not ok:
            falhas += 1

    # ---- V: cai 5,00 em 30 min, sobe ate +10,00 em mais 60 min -------------
    desce = np.linspace(2000.0, 1995.0, 30 * 60, endpoint=False)
    sobe = np.linspace(1995.0, 2010.0, 60 * 60)
    resto = np.full(30 * 60, 2010.0)
    ticks = _serie_sintetica(np.concatenate([desce, sobe, resto]), spread, inicio)
    serie = montar_serie(ticks)
    acum = acumulado_noturno(serie.ts_ns, custo, "buy")
    cob = int(serie.ts_ns[-1])

    print("V comprado (entrada no ask=2000,20; fundo em 1995,00)")
    r = avaliar(serie, 0, 2000.0 + spread, +1, acum, cobertura_ns=cob, horizontes=(1.0,))
    checa("MAE = 5,20", r.mae_usd_oz, 5.20, 0.01)
    # bid cruza 2000,20 em ~50,8 min; a barra M1 resolve para o minuto.
    checa("tempo-ate-verde ~ 50 min", r.horas_ate_verde, 50.0 / 60.0, 2.0 / 60.0)
    checa("verde dentro de 1h", 1.0 if r.verde_em[1.0] else 0.0, 1.0, 0.0)

    # ---- queda e fundo chato: nunca vira ----------------------------------
    # O fundo chato existe para que o MAE nao dependa de onde o horizonte
    # corta a serie. Com queda continua, a resposta certa seria funcao da
    # borda da ultima barra — e um teste cuja expectativa e a propria conta
    # do codigo nao testa nada.
    print("queda ate 1990 e fundo chato, comprado (nunca vira)")
    cai = np.concatenate(
        [np.linspace(2000.0, 1990.0, 30 * 60, endpoint=False), np.full(150 * 60, 1990.0)]
    )
    serie2 = montar_serie(_serie_sintetica(cai, spread, inicio))
    acum2 = acumulado_noturno(serie2.ts_ns, custo, "buy")
    r2 = avaliar(
        serie2, 0, 2000.0 + spread, +1, acum2,
        cobertura_ns=int(serie2.ts_ns[-1]), horizontes=(1.0,),
    )
    checa("nunca vira", r2.horas_ate_verde, None, 0.0)
    checa("MAE = 10,20", r2.mae_usd_oz, 10.20, 0.02)
    checa("nao verde em 1h", 0.0 if not r2.verde_em[1.0] else 1.0, 0.0, 0.0)

    # ---- censura: horizonte maior que a cobertura -------------------------
    print("censura")
    r3 = avaliar(
        serie2, 0, 2000.0 + spread, +1, acum2,
        cobertura_ns=int(serie2.ts_ns[-1]), horizontes=(24.0,),
    )
    checa("24h alem da cobertura vira censura", 1.0 if r3.verde_em[24.0] is None else 0.0, 1.0, 0.0)

    # ---- swap: segunda 22:00 -> sexta, com quarta tripla -------------------
    print("swap acumulado")
    eixo = pd.date_range(inicio, inicio + pd.Timedelta(days=9), freq="1min", tz="UTC")
    eixo_ns = _ns(eixo)
    acum3 = acumulado_noturno(eixo_ns, custo, "buy")

    def em(quando: str) -> int:
        return int(np.searchsorted(eixo_ns, pd.Timestamp(quando, tz="UTC").value))

    # Rollover as 21:00. Aberta segunda 22:00 — o rollover de segunda ja passou.
    # Cruzados: ter(1) + qua(3, dia triplo) + qui(1) = 5. O de sexta e as 21:00,
    # depois do fecho as 12:00, e nao entra.
    checa(
        "5 noites entre seg 22h e sex 12h",
        (acum3[em("2026-03-06 12:00")] - acum3[em("2026-03-02 22:00")]) / 0.5238, 5.0, 1e-9,
    )
    checa(
        "fim de semana nao cobra",
        (acum3[em("2026-03-09 00:00")] - acum3[em("2026-03-07 00:00")]) / 0.5238, 0.0, 1e-9,
    )
    # Caso negativo do dia triplo: sem quarta no meio, tres dias sao tres noites.
    sem_triplo = CustoNoturno(
        buy_usd_oz=0.5238, sell_usd_oz=0.0, triple_weekday=None,
        rollover_hour_utc=21, broker_id="autoteste", digits=3,
    )
    acum4 = acumulado_noturno(eixo_ns, sem_triplo, "buy")
    checa(
        "sem dia triplo, seg 22h a sex 12h da 3 noites",
        (acum4[em("2026-03-06 12:00")] - acum4[em("2026-03-02 22:00")]) / 0.5238, 3.0, 1e-9,
    )

    # ---- prob_superioridade: caso conhecido -------------------------------
    print("estatistica")
    checa(
        "identicas dao 0,50",
        prob_superioridade(np.array([1.0, 2.0, 3.0]), np.array([1.0, 2.0, 3.0])),
        0.5, 1e-12,
    )
    checa(
        "todas menores dao 1,00",
        prob_superioridade(np.array([1.0, 1.0]), np.array([5.0, 6.0])),
        1.0, 1e-12,
    )

    # `_controles` conta pelo proprio `checa`, que fecha sobre `falhas`. Nada
    # de `falhas += _controles(...)`: o `+=` le `falhas` ANTES de chamar a
    # funcao e grava o valor velho por cima do que ela incrementou — e o
    # autoteste sai verde com falha na lista, que e o pior desfecho possivel
    # para uma ferramenta de verificacao.
    # ---- episodios --------------------------------------------------------
    print("agrupamento de episodios")
    t0 = pd.Timestamp("2026-03-02 10:00", tz="UTC")
    seq = [
        Entrada(t0, "buy"),                                   # episodio 0
        Entrada(t0 + pd.Timedelta(minutes=1), "buy"),         # 0: empilhou
        Entrada(t0 + pd.Timedelta(minutes=2), "buy"),         # 0: empilhou
        Entrada(t0 + pd.Timedelta(minutes=3), "sell"),        # 1: inverter nao e empilhar
        Entrada(t0 + pd.Timedelta(hours=4), "sell"),          # 2: longe demais
    ]
    checa("gap 60 min junta o empilhamento: 3 episodios",
          float(np.unique(agrupar_episodios(seq, 60)).size), 3.0, 0)
    checa("gap menor que o espacamento separa: 5 episodios",
          float(np.unique(agrupar_episodios(seq, 0.5)).size), 5.0, 0)
    checa("gap zero isola tudo: 5 episodios",
          float(np.unique(agrupar_episodios(seq, 0)).size), 5.0, 0)

    # O IC95 por episodio TEM de ser mais largo que o por entrada quando ha
    # empilhamento. Se nao for, o agrupamento nao esta chegando ao bootstrap e
    # o intervalo continua estreito demais — falsa precisao, que convida a
    # concluir. 20 episodios de 5 entradas, com efeito comum dentro de cada um.
    g = np.random.default_rng(5)
    ef_a = np.repeat(g.normal(0, 1.0, 20), 5)
    ef_b = np.repeat(g.normal(0, 1.0, 20), 5)
    va = ef_a + g.normal(0, 0.1, 100)
    vb = ef_b + g.normal(0, 0.1, 100)
    grupos = np.repeat(np.arange(20), 5)
    solto = np.arange(100)

    lo_e, hi_e = ic_bootstrap(va, vb, grupos, grupos, np.random.default_rng(5))
    lo_s, hi_s = ic_bootstrap(va, vb, solto, solto, np.random.default_rng(5))
    print(f"  IC por episodio [{lo_e:.3f}, {hi_e:.3f}]  por entrada [{lo_s:.3f}, {hi_s:.3f}]")
    checa_min("IC por episodio e mais largo", (hi_e - lo_e) - (hi_s - lo_s), 0.02)

    _controles(checa, checa_min)

    print()
    print("autoteste: FALHOU" if falhas else "autoteste: ok")
    return 1 if falhas else 0


def _controles(checa, checa_min) -> None:
    """Controle negativo e controle positivo do caminho completo.

    As checagens acima validam as pecas: MAE, virada, swap, estatistica. Nada
    delas prova que `rodar` — sorteio, casamento, comparacao — responde certo.
    Uma ferramenta que so foi vista em verde nao distingue "nao ha achado" de
    "nao consigo achar", e este estudo existe justamente para produzir um
    numero em que se vai acreditar.

    NEGATIVO  passeio aleatorio, entradas aleatorias. Nao ha edge nenhum para
              encontrar, entao P tem de ficar em 0,50. Se der longe disso, o
              vies esta no sorteio ou no casamento, e qualquer resultado sobre
              dado real estaria contaminado pelo mesmo vies.

    POSITIVO  o mesmo passeio aleatorio, com um movimento favoravel PLANTADO
              logo depois de cada entrada real. Ha edge de timing por
              construcao. Se a ferramenta nao acusar ISSO, ela nao acusaria
              nada.

    O movimento plantado comeca NA entrada e volta ao ponto de partida depois,
    de proposito: assim ele nao mexe no range pre-entrada nem no nivel medio da
    serie, e nenhum dos tres casamentos consegue absorve-lo. Um edge plantado
    que o casamento por volatilidade pudesse explicar mediria o casamento, nao
    a ferramenta.
    """
    custo = CustoNoturno(
        buy_usd_oz=0.5238, sell_usd_oz=0.0, triple_weekday=2,
        rollover_hour_utc=21, broker_id="autoteste", digits=3,
    )
    inicio = pd.Timestamp("2026-03-02 00:00", tz="UTC")
    passo_s = 10
    n = 20 * 24 * (3600 // passo_s)
    limite_entradas = 10 * 24 * (3600 // passo_s)

    def _rodar(bid: np.ndarray, quais: np.ndarray, seed: int) -> dict:
        ts = inicio + pd.to_timedelta(np.arange(bid.size) * passo_s, unit="s")
        ticks = pd.DataFrame({
            "ts_utc": ts, "bid": bid, "ask": bid + 0.20,
            "bid_vol": np.ones(bid.size, dtype="float32"),
            "ask_vol": np.ones(bid.size, dtype="float32"),
        })
        entradas = [Entrada(ts_utc=ts[i], side="buy") for i in quais]
        return rodar(entradas, montar_serie(ticks), ticks, custo, k=10, seed=seed)

    g = np.random.default_rng(11)
    base = 3300.0 + np.cumsum(g.normal(0.0, 0.05, n))
    quais = np.sort(g.choice(limite_entradas, 120, replace=False))

    # ---- negativo ---------------------------------------------------------
    print("controle negativo (passeio aleatorio, entradas aleatorias)")
    rel = _rodar(base, quais, seed=11)
    for esquema, b in rel["casamentos"].items():
        checa(
            f"{esquema}: P fica em 0,50",
            b["comparacao"]["p_real_mae_menor"], 0.50, 0.10,
        )

    # ---- positivo ---------------------------------------------------------
    #
    # A rampa sobe RAPIDO e desce devagar. Subida lenta nao serve: o MAE e
    # medido so ate virar o verde, e nessa janela curta o ruido do passeio
    # ainda domina uma rampa suave — o edge existe e a ferramenta o subestima,
    # o que faria o controle medir a rampa em vez da ferramenta.
    #
    # E ha um teto que nao se vence: o MAE nunca fica abaixo do spread, porque
    # comprado entra no ask e mede o bid. Real e sintetica empilham no mesmo
    # piso de 0,20, empate conta meio, e P nao chega perto de 1,00 por mais
    # forte que seja o edge. Por isso o limiar e direcional e nao proximidade
    # de 1,00.
    print("controle positivo (mesma serie, movimento favoravel plantado)")
    subida = 5 * (60 // passo_s)
    descida = 55 * (60 // passo_s)
    onda = np.concatenate([
        np.linspace(0.0, 2.0, subida, endpoint=False),
        np.linspace(2.0, 0.0, descida),
    ])
    plantado = base.copy()
    for i in quais:
        fim = min(i + onda.size, plantado.size)
        plantado[i:fim] += onda[: fim - i]
    rel = _rodar(plantado, quais, seed=11)
    for esquema, b in rel["casamentos"].items():
        checa_min(
            f"{esquema}: P acusa o edge plantado",
            b["comparacao"]["p_real_mae_menor"], 0.80,
        )


# ---------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Baseline aleatorio casado das entradas manuais. Exploracao."
    )
    p.add_argument("--self-test", action="store_true", help="roda contra caso positivo conhecido")
    p.add_argument("--deals", type=Path, help="CSV do historico de deals (fica em RISER-data)")
    # Sem padrao, nos dois. Simbolo nao se adivinha (invariante 3), e a
    # corretora decide o swap — deixar uma delas como padrao faria o estudo
    # responder sobre um custo que ninguem escolheu.
    p.add_argument("--instrumento", help="chave do instrumento no armazenamento de ticks")
    p.add_argument("--broker", help="id do manifesto em config/brokers/")
    p.add_argument(
        "--rollover-hour-utc", type=int,
        help="hora UTC do rollover da corretora. Obrigatorio: server.timezone_offset "
             "ainda esta VERIFICAR nos manifestos, e adivinhar decide sozinho se uma "
             "operacao de horas paga swap ou nao paga nenhum.",
    )
    p.add_argument("--k", type=int, default=20, help="sinteticas por entrada real")
    p.add_argument(
        "--episodio-gap-min", type=float, default=60.0,
        help="entradas na mesma direcao dentro deste intervalo sao um episodio so. "
             "O relatorio traz a contagem em varios gaps para mostrar a escala.",
    )
    p.add_argument("--seed", type=int, default=20260808)
    args = p.parse_args(argv)

    if args.self_test:
        return autoteste()

    if not (args.deals and args.instrumento and args.broker) or args.rollover_hour_utc is None:
        p.error(
            "--deals, --instrumento, --broker e --rollover-hour-utc sao "
            "obrigatorios fora do --self-test"
        )

    entradas = ler_entradas(args.deals)
    if not entradas:
        raise SystemExit("nenhuma entrada no CSV.")

    inicio = entradas[0].ts_utc
    fim = entradas[-1].ts_utc + pd.Timedelta(hours=max(HORIZONTES_H))
    serie, ticks = carregar(args.instrumento, inicio, fim)
    custo = CustoNoturno.from_manifest(args.broker, args.rollover_hour_utc)

    rel = rodar(
        entradas, serie, ticks, custo,
        k=args.k, seed=args.seed, gap_min=args.episodio_gap_min,
    )
    rel["instrumento"] = args.instrumento
    imprimir(rel)

    # Resultado e dado: vive fora do repositorio (invariante 7).
    dest = data_root() / "lab" / "baseline_casado"
    dest.mkdir(parents=True, exist_ok=True)
    carimbo = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    alvo = dest / f"{args.instrumento}-{carimbo}.json"
    alvo.write_text(json.dumps(rel, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"relatorio: {alvo}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
