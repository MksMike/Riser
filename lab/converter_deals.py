"""Converte o Relatorio de Historico do MT5 (HTML) no CSV que o baseline le.

    python lab/converter_deals.py --entrada <report.html> --saida <deals.csv> \
        --server-offset-h 3

O relatorio do MT5 vem em UTF-16 e traz varias seccoes. So a de POSICOES
interessa: cada linha e uma posicao, e o primeiro `Horario` e o instante de
ENTRADA. A seccao de Negocios traz o dobro das linhas — abertura e fecho — e
ler a errada faria cada saida entrar como se fosse uma entrada na direcao
oposta, um estudo sobre a contabilidade em vez de sobre o timing.

FUSO: o relatorio esta em hora do SERVIDOR. `server.timezone_offset` continua
VERIFICAR nos manifestos, entao o offset e argumento OBRIGATORIO — adivinhar
deslocaria todas as entradas e o casamento por horario passaria a comparar a
entrada com a hora errada do dia, produzindo um resultado plausivel e errado.

O CSV de saida traz `preco_abertura` alem de `ts_utc,side`. O baseline ignora
colunas a mais; ela existe para conferir o offset contra o tick data.
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

LINHA = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
CELULA = re.compile(r"<t[hd][^>]*>(.*?)</t[hd]>", re.S | re.I)
QUANDO = re.compile(r"^\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}$")


def ler_texto(path: Path) -> str:
    """O relatorio sai em UTF-16. Tentar utf-8 primeiro e barato e evita
    depender de quem gerou ter usado a mesma build."""
    raw = path.read_bytes()
    for enc in ("utf-16", "utf-8-sig", "utf-8", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError(f"{path.name}: nenhum encoding conhecido decodifica o arquivo")


def limpar(celula: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", celula)).replace("\xa0", " ").strip()


def extrair_posicoes(texto: str) -> list[dict]:
    """Linhas da seccao POSICOES.

    Reconhecidas pela forma, nao pela posicao no documento: primeira celula com
    data-hora, terceira com o simbolo, quarta com buy/sell, e a quinta com um
    NUMERO. E a quinta que separa Posicoes de Negocios — la ela e `in`/`out`.
    """
    achadas = []
    for bruta in LINHA.findall(texto):
        cels = [limpar(c) for c in CELULA.findall(bruta)]
        if len(cels) < 6 or not QUANDO.match(cels[0]):
            continue
        tipo = cels[3].lower()
        if tipo not in ("buy", "sell"):
            continue

        # O relatorio intercala celulas ocultas (`class="hidden" colspan=8`)
        # que saem vazias. Indice fixo apanha a oculta em vez do volume, e a
        # linha inteira e descartada em silencio — foi o que aconteceu na
        # primeira tentativa. Procurar os dois primeiros numeros depois do tipo
        # e imune ao numero de colunas escondidas.
        numeros = []
        for c in cels[4:]:
            if not c:
                continue
            try:
                numeros.append(float(c.replace(",", ".")))
            except ValueError:
                break  # 'in'/'out' => e a seccao Negocios, nao Posicoes
            if len(numeros) == 2:
                break
        if len(numeros) < 2:
            continue

        achadas.append({
            "quando_srv": cels[0],
            "symbol": cels[2],
            "side": tipo,
            "volume": f"{numeros[0]:g}",
            "preco": f"{numeros[1]:.3f}",
        })
    return achadas


def converter(entrada: Path, saida: Path, offset_h: float, symbol: str | None) -> dict:
    texto = ler_texto(entrada)
    linhas = extrair_posicoes(texto)
    if not linhas:
        raise SystemExit(
            f"{entrada.name}: nenhuma posicao reconhecida. Confirme que o "
            "relatorio inclui a seccao Posicoes e todo o historico."
        )

    if symbol:
        linhas = [x for x in linhas if x["symbol"] == symbol]
        if not linhas:
            raise SystemExit(f"nenhuma posicao com symbol={symbol!r}")

    desloc = timedelta(hours=offset_h)
    saidas = []
    for x in linhas:
        srv = datetime.strptime(x["quando_srv"], "%Y.%m.%d %H:%M:%S")
        ts = (srv - desloc).replace(tzinfo=timezone.utc)
        saidas.append({
            "ts_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "side": x["side"],
            "symbol": x["symbol"],
            "volume": x["volume"],
            "preco_abertura": x["preco"],
            "ts_servidor": x["quando_srv"],
        })
    saidas.sort(key=lambda r: r["ts_utc"])

    saida.parent.mkdir(parents=True, exist_ok=True)
    with saida.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(saidas[0]))
        w.writeheader()
        w.writerows(saidas)

    por_lado = Counter(r["side"] for r in saidas)
    por_symbol = Counter(r["symbol"] for r in saidas)
    por_mes = Counter(r["ts_utc"][:7] for r in saidas)
    return {
        "linhas": len(saidas),
        "de": saidas[0]["ts_utc"],
        "ate": saidas[-1]["ts_utc"],
        "por_lado": dict(por_lado),
        "por_symbol": dict(por_symbol),
        "por_mes": dict(sorted(por_mes.items())),
        "offset_h": offset_h,
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--entrada", type=Path, required=True)
    p.add_argument("--saida", type=Path, required=True)
    p.add_argument(
        "--server-offset-h", type=float, required=True,
        help="horas do servidor face ao UTC. Obrigatorio: server.timezone_offset "
             "esta VERIFICAR e adivinhar desloca todas as entradas.",
    )
    p.add_argument("--symbol", help="filtra por ativo, ex: XAUUSDm")
    args = p.parse_args(argv)

    r = converter(args.entrada, args.saida, args.server_offset_h, args.symbol)
    print(f"  linhas          : {r['linhas']}")
    print(f"  faixa (UTC)     : {r['de']}  ->  {r['ate']}")
    print(f"  offset aplicado : servidor {r['offset_h']:+.0f}h face ao UTC")
    print(f"  por lado        : {r['por_lado']}")
    print(f"  por ativo       : {r['por_symbol']}")
    print("  por mes         :")
    for m, n in r["por_mes"].items():
        print(f"      {m}  {n:>5}  {'#' * min(60, n)}")
    print(f"  gravado em      : {args.saida}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
