"""Confere o manifesto da corretora contra o que o servidor diz.

O manifesto de `config/brokers/` e a DECLARACAO. O JSON produzido por
`mql5/Scripts/ReadSymbolSpecs.mq5` e a MEDICAO. Este modulo compara os dois.

Nao corrige nada. Corrigir automaticamente transformaria uma divergencia que
alguem precisa ver numa edicao silenciosa de arquivo versionado — e o manifesto
e exatamente o lugar onde uma edicao silenciosa custa caro, porque tudo a
jusante assume que ele foi conferido por uma pessoa.

Divergencia e ERRO, nao aviso. Campo ainda marcado VERIFICAR e outra coisa:
nao e divergencia, e ausencia de declaracao. Os dois aparecem separados porque
pedem acoes diferentes — um manda corrigir, o outro manda preencher.

---------------------------------------------------------------- por que digits

`digits` e o VERIFICAR de maior alcance do projeto. Com 2 em vez de 3, o ponto
vale dez vezes mais e toda conversao sai dez vezes errada: swap, spread,
distancia de stop, degrau de trailing. E sai plausivel. E a mesma classe de
falha do ask/bid trocado no `.bi5` e do mes com base zero da Dukascopy — o dado
continua processando e o erro so aparece quando alguem calcula custo.

Por isso este modulo nao se limita a comparar campo a campo: verifica tambem a
COERENCIA interna do que o servidor devolveu (`point` contra `digits`,
`tick_size` contra `point`) e imprime o swap ja convertido para USD por onca,
que e o numero em que um erro de escala fica visivel a olho.
"""

from __future__ import annotations

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml

from riser.core.paths import data_root, repo_root

VERIFICAR = "VERIFICAR"

ERRO = "erro"
PENDENTE = "pendente"
INFO = "info"


@dataclass(frozen=True)
class Achado:
    campo: str
    manifesto: Any
    servidor: Any
    severidade: str
    nota: str = ""

    def __str__(self) -> str:
        return (
            f"[{self.severidade.upper():<8}] {self.campo}\n"
            f"           manifesto: {self.manifesto!r}\n"
            f"           servidor : {self.servidor!r}"
            + (f"\n           {self.nota}" if self.nota else "")
        )


@dataclass(frozen=True)
class Campo:
    """Um par declaracao/medicao comparavel diretamente."""

    manifesto: str
    servidor: str
    tipo: Callable[[Any], Any]
    nota: str = ""


CAMPOS: tuple[Campo, ...] = (
    Campo("symbol.digits", "specs.digits", int,
          "escala de tudo: um digito a menos multiplica toda conversao por dez"),
    Campo("symbol.contract_oz", "specs.trade_contract_size", float,
          "tamanho do contrato: entra no risco em JPY e no lote na borda"),
    Campo("symbol.volume_min", "specs.volume_min", float),
    Campo("symbol.volume_max", "specs.volume_max", float),
    Campo("symbol.volume_step", "specs.volume_step", float),
    Campo("symbol.stops_level_points", "specs.trade_stops_level", int,
          "distancia minima de stop; zero significa stop colado ao preco"),
    Campo("account.currency", "account.currency", str,
          "moeda de risco e resultado (invariante 2)"),
    Campo("account.profit_currency", "specs.currency_profit", str),
    Campo("execution.account_mode", "account.margin_mode", str,
          "hedging e netting mudam o que 'uma posicao' significa"),
)


# ------------------------------------------------------------------- leitura


def _cavar(d: dict, caminho: str) -> Any:
    """Desce por 'a.b.c'. Ausencia devolve o marcador, nunca None silencioso."""
    atual: Any = d
    for parte in caminho.split("."):
        if not isinstance(atual, dict) or parte not in atual:
            return _AUSENTE
        atual = atual[parte]
    return atual


class _Ausente:
    def __repr__(self) -> str:
        return "<ausente>"


_AUSENTE = _Ausente()


def carregar_manifesto(broker_id: str, *, root: Path | None = None) -> dict:
    p = (root or repo_root()) / "config" / "brokers" / f"{broker_id}.yaml"
    if not p.is_file():
        raise FileNotFoundError(f"manifesto nao encontrado: {p}")
    return yaml.safe_load(p.read_text(encoding="utf-8"))


def achar_specs(*, root: Path | None = None) -> Path:
    """O `-latest.json` mais recente sob RISER-data/mt5/<alias>/<hash>/symbol-specs/.

    Varre todos os aliases e todas as contas de proposito: se houver mais de um,
    escolher em silencio o de um terminal qualquer compararia o manifesto de uma
    corretora com a medicao de outra.
    """
    base = (root or data_root()) / "mt5"
    achados = sorted(
        base.glob("*/*/symbol-specs/*-latest.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not achados:
        raise FileNotFoundError(
            f"nenhum symbol-specs em {base}. Rode ReadSymbolSpecs.mq5 no "
            "terminal antes: este verificador nao adivinha o que o servidor diz."
        )
    if len({p.parent for p in achados}) > 1:
        outros = ", ".join(str(p) for p in achados[1:4])
        raise ValueError(
            f"mais de uma conta com specs gravadas. Escolha explicitamente com "
            f"--specs. Mais recente: {achados[0]}. Outras: {outros}"
        )
    return achados[0]


def carregar_specs(path: Path) -> dict:
    dados = json.loads(path.read_text(encoding="utf-8"))
    schema = dados.get("schema")
    if schema != "symbol-specs/1":
        raise ValueError(
            f"{path.name}: schema {schema!r} nao reconhecido. Ler um schema "
            "desconhecido como se fosse o atual produziria comparacao errada."
        )
    return dados


# ---------------------------------------------------------------- comparacao


def _e_verificar(v: Any) -> bool:
    return isinstance(v, str) and VERIFICAR in v.upper()


def _igual(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
        except (TypeError, ValueError):
            return False
    if isinstance(a, str) and isinstance(b, str):
        return a.strip().lower() == b.strip().lower()
    return a == b


def _compara_campos(man: dict, spec: dict) -> Iterable[Achado]:
    for c in CAMPOS:
        declarado = _cavar(man, c.manifesto)
        medido = _cavar(spec, c.servidor)

        if medido is _AUSENTE:
            yield Achado(c.manifesto, declarado, medido, ERRO,
                         f"o JSON de specs nao traz '{c.servidor}'")
            continue
        if declarado is _AUSENTE:
            yield Achado(c.manifesto, declarado, medido, PENDENTE,
                         "campo ausente do manifesto; o servidor tem valor")
            continue
        if _e_verificar(declarado):
            yield Achado(c.manifesto, declarado, medido, PENDENTE,
                         "preencher com o valor medido, depois de conferir")
            continue

        try:
            esperado = c.tipo(declarado)
        except (TypeError, ValueError):
            yield Achado(c.manifesto, declarado, medido, ERRO,
                         f"valor do manifesto nao converte para {c.tipo.__name__}")
            continue

        if not _igual(esperado, c.tipo(medido)):
            yield Achado(c.manifesto, declarado, medido, ERRO, c.nota)


def _compara_simbolo(man: dict, spec: dict) -> Iterable[Achado]:
    """O simbolo medido tem de estar na lista `resolve` do manifesto.

    `resolve` e a unica chave do manifesto autorizada a conter simbolo literal
    (invariante 3). Medir um simbolo que nao esta nela significa que a medicao
    e de outro instrumento — e comparar digits nesse caso e pior que nao
    comparar, porque o resultado parece valido.
    """
    lista = _cavar(man, "symbol.resolve")
    medido = _cavar(spec, "symbol")
    if not isinstance(lista, list):
        yield Achado("symbol.resolve", lista, medido, ERRO, "ausente ou nao e lista")
        return
    if medido not in lista:
        yield Achado("symbol.resolve", lista, medido, ERRO,
                     "o simbolo medido nao esta na lista de resolucao: a medicao "
                     "pode ser de outro instrumento")


def _compara_filling(man: dict, spec: dict) -> Iterable[Achado]:
    declarado = _cavar(man, "execution.filling")
    medido = _cavar(spec, "specs.filling_mode")
    if not isinstance(declarado, list) or not isinstance(medido, list):
        yield Achado("execution.filling", declarado, medido, ERRO, "ausente ou nao e lista")
        return

    d = {str(x).strip().upper() for x in declarado}
    m = {str(x).strip().upper() for x in medido}

    # Faltar no servidor e erro: o manifesto promete um modo que a corretora
    # nao oferece, e a ordem so falha na hora de enviar. Sobrar no servidor e
    # informacao: o manifesto esta incompleto, nao errado.
    if d - m:
        yield Achado("execution.filling", sorted(d), sorted(m), ERRO,
                     f"declarado(s) e indisponivel(is) no servidor: {sorted(d - m)}")
    if m - d:
        yield Achado("execution.filling", sorted(d), sorted(m), INFO,
                     f"o servidor oferece a mais: {sorted(m - d)}")


def _compara_swap(man: dict, spec: dict) -> Iterable[Achado]:
    """Swap so e comparavel se o servidor cobra em PONTOS.

    `SYMBOL_SWAP_MODE` define a UNIDADE de `swap_long`/`swap_short`. Em POINTS
    sao pontos; em CURRENCY_* sao dinheiro; em INTEREST_* sao porcentagem ao
    ano. O manifesto declara `buy_points`, e comparar um numero em pontos com
    um numero que nao esta em pontos daria "confere" ou "diverge" por acidente
    da magnitude, sem nada a ver com a verdade.
    """
    modo = _cavar(spec, "swap.mode")
    if modo != "POINTS":
        yield Achado("swap.mode", "buy_points/sell_points (implica POINTS)", modo, ERRO,
                     "o manifesto declara swap em pontos, mas o servidor cobra "
                     "noutra unidade. Os valores de swap do manifesto nao "
                     "significam o que dizem e a comparacao numerica fica suspensa.")
        return

    for lado, chave_man, chave_spec in (
        ("compra", "swap.buy_points", "swap.long"),
        ("venda", "swap.sell_points", "swap.short"),
    ):
        declarado = _cavar(man, chave_man)
        medido = _cavar(spec, chave_spec)
        if declarado is _AUSENTE or medido is _AUSENTE:
            yield Achado(chave_man, declarado, medido, ERRO, "ausente de um dos lados")
            continue
        if _e_verificar(declarado):
            yield Achado(chave_man, declarado, medido, PENDENTE, f"swap de {lado}")
            continue
        if not _igual(float(declarado), float(medido)):
            yield Achado(chave_man, declarado, medido, ERRO,
                         f"swap de {lado} divergente; o custo de carrego domina "
                         "qualquer estilo que segure posicao")

    declarado_dia = _cavar(man, "swap.triple_day")
    medido_dia = _cavar(spec, "swap.rollover3days")
    if _e_verificar(declarado_dia):
        yield Achado("swap.triple_day", declarado_dia, medido_dia, PENDENTE)
    elif not _igual(str(declarado_dia), str(medido_dia)):
        yield Achado("swap.triple_day", declarado_dia, medido_dia, ERRO,
                     "o dia triplo varia por corretora e triplica o custo de "
                     "uma noite; errar o dia erra a conta da semana inteira")


def _compara_conta(man: dict, spec: dict) -> Iterable[Achado]:
    """`is_demo` do manifesto contra o modo real da conta conectada.

    O mesmo terminal serve conta demo e conta real. Quem determina `source` e o
    login resolvido em runtime, nao o terminal — e marcar log de conta real com
    `source: demo` (ou o contrario) contamina toda medicao de custo depois.
    """
    declarado = _cavar(man, "is_demo")
    medido = _cavar(spec, "account.source")
    if declarado is _AUSENTE or medido is _AUSENTE:
        yield Achado("is_demo", declarado, medido, ERRO, "ausente de um dos lados")
        return
    esperado = "demo" if declarado else "live"
    if medido == "contest":
        yield Achado("is_demo", declarado, medido, ERRO,
                     "conta de concurso nao e demo nem real; nao ha manifesto para ela")
    elif medido != esperado:
        yield Achado("is_demo", declarado, medido, ERRO,
                     "a conta conectada nao e do tipo que este manifesto declara")


def _coerencia_do_servidor(spec: dict) -> Iterable[Achado]:
    """O que o servidor diz tem de fechar consigo mesmo.

    Estas checagens nao olham o manifesto. Existem porque um `point` que nao
    corresponde a `digits`, ou um `tick_size` diferente de `point`, quebra
    suposicoes que o projeto inteiro faz — e nesse caso o problema nao e o
    manifesto estar errado, e o instrumento nao ser o que se pensava.
    """
    digits = _cavar(spec, "specs.digits")
    point = _cavar(spec, "specs.point")
    tick = _cavar(spec, "specs.trade_tick_size")

    if isinstance(digits, int) and isinstance(point, (int, float)):
        esperado = 10.0 ** (-digits)
        if not math.isclose(float(point), esperado, rel_tol=1e-9):
            yield Achado("specs.point", f"10^-{digits} = {esperado:g}", point, ERRO,
                         "o servidor devolveu point incoerente com digits")

    if isinstance(tick, (int, float)) and isinstance(point, (int, float)) and point:
        if not math.isclose(float(tick), float(point), rel_tol=1e-9):
            yield Achado("specs.trade_tick_size", f"= point ({point})", tick, INFO,
                         "o incremento de preco nao e o ponto: toda distancia "
                         "calculada em multiplos de point precisa ser revista")


def _derivados(spec: dict) -> list[str]:
    """Numeros derivados, so para leitura humana. Nao geram achado.

    Existem porque um erro de escala e invisivel no campo cru e obvio no valor
    convertido: 0,52 por noite e plausivel; 5,24 por noite nao e.
    """
    linhas: list[str] = []
    digits = _cavar(spec, "specs.digits")
    modo = _cavar(spec, "swap.mode")
    if not isinstance(digits, int):
        return linhas

    escala = 10.0 ** (-digits)
    linhas.append(f"point derivado de digits={digits}: {escala:g} USD por onca")

    if modo == "POINTS":
        for nome, chave in (("compra", "swap.long"), ("venda", "swap.short")):
            v = _cavar(spec, chave)
            if isinstance(v, (int, float)):
                # `+ 0.0` mata o zero negativo: -0,0000 num relatorio de custo
                # faz procurar sinal trocado onde so ha ausencia de swap.
                linhas.append(
                    f"swap de {nome}: {v:g} -> {-v * escala + 0.0:+.4f} USD por onca "
                    "por rollover (custo positivo)"
                )
    else:
        linhas.append(f"swap em modo {modo}: nao ha conversao para USD por onca aqui")

    spread = _cavar(spec, "instantaneo.spread_derivado_por_digits")
    if isinstance(spread, (int, float)):
        linhas.append(
            f"spread instantaneo: {spread:.4f} USD por onca "
            "(referencia de escala; spread medido vem do coletor)"
        )
    return linhas


def verificar(man: dict, spec: dict) -> list[Achado]:
    achados: list[Achado] = []
    achados += list(_compara_simbolo(man, spec))
    achados += list(_compara_campos(man, spec))
    achados += list(_compara_filling(man, spec))
    achados += list(_compara_swap(man, spec))
    achados += list(_compara_conta(man, spec))
    achados += list(_coerencia_do_servidor(spec))
    ordem = {ERRO: 0, PENDENTE: 1, INFO: 2}
    return sorted(achados, key=lambda a: (ordem.get(a.severidade, 9), a.campo))


# ---------------------------------------------------------------------- cli


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Confere config/brokers/<id>.yaml contra as specs lidas do servidor."
    )
    p.add_argument("--broker", required=True, help="id do manifesto em config/brokers/")
    p.add_argument("--specs", type=Path, help="JSON do ReadSymbolSpecs (padrao: o mais recente)")
    p.add_argument("--pendente-e-erro", action="store_true",
                   help="sair diferente de zero tambem com VERIFICAR por preencher")
    args = p.parse_args(argv)

    man = carregar_manifesto(args.broker)
    caminho = args.specs or achar_specs()
    spec = carregar_specs(caminho)

    achados = verificar(man, spec)

    print()
    print(f"manifesto : config/brokers/{args.broker}.yaml")
    print(f"medicao   : {caminho}")
    print(f"simbolo   : {spec.get('symbol')}   conta {_cavar(spec, 'account.hash')}"
          f"   {_cavar(spec, 'account.source')}   lido em {spec.get('lido_em_utc')}")
    print()
    for linha in _derivados(spec):
        print(f"  {linha}")
    print()

    erros = [a for a in achados if a.severidade == ERRO]
    pendentes = [a for a in achados if a.severidade == PENDENTE]

    if not achados:
        print("nenhum achado: manifesto e servidor batem em tudo que se compara.")
    for a in achados:
        print(a)
        print()

    print(f"{len(erros)} erro(s), {len(pendentes)} pendente(s).")
    if erros:
        # Nada fora do ASCII no que e IMPRESSO: o console do Windows abre em
        # cp1252 e um travessao sai corrompido, ou derruba tudo depois do
        # calculo ja ter rodado.
        print("Divergencia entre manifesto e servidor e ERRO. Corrija o manifesto "
              "a mao, depois de decidir qual dos dois esta certo. Este "
              "verificador nao edita arquivo versionado.")
    if pendentes:
        print("Pendente e campo VERIFICAR ainda por preencher: nao e divergencia, "
              "e ausencia de declaracao.")
    print()

    if erros:
        return 1
    if pendentes and args.pendente_e_erro:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
