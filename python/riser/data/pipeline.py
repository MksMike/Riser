"""Entrada de linha de comando do pipeline de dados. Uma etapa por vez.

    python -m riser.data.pipeline download  --instrumento XAUUSD --de 2024-08 --ate 2026-07
    python -m riser.data.pipeline parse     --instrumento XAUUSD --de 2026-07 --ate 2026-07
    python -m riser.data.pipeline aggregate --instrumento XAUUSD --de 2026-07 --ate 2026-07
    python -m riser.data.pipeline validate  --instrumento XAUUSD --de 2026-07 --ate 2026-07

Script descartavel serve para 744 arquivos horarios. Nao serve para 17 mil:
uma corrida de
dois anos atravessa reinicio de PC, queda de rede e possivelmente troca de
maquina, e nada disso pode custar o que ja foi baixado.

--------------------------------------------------------------- as quatro etapas

    download    .bi5 bruto        -> RISER-data/raw/
    parse       .bi5              -> RISER-data/ticks/     + relatorio descritivo
    aggregate   ticks             -> RISER-data/bars/
    validate    barras agregadas  x  M1 publicado pela Dukascopy

As etapas NAO se encadeiam. Rodar `download` de dois anos e depois decidir o que
parsear e um fluxo legitimo e e o esperado — encadear parse e agregacao dentro do
download tiraria essa decisao de quem a deve tomar, e faria uma falha de
agregacao interromper um download de horas.

`validate` existe apesar de nao ter sido pedida entre as etapas: sem ela ha dado
no disco e nenhuma evidencia de que a agregacao esta certa, e e justamente a
pergunta que decide se vale a pena acumular dois anos em cima.

--------------------------------------------------------------------- retomada

A retomada se decide pelo ARTEFATO no disco, nunca por arquivo de estado.

Arquivo de estado e uma segunda verdade: quando ele diz "mes feito" e o Parquet
nao esta la — porque alguem apagou, porque o disco encheu, porque a escrita foi
interrompida — a retomada pula um mes que nao existe, e a falta so aparece na
analise. O artefato e a unica coisa que nao mente sobre si mesma.

O arquivo de progresso existe, mas so para ser LIDO de fora enquanto a corrida
anda. Ele nunca e consultado para decidir o que fazer.

------------------------------------------------------------------ interrupcao

Ctrl+C marca uma bandeira e a etapa termina a unidade em curso — um arquivo
horario, no download; um mes, nas demais. Segundo Ctrl+C sai imediatamente.

A gravacao ja e atomica em todas as etapas (`write_atomic`, `write_month`,
`write_bars` escrevem em temporario e renomeiam), entao um corte no meio deixa o
temporario para tras e nunca um Parquet truncado. O tratamento de sinal existe
para o outro lado: sair no meio de um mes deixaria trabalho pela metade que a
retomada nao veria, porque a retomada olha o artefato final.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from riser.core.log import JsonlLogger
from riser.core.paths import data_root
from riser.data.bars import TIMEFRAMES, aggregate_month, bars_path, write_bars
from riser.data.dukascopy import (
    ABSENT_SUFFIX,
    CONFIG_PATH,
    FEED,
    FeedConfig,
    download_month,
    estimativa_restante,
    make_logger,
    raw_path,
)
from riser.data.ticks import (
    InstrumentSpec,
    ingest_month,
    parquet_path,
    read_month_with_overlap,
    summarize,
)

ETAPAS = ("download", "completude", "parse", "aggregate", "validate")

# Teto de passadas do laco de convergencia do download. Existe para que o laco
# nao vire uma forma elegante de nunca terminar quando a rede esta ruim.
MAX_PASSADAS = 4

# Limiares de lacuna, em segundos. Escolhidos pelo que cada um significa, nao
# por serem redondos: 5 min separa o silencio de sessao asiatica do resto; 1h
# nao acontece em mercado aberto; 4h so cabe em pausa diaria ou fim de semana.
LACUNAS_S = (60, 300, 3600, 14400)


# ------------------------------------------------------------------- meses


def meses_no_intervalo(de: str, ate: str) -> list[tuple[int, int]]:
    """['2024-08', '2024-10'] -> [(2024,8), (2024,9), (2024,10)]. Inclusivo."""
    try:
        ini = datetime.strptime(de, "%Y-%m")
        fim = datetime.strptime(ate, "%Y-%m")
    except ValueError as exc:
        raise SystemExit(f"data invalida ({exc}). Formato esperado: AAAA-MM.") from exc
    if fim < ini:
        raise SystemExit(f"--ate ({ate}) e anterior a --de ({de}).")

    out: list[tuple[int, int]] = []
    ano, mes = ini.year, ini.month
    while (ano, mes) <= (fim.year, fim.month):
        out.append((ano, mes))
        ano, mes = (ano + 1, 1) if mes == 12 else (ano, mes + 1)
    return out


# -------------------------------------------------------------- interrupcao


class Interrupcao:
    """Ctrl+C pede parada; a unidade em curso termina antes de sair.

    Matar no meio de um mes nao corrompe arquivo — a gravacao e atomica — mas
    deixa trabalho pela metade que a retomada nao enxerga, porque ela olha o
    artefato final. Terminar a unidade e o que torna a retomada exata.
    """

    def __init__(self) -> None:
        self.pedida = False
        self._anterior = None

    def __enter__(self) -> "Interrupcao":
        def handler(signum, frame):  # noqa: ARG001
            if self.pedida:
                print("\nsegundo Ctrl+C: saindo agora.", flush=True)
                raise KeyboardInterrupt
            self.pedida = True
            print(
                "\nCtrl+C recebido. Terminando a unidade em curso e parando. "
                "Ctrl+C de novo para sair imediatamente.",
                flush=True,
            )

        try:
            self._anterior = signal.signal(signal.SIGINT, handler)
        except ValueError:
            # Fora da thread principal (teste, notebook). Sem handler, o
            # comportamento padrao ja e aceitavel por causa da escrita atomica.
            self._anterior = None
        return self

    def __exit__(self, *exc: object) -> None:
        if self._anterior is not None:
            signal.signal(signal.SIGINT, self._anterior)


# ---------------------------------------------------------------- progresso


class TravaOcupada(RuntimeError):
    """Ja ha uma corrida VIVA sobre este instrumento e etapa."""


def _processo_vivo(pid: int) -> bool:
    """O processo existe agora? FATO, nao palpite.

    NAO usa `os.kill(pid, 0)`. Esse e o idioma de POSIX; no Windows, o
    `os.kill` da CPython abre o processo e chama `TerminateProcess(handle, sig)`
    para qualquer sinal que nao seja CTRL_C_EVENT ou CTRL_BREAK_EVENT — ou seja,
    a chamada que deveria apenas perguntar pode MATAR. Num teste controlado aqui
    a cobaia sobreviveu, o que torna o comportamento ambiguo entre versoes; e
    ambiguidade e motivo bastante para nao usar a chamada num codigo cuja
    unica funcao e proteger uma corrida de dois anos.

    `OpenProcess` + `WaitForSingleObject(0)` responde sem tocar no processo.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x102
        k = ctypes.windll.kernel32  # type: ignore[attr-defined]
        h = k.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
        if not h:
            return False
        try:
            return k.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
        finally:
            k.CloseHandle(h)
    try:
        os.kill(pid, 0)          # em POSIX isto so consulta
    except ProcessLookupError:
        return False
    except PermissionError:
        return True              # existe, e de outro usuario
    return True


def _boot_utc() -> datetime | None:
    """Instante em que a maquina ligou, ou None se nao der para saber."""
    if sys.platform != "win32":
        return None
    try:
        ms = ctypes.windll.kernel32.GetTickCount64()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return None
    return datetime.now(timezone.utc) - timedelta(milliseconds=ms)


def _trava_orfa(dono: dict) -> str | None:
    """Motivo pelo qual a trava e orfa, ou None se o dono ainda esta vivo.

    Duas razoes, e as duas sao FATO verificavel — nunca idade do arquivo. PID
    morto e fato; "esta parado ha muito tempo" e palpite, e um palpite errado
    mataria uma corrida de dois anos que so estava lenta.
    """
    pid = dono.get("pid")
    if not isinstance(pid, int):
        return "a trava nao registra PID"

    if not _processo_vivo(pid):
        return f"o processo {pid} nao existe mais"

    # PID vivo, mas pode ser OUTRO processo que herdou o numero depois de um
    # reinicio. Um processo nao pode ter comecado antes da maquina ligar.
    boot = _boot_utc()
    inicio = dono.get("inicio_utc")
    if boot is not None and isinstance(inicio, str):
        try:
            quando = datetime.fromisoformat(inicio)
        except ValueError:
            return None
        if quando.tzinfo is None:
            quando = quando.replace(tzinfo=timezone.utc)
        if quando < boot:
            return (f"a trava e de antes do ultimo boot ({boot:%Y-%m-%d %H:%M} UTC); "
                    f"o PID {pid} foi reaproveitado por outro processo")
    return None


class Trava:
    """Uma corrida por instrumento e etapa. Duas ao mesmo tempo se sabotam.

    Nao e precaucao teorica. Duas corridas coexistiram neste projeto por horas:
    uma de uma sessao anterior e outra nova, sobre o mesmo diretorio. O efeito
    imediato foi um contador que parecia errado — a segunda via como "ja tinha"
    o que a primeira acabara de escrever — e o efeito caro foi outro: o proprio
    `config/feeds/dukascopy.yaml` registra que o limite da Dukascopy e por
    CONEXAO CONCORRENTE, e que quatro conexoes derrubaram a taxa de sucesso para
    um oitavo. Duas corridas nao dobram a velocidade: fazem as duas falharem
    mais e o servidor punir as duas.

    Trava ocupada NAO e resolvida por heuristica de idade. O processo pode estar
    vivo e so lento, e matar uma corrida de dois anos por um palpite de timeout
    seria pior que o problema. Para e pede, com o PID na mao — a mesma regra do
    compilador ambiguo (CLAUDE.md, Higiene de ferramenta).

    Mas trava ORFA e assumida sozinha, sem perguntar, porque a orfandade se
    verifica: PID morto e fato. Sem isso, um corte de energia no meio da corrida
    de dois anos deixaria um arquivo apontando para um processo inexistente, e a
    retomada recusaria — descoberto as duas da manha, depois do reinicio, que e
    exatamente quando ninguem quer depurar arquivo de trava.
    """

    def __init__(self, instrumento: str, etapa: str, *, root: Path | None = None) -> None:
        base = (root or data_root()) / "state" / instrumento
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / f"{etapa}.lock"
        self.etapa = etapa
        self.orfa_assumida: str | None = None

    def __enter__(self) -> "Trava":
        conteudo = json.dumps({
            "pid": os.getpid(),
            "etapa": self.etapa,
            "inicio_utc": datetime.now(timezone.utc).isoformat(),
        })
        try:
            # 'x' e atomico no nivel do sistema de arquivos: quem cria, ganha.
            with open(self.path, "x", encoding="utf-8") as fh:
                fh.write(conteudo)
            return self
        except FileExistsError:
            pass

        try:
            dono = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            dono = {}

        motivo = _trava_orfa(dono)
        if motivo is None:
            raise TravaOcupada(
                f"ja ha uma corrida VIVA de '{self.etapa}' neste instrumento.\n"
                f"   dono: pid {dono.get('pid', '?')}, desde {dono.get('inicio_utc', '?')}\n"
                f"   trava: {self.path}\n\n"
                "   Duas corridas sobre o mesmo diretorio se sabotam: a Dukascopy\n"
                "   limita por conexao concorrente, e as duas passam a falhar mais.\n"
                "   Espere a outra terminar, ou pare o processo acima."
            ) from None

        # Orfa: assume, e diz em voz alta. Assumir em silencio esconderia que
        # uma corrida anterior morreu no meio, que e informacao.
        print(f"    trava orfa assumida: {motivo}", flush=True)
        self.path.write_text(conteudo, encoding="utf-8")
        self.orfa_assumida = motivo
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.path.unlink()
        except OSError:
            pass


class Progresso:
    """Arquivo de progresso legivel de fora. NUNCA consultado para decidir.

    Serve para acompanhar uma corrida de horas de outro terminal, ou para saber
    onde ela parou depois de uma queda. Se ele divergir da realidade, quem manda
    e o disco.
    """

    def __init__(self, instrumento: str, etapa: str, total: int, *, root: Path | None = None) -> None:
        base = (root or data_root()) / "state" / instrumento
        base.mkdir(parents=True, exist_ok=True)
        self.path = base / f"{etapa}.json"
        self.dados = {
            "instrumento": instrumento,
            "etapa": etapa,
            "pid": os.getpid(),
            "inicio_utc": datetime.now(timezone.utc).isoformat(),
            "total_meses": total,
            "concluidos": 0,
            "mes_atual": None,
            "encerrado": False,
            "interrompido": False,
            "meses": {},
        }
        self.gravar()

    def gravar(self) -> None:
        """Temporario + rename: leitor de fora nunca ve JSON pela metade."""
        self.dados["atualizado_utc"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".json.part")
        tmp.write_text(
            json.dumps(self.dados, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        tmp.replace(self.path)

    def comecou(self, ano: int, mes: int) -> None:
        self.dados["mes_atual"] = f"{ano:04d}-{mes:02d}"
        self.gravar()

    def terminou(self, ano: int, mes: int, resultado: dict) -> None:
        self.dados["meses"][f"{ano:04d}-{mes:02d}"] = resultado
        self.dados["concluidos"] += 1
        self.dados["mes_atual"] = None
        self.gravar()

    def fim(self, *, interrompido: bool) -> None:
        self.dados["encerrado"] = True
        self.dados["interrompido"] = interrompido
        self.gravar()


# ------------------------------------------------------------------ lacunas


def analisar_lacunas(df: pd.DataFrame, *, top: int = 10) -> dict:
    """Silencio entre ticks consecutivos.

    Lacuna nao e sinonimo de dado faltando: a pausa diaria e o fim de semana
    produzem silencio esperado, e a ADR 0008 trata de nao confundir os dois.
    Aqui so se MEDE — a leitura de qual e qual vem depois, com o horario do
    servidor em maos.
    """
    if len(df) < 2:
        return {"n_ticks": int(len(df)), "sem_dados": True}

    ts = pd.DatetimeIndex(df["ts_utc"]).tz_convert("UTC")
    delta = ts.to_series().diff().dropna()
    seg = delta.dt.total_seconds()

    maiores = seg.nlargest(top)
    return {
        "n_ticks": int(len(df)),
        "intervalo_s": {
            "p50": float(seg.quantile(0.50)),
            "p95": float(seg.quantile(0.95)),
            "p99": float(seg.quantile(0.99)),
            "max": float(seg.max()),
        },
        "acima_de": {
            f"{lim}s": int((seg > lim).sum()) for lim in LACUNAS_S
        },
        "maiores": [
            {
                "fim_utc": ts_i.isoformat(),
                "duracao_s": float(v),
                "duracao_h": round(float(v) / 3600.0, 2),
            }
            for ts_i, v in maiores.items()
        ],
    }


# ---------------------------------------------------------------- relatorio


def gravar_relatorio(
    instrumento: str, ano: int, mes: int, payload: dict, log: JsonlLogger,
    *, root: Path | None = None, sufixo: str = "parse",
) -> Path:
    """Relatorio em arquivo proprio, com o envelope completo de log.

    Console e efemero. Estas descritivas sao a primeira medicao real do projeto
    e vao ser comparadas com outros meses e outras corretoras — sem `run_id`,
    `build_hash` e `config_hash` junto, comparar duas delas seria comparar duas
    coisas de origem desconhecida (invariante 6).
    """
    base = (root or data_root()) / "reports" / instrumento
    base.mkdir(parents=True, exist_ok=True)
    dest = base / f"{ano:04d}-{mes:02d}-{sufixo}.json"

    doc = log.envelope("info")
    doc.update(payload)

    tmp = dest.with_suffix(".json.part")
    tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)
    return dest


# ------------------------------------------------------------------- etapas


def completude(instrumento: str, ano: int, mes: int, *, root: Path | None = None) -> dict:
    """Arquivo horario a arquivo horario, em QUATRO estados. So um pede acao.

        presente   .bi5 com conteudo
        vazio      .bi5 de zero byte: o servidor respondeu 200 sem tick nenhum.
                   E dado, nao falta — mercado aberto sem negocio, ou fechado.
        ausente    marcador .absent: o servidor devolveu 404. Nunca vai chegar.
        falhou     nem arquivo nem marcador: as tentativas se esgotaram, ou o
                   arquivo nunca foi pedido. E o UNICO que pede nova passada.

    Somar 'vazio' e 'ausente' em "faltando" produziria um mes eternamente
    incompleto — fim de semana e feriado nunca teriam dado — e a corrida de dois
    anos nunca convergiria.

    VOCABULARIO: a unidade aqui e o ARQUIVO HORARIO, nao "hora". Num relatorio
    que tambem fala de tempo de execucao, "faltam 385 horas" se le como duracao
    e o erro passa despercebido. "hora" fica reservada para duracao.
    """
    inicio = datetime(ano, mes, 1, tzinfo=timezone.utc)
    fim = (
        datetime(ano + 1, 1, 1, tzinfo=timezone.utc)
        if mes == 12
        else datetime(ano, mes + 1, 1, tzinfo=timezone.utc)
    )

    por_dia: dict[str, dict[str, int]] = {}
    total = {"presente": 0, "vazio": 0, "ausente": 0, "falhou": 0}
    faltando: list[str] = []

    marca = inicio
    while marca < fim:
        p = raw_path(instrumento, marca, root)
        marcador = p.with_name(p.name + ABSENT_SUFFIX)
        if p.exists():
            estado = "presente" if p.stat().st_size > 0 else "vazio"
        elif marcador.exists():
            estado = "ausente"
        else:
            estado = "falhou"
            faltando.append(marca.isoformat())

        dia = f"{marca:%Y-%m-%d}"
        por_dia.setdefault(dia, {"presente": 0, "vazio": 0, "ausente": 0, "falhou": 0})
        por_dia[dia][estado] += 1
        total[estado] += 1
        marca += timedelta(hours=1)

    arquivos = sum(total.values())
    return {
        "instrumento": instrumento,
        "ano": ano,
        "mes": mes,
        "unidade": "arquivo_horario",
        "arquivos_horarios_no_mes": arquivos,
        "total": total,
        # Resolvido = tudo que nao pede acao. E este o numero que decide se o
        # mes serve, nao "presente / total".
        "arquivos_resolvidos": arquivos - total["falhou"],
        "completo": total["falhou"] == 0,
        "por_dia": por_dia,
        "arquivos_faltando": faltando[:200],
        "arquivos_faltando_total": len(faltando),
    }


def etapa_completude(instrumento: str, ano: int, mes: int, log: JsonlLogger, *, force: bool) -> dict:
    """Grava o relatorio de completude. Sem isto, 'quais meses estao
    incompletos' exigiria reprocessar dois anos para descobrir."""
    r = completude(instrumento, ano, mes)
    dest = gravar_relatorio(
        instrumento, ano, mes,
        {"event": "completude", "feed": FEED, **r},
        log, sufixo="completude",
    )
    log.info(event="completude", instrumento=instrumento, ano=ano, mes=mes,
             completo=r["completo"], arquivos_falhando=r["total"]["falhou"],
             arquivo=str(dest))
    return {"estado": "medido", "completo": r["completo"],
            "total": r["total"], "relatorio": str(dest)}


def etapa_download(instrumento: str, ano: int, mes: int, log: JsonlLogger, *, force: bool) -> dict:
    """Baixa o mes ate CONVERGIR, nao uma vez.

    Uma passada pode nao fechar um mes: arquivo horario que esgota as tentativas
    por rede nao deixa .bi5 nem marcador, e fica como buraco silencioso. "Rodar
    de novo ate parar de mudar" nao pode ser procedimento manual quando sao dois
    anos.

    QUANTOS buracos esperar ainda nao se sabe. A primeira estimativa saiu de uma
    corrida contaminada — dois downloaders concorrentes sobre o mesmo diretorio,
    condicao que o proprio config/feeds/dukascopy.yaml documenta como capaz de
    derrubar o sucesso para um oitavo. Os 503 e timeouts observados falam dessa
    condicao, nao do regime serial limpo. A taxa real esta por medir; ate la, o
    laco existe porque a falha e POSSIVEL, e nao porque se saiba que e frequente.

    Tres desfechos de parada, e a diferenca entre eles importa:

        convergiu   nenhum arquivo horario falhou por rede. O mes esta resolvido
                    — o que sobra e ausencia confirmada, e essa nao muda.
        estagnou    a passada nao resolveu NADA e ainda ha falha. Insistir so
                    repete o mesmo erro; o problema esta na rede ou no servidor,
                    nao na quantidade de tentativas.
        teto        acabaram as passadas. Reportado como incompleto, de
                    proposito: um laco sem teto e um jeito de nunca terminar.
    """
    historico: list[dict] = []
    motivo = "teto"

    intervalo = FeedConfig.load().min_interval_s
    for passada in range(1, MAX_PASSADAS + 1):
        antes = completude(instrumento, ano, mes)["total"]["falhou"]
        print(
            f"    passada {passada}/{MAX_PASSADAS}  faltam {antes} arquivo(s) "
            f"horario(s), ~{estimativa_restante(antes, intervalo)} no piso",
            flush=True,
        )
        if antes == 0 and passada > 1:
            motivo = "convergiu"
            break

        t = download_month(instrumento, ano, mes, logger=log, progress=True)
        depois = completude(instrumento, ano, mes)["total"]["falhou"]
        resolvidas = antes - depois
        historico.append({"passada": passada, "tally": t,
                          "pendentes_antes": antes, "pendentes_depois": depois})
        log.info(event="download_passada", instrumento=instrumento, ano=ano, mes=mes,
                 passada=passada, resolvidas=resolvidas, pendentes=depois, **t)

        if depois == 0:
            motivo = "convergiu"
            break
        if resolvidas == 0:
            motivo = "estagnou"
            break

    r = completude(instrumento, ano, mes)
    dest = gravar_relatorio(
        instrumento, ano, mes,
        {"event": "completude", "feed": FEED, "motivo_parada": motivo,
         "passadas": historico, **r},
        log, sufixo="completude",
    )
    return {"estado": motivo, "completo": r["completo"], "total": r["total"],
            "passadas": len(historico), "relatorio": str(dest)}


def etapa_parse(instrumento: str, ano: int, mes: int, log: JsonlLogger, *, force: bool) -> dict:
    p = parquet_path(instrumento, ano, mes)
    if p.exists() and not force:
        return {"estado": "ja_existe", "arquivo": str(p)}

    caminho, n = ingest_month(instrumento, ano, mes, logger=log, strict=True)

    # Sobreposicao para que a ultima barra do mes tenha tick que a feche; as
    # descritivas saem do mes puro, sem ela, para nao contar tick de julho como
    # se fosse de junho.
    puro = read_month_with_overlap(instrumento, ano, mes, overlap_h=0.0)
    desc = summarize(puro)
    desc["lacunas"] = analisar_lacunas(puro)

    payload = {
        "event": "parse_report",
        "instrumento": instrumento,
        "ano": ano,
        "mes": mes,
        "feed": FEED,
        "arquivo": str(caminho),
        "descritivas": desc,
    }
    rel = gravar_relatorio(instrumento, ano, mes, payload, log)
    log.info(event="parse_report_gravado", arquivo=str(rel), ticks=n)
    return {"estado": "parseado", "ticks": n, "arquivo": str(caminho), "relatorio": str(rel)}


def etapa_aggregate(instrumento: str, ano: int, mes: int, log: JsonlLogger, *, force: bool) -> dict:
    faltando = [
        tf for tf in TIMEFRAMES
        if not bars_path(instrumento, tf, ano, mes).exists()
    ]
    if not faltando and not force:
        return {"estado": "ja_existe"}

    df = read_month_with_overlap(instrumento, ano, mes)
    if df.empty:
        return {"estado": "sem_ticks"}

    saida: dict[str, int] = {}
    for tf, seg in TIMEFRAMES.items():
        bars = aggregate_month(df, seg, ano, mes)
        write_bars(bars, instrumento, tf, ano, mes)
        saida[tf] = int(len(bars))
    log.info(event="aggregate", instrumento=instrumento, ano=ano, mes=mes, **saida)
    return {"estado": "agregado", "barras": saida}


def etapa_validate(instrumento: str, ano: int, mes: int, log: JsonlLogger, *, force: bool) -> dict:
    """Compara o M1 agregado com o M1 publicado pela Dukascopy.

    Passa pelo portao de `exigir_comparacao()`: divergencia zero sem rotulo em
    comum nao e aprovacao, e sim nada verificado.
    """
    from riser.harness.dukascopy_reference import (
        compare_ohlc,
        decode_candles,
        download_reference_m1,
        top_divergences,
    )

    ref_path = download_reference_m1(instrumento, ano, mes)
    if ref_path is None:
        log.warn(event="validate_sem_referencia", instrumento=instrumento, ano=ano, mes=mes)
        return {"estado": "sem_referencia"}

    spec = InstrumentSpec.load(instrumento)
    referencia = decode_candles(ref_path.read_bytes(), ano, mes, spec)

    nossas_path = bars_path(instrumento, "M1", ano, mes)
    if not nossas_path.exists():
        return {"estado": "sem_barras", "esperado": str(nossas_path)}
    nossas = pd.read_parquet(nossas_path)

    diff = compare_ohlc(nossas, referencia)
    # Levanta se nao houve rotulo em comum. Deixar passar seria reportar
    # "divergencia 0.0" para uma comparacao que nao aconteceu.
    diff.exigir_comparacao()

    piores = top_divergences(nossas, referencia, n=5)
    resultado = {
        "estado": "comparado",
        "comuns": diff.comuns,
        "so_nossas": diff.so_nossas,
        "so_referencia": diff.so_referencia,
        "max_abs_usd_oz": diff.max_abs,
        "max_geral_usd_oz": diff.max_geral,
        "pior": diff.pior,
        "top5": [
            {"ts_utc": i.isoformat(), **{k: float(v) for k, v in linha.items()}}
            for i, linha in piores.iterrows()
        ] if len(piores) else [],
    }
    log.info(event="validate", instrumento=instrumento, ano=ano, mes=mes,
             comuns=diff.comuns, max_geral=diff.max_geral)
    return resultado


ETAPA_FN = {
    "download": etapa_download,
    "completude": etapa_completude,
    "parse": etapa_parse,
    "aggregate": etapa_aggregate,
    "validate": etapa_validate,
}


# -------------------------------------------------------------------- runner


def rodar(
    etapa: str,
    instrumento: str,
    meses: list[tuple[int, int]],
    *,
    force: bool = False,
    log: JsonlLogger | None = None,
) -> dict:
    fn = ETAPA_FN[etapa]
    log = log or make_logger()
    prog = Progresso(instrumento, etapa, len(meses))
    t0 = time.time()
    interrompido = False

    log.info(event="pipeline_start", etapa=etapa, instrumento=instrumento,
             meses=len(meses), de=f"{meses[0][0]:04d}-{meses[0][1]:02d}",
             ate=f"{meses[-1][0]:04d}-{meses[-1][1]:02d}", force=force)

    with Trava(instrumento, etapa), Interrupcao() as sinal:
        for n, (ano, mes) in enumerate(meses, 1):
            if sinal.pedida:
                interrompido = True
                break
            rotulo = f"{ano:04d}-{mes:02d}"
            print(f"\n[{n}/{len(meses)}] {etapa} {instrumento} {rotulo}", flush=True)
            prog.comecou(ano, mes)
            try:
                r = fn(instrumento, ano, mes, log, force=force)
            except Exception as exc:  # noqa: BLE001
                # Um mes que falha nao custa os outros; o desfecho fica gravado
                # no progresso e o codigo de saida final denuncia.
                r = {"estado": "erro", "erro": f"{type(exc).__name__}: {exc}"}
                log.error("E5005", event="pipeline_month_fail", etapa=etapa,
                          instrumento=instrumento, ano=ano, mes=mes, msg=str(exc))
            print(f"    {r}", flush=True)
            prog.terminou(ano, mes, r)

    prog.fim(interrompido=interrompido)
    dt = time.time() - t0
    log.info(event="pipeline_done", etapa=etapa, instrumento=instrumento,
             concluidos=prog.dados["concluidos"], interrompido=interrompido,
             segundos=round(dt, 1))

    erros = [m for m, r in prog.dados["meses"].items() if r.get("estado") == "erro"]
    print(f"\n{etapa}: {prog.dados['concluidos']}/{len(meses)} mes(es) em {dt:.0f}s"
          f"{'  INTERROMPIDO' if interrompido else ''}", flush=True)
    if erros:
        print(f"  meses com erro: {', '.join(erros)}", flush=True)
    print(f"  progresso: {prog.path}", flush=True)

    return {"concluidos": prog.dados["concluidos"], "erros": erros,
            "interrompido": interrompido, "meses": prog.dados["meses"]}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m riser.data.pipeline",
        description="Pipeline de dados, uma etapa por vez. As etapas nao se encadeiam.",
    )
    p.add_argument("etapa", choices=ETAPAS)
    p.add_argument("--instrumento", required=True,
                   help="chave do instrumento em config/feeds/dukascopy.yaml")
    p.add_argument("--de", required=True, metavar="AAAA-MM")
    p.add_argument("--ate", required=True, metavar="AAAA-MM")
    p.add_argument("--force", action="store_true",
                   help="refaz mes cujo artefato ja existe (nao vale para download, "
                        "que sempre pula arquivo horario ja baixado)")
    args = p.parse_args(argv)

    meses = meses_no_intervalo(args.de, args.ate)
    log = make_logger()
    log.boot(comando=" ".join(argv or sys.argv[1:]), config=str(CONFIG_PATH))

    try:
        r = rodar(args.etapa, args.instrumento, meses, force=args.force, log=log)
    except TravaOcupada as exc:
        # Problema operacional nao merece rastro de excecao: a linha acionavel
        # tem de ser a primeira que se le.
        print(f"\n{exc}\n", file=sys.stderr)
        log.error("E5006", event="trava_ocupada", etapa=args.etapa,
                  instrumento=args.instrumento, msg=str(exc))
        log.close()
        return 2
    log.close()

    if r["erros"]:
        return 1
    if r["interrompido"]:
        return 130  # convencao de SIGINT
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
