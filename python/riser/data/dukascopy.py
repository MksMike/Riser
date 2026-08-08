"""Downloader de ticks brutos da Dukascopy.

Este modulo NAO parseia nada. Ele busca o `.bi5` de cada arquivo horario e o
guarda intacto. A separacao e deliberada: baixar uma vez, parsear muitas. Um bug
no parser custa um reprocessamento local; um bruto apagado custa uma refeitura
completa do download, e arquivo antigo sai do ar sem aviso.

VOCABULARIO: a unidade de particionamento do feed e o ARQUIVO HORARIO. "hora"
fica reservada para duracao — num relatorio que fala das duas coisas, "faltam
385 horas" se le como tempo de execucao e o engano passa despercebido.

Ordem cronologica reversa — mes mais recente primeiro. O dado recente e o que
descreve o regime atual do mercado, e um download de dois anos interrompido no
meio deixa o periodo util pronto em vez de deixar 2024 completo e 2026 vazio.

Unidades: este modulo nao interpreta preco. Nao ha ponto nem lote aqui, e nao
pode haver — o `.bi5` guarda inteiros cuja escala e propriedade do instrumento,
resolvida em `ticks.py` a partir de `config/feeds/dukascopy.yaml`.
"""

from __future__ import annotations

import http.client
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

import yaml

from riser.core.log import JsonlLogger
from riser.core.paths import raw_dir, repo_root

FEED = "dukascopy"
CONFIG_PATH = repo_root() / "config" / "feeds" / "dukascopy.yaml"

# Marcador de arquivo horario que o servidor declara inexistente. Zero bytes,
# ao lado do lugar onde o .bi5 estaria. Sem ele, uma retomada volta a pedir os
# mesmos ausentes para sempre.
#
# NAO se usa um .bi5 vazio para isso: 200 com zero bytes e uma resposta
# legitima e significa "arquivo horario sem tick", que e dado, nao ausencia.
# MEDIDO: neste feed o 200 vazio e a regra e o 404 quase nao aparece. Ver a
# contagem de julho em config/feeds/dukascopy.yaml.
ABSENT_SUFFIX = ".absent"


class DukascopyError(RuntimeError):
    """Falha de download que sobreviveu a todas as tentativas."""


def make_logger(**kwargs: object) -> JsonlLogger:
    """Logger com o contexto certo para este feed.

    `broker_id=None` de proposito: a Dukascopy e feed de referencia, sem
    execucao, sem custo e sem conta. Poe-la em `broker_id` faria uma agregacao
    por corretora, para comparar custo, incluir algo que nunca executou nada.

    Existe como funcao para que o uso correto seja o caminho facil — quem
    instanciar o JsonlLogger a mao pode passar o campo errado sem que nada
    reclame.
    """
    return JsonlLogger(
        "dukascopy",
        alias=FEED,
        feed_id=FEED,
        broker_id=None,
        account_hash=None,
        config_paths=(CONFIG_PATH,),
        **kwargs,  # type: ignore[arg-type]
    )


@dataclass(frozen=True)
class FeedConfig:
    base_url: str
    path_template: str
    min_interval_s: float
    max_retries: int
    backoff_base_s: float
    backoff_max_s: float
    timeout_s: float
    workers: int
    history_starts: dict[str, datetime]

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "FeedConfig":
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        pol = raw["politeness"]
        starts: dict[str, datetime] = {}
        for name, spec in (raw.get("instruments") or {}).items():
            if spec.get("history_starts"):
                starts[name] = datetime.fromisoformat(
                    str(spec["history_starts"])
                ).replace(tzinfo=timezone.utc)
        return cls(
            base_url=raw["source"]["base_url"].rstrip("/"),
            path_template=raw["source"]["path_template"],
            min_interval_s=float(pol["min_interval_s"]),
            max_retries=int(pol["max_retries"]),
            backoff_base_s=float(pol["backoff_base_s"]),
            backoff_max_s=float(pol["backoff_max_s"]),
            timeout_s=float(pol["timeout_s"]),
            workers=int(pol.get("workers", 1)),
            history_starts=starts,
        )


# --------------------------------------------------------------------- URLs


def hour_url(cfg: FeedConfig, instrument: str, hour: datetime) -> str:
    """Monta a URL de um arquivo horario.

    ARMADILHA: o caminho da Dukascopy usa o mes com base ZERO. Janeiro e `00`,
    dezembro e `11`. A conversao acontece so aqui, num lugar unico e testado —
    espalha-la pelo codigo produziria um erro de um mes que nao levanta excecao
    nenhuma, apenas devolve o mes errado.
    """
    if hour.tzinfo is None:
        raise ValueError("hour precisa ser timezone-aware em UTC")
    h = hour.astimezone(timezone.utc)
    path = cfg.path_template.format(
        instrument=instrument,
        year=h.year,
        month0=h.month - 1,
        day=h.day,
        hour=h.hour,
    )
    return f"{cfg.base_url}/{path}"


def raw_path(instrument: str, hour: datetime, root: Path | None = None) -> Path:
    """Caminho local do bruto.

    Guardado com mes com base UM, ao contrario da URL. A leitura humana de um
    diretorio de dados e frequente e `2026/07/` significar agosto seria uma
    fonte permanente de erro de interpretacao.
    """
    h = hour.astimezone(timezone.utc)
    base = root if root is not None else raw_dir(FEED)
    return (
        base / instrument / f"{h.year:04d}" / f"{h.month:02d}" / f"{h.day:02d}"
        / f"{h.hour:02d}h_ticks.bi5"
    )


def formatar_duracao(segundos: float) -> str:
    """Duracao legivel: 45s, 26min, 2h10, 3d04h.

    Existe porque "faltam 385" e um numero sem escala. Um relatorio que diz
    "385 arquivos horarios restantes, ~26min" responde de imediato o que um
    numero cru obriga a calcular na cabeca — e na corrida de dois anos essa
    conta aparece em todo relatorio.
    """
    s = max(0.0, float(segundos))
    if s < 90:
        return f"{s:.0f}s"
    if s < 90 * 60:
        return f"{s / 60.0:.0f}min"
    # Arredonda o resto em vez de truncar: 19h29,6 exibido como 19h29 sugere
    # precisao que a estimativa nao tem, e erra para menos justo onde o numero
    # serve para dimensionar.
    if s < 48 * 3600:
        h, resto = divmod(int(round(s / 60.0)), 60)
        return f"{h}h{resto:02d}"
    d, resto = divmod(int(round(s / 3600.0)), 24)
    return f"{d}d{resto:02d}h"


def estimativa_restante(arquivos_pendentes: int, intervalo_s: float) -> str:
    """Piso de duracao: arquivos que ainda precisam de rede x intervalo configurado.

    E PISO, nao previsao. Ignora tempo de resposta do servidor e retentativas —
    a medicao de campo registrada no `dukascopy.yaml` mostrou tempo por
    requisicao acima do intervalo puro. Serve para dimensionar, nao para
    prometer.
    """
    return formatar_duracao(arquivos_pendentes * intervalo_s)


def hours_reverse(start: datetime, end: datetime) -> Iterator[datetime]:
    """Arquivos horarios de [start, end), do MAIS RECENTE para o mais antigo.

    Rende o instante que identifica cada arquivo horario do feed — a unidade de
    particionamento, nao uma medida de duracao.

    Semiaberto no fim: `end` exclusivo evita baixar o arquivo da hora corrente,
    que ainda esta a ser escrito pelo servidor e devolveria conteudo parcial que
    a retomada trataria como completo.
    """
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start e end precisam ser timezone-aware em UTC")
    cur = end.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    floor = start.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    if cur >= end.astimezone(timezone.utc):
        cur -= timedelta(hours=1)
    while cur >= floor:
        yield cur
        cur -= timedelta(hours=1)


def month_start(dt: datetime) -> datetime:
    d = dt.astimezone(timezone.utc)
    return d.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def last_complete_month(now: datetime) -> tuple[datetime, datetime]:
    """[inicio, fim) do ultimo mes inteiro terminado antes de `now`.

    "Completo" importa: o mes corrente tem arquivos que ainda nao existem, e uma
    distribuicao de spread calculada sobre mes parcial nao e comparavel com as
    dos meses cheios.
    """
    this_month = month_start(now)
    end = this_month
    start = month_start(end - timedelta(days=1))
    return start, end


# ----------------------------------------------------------------- download


USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) RISER/0.1 (pesquisa)"

# Uma sessao por thread. Compartilhar uma conexao HTTP entre threads corrompe
# a maquina de estados do protocolo — respostas saem trocadas entre pedidos.
_LOCAL = threading.local()


def sessao_da_thread(cfg: FeedConfig) -> "Sessao":
    s = getattr(_LOCAL, "sessao", None)
    if s is None:
        host = cfg.base_url.split("//", 1)[1].split("/", 1)[0]
        s = _LOCAL.sessao = Sessao(host, cfg.timeout_s)
    return s


class Sessao:
    """Conexao TLS reaproveitada para muitos pedidos.

    E a diferenca entre 4 e 41 arquivos por minuto, medida em campo limpo. Sem
    keep-alive, cada pedido abre um TCP+TLS novo, e o que a Dukascopy recusa e
    a taxa de CONEXOES NOVAS, nao a de pedidos: serial com 4 s de intervalo
    entre pedidos deu 20% de sucesso, e duas conexoes persistentes sem intervalo
    nenhum deram 100%.

    Uma por thread. Reconecta so quando o servidor fecha ou o socket morre — e
    tenta duas vezes, porque a primeira falha depois de uma conexao ociosa e
    quase sempre o servidor a ter fechado do lado dele.
    """

    def __init__(self, host: str, timeout_s: float) -> None:
        self.host = host
        self.timeout_s = timeout_s
        self.conn: http.client.HTTPSConnection | None = None
        self.reconexoes = 0

    def _abrir(self) -> http.client.HTTPSConnection:
        if self.conn is None:
            self.conn = http.client.HTTPSConnection(self.host, timeout=self.timeout_s)
            self.reconexoes += 1
        return self.conn

    def get(self, caminho: str) -> tuple[int, bytes]:
        """Devolve (status, corpo). Levanta se as duas tentativas falharem."""
        ultimo: Exception | None = None
        for tentativa in (1, 2):
            c = self._abrir()
            try:
                c.request("GET", caminho, headers={
                    "User-Agent": USER_AGENT,
                    "Connection": "keep-alive",
                    "Accept": "*/*",
                })
                r = c.getresponse()
                return r.status, r.read()
            except (http.client.HTTPException, OSError, TimeoutError) as exc:
                ultimo = exc
                self.fechar()
                if tentativa == 2:
                    raise
        raise ultimo  # type: ignore[misc]

    def fechar(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001, S110
                pass
            self.conn = None


class RateLimiter:
    """Intervalo minimo entre requisicoes.

    Conservador de proposito: este e um servico publico e gratuito, e o custo
    de ser bloqueado e perder o unico feed de referencia independente que o
    projeto tem — o criterio 7 do SVC depende dele existir.
    """

    def __init__(self, min_interval_s: float) -> None:
        self.min_interval_s = min_interval_s
        # Por THREAD, nao global: o intervalo protege a conexao, e cada thread
        # tem a sua. Um relogio unico serializaria as conexoes e desfaria o
        # ganho da concorrencia.
        self._local = threading.local()

    def wait(self) -> None:
        if self.min_interval_s <= 0:
            return
        ultimo = getattr(self._local, "ultimo", 0.0)
        d = self.min_interval_s - (time.monotonic() - ultimo)
        if d > 0:
            time.sleep(d)
        self._local.ultimo = time.monotonic()


def fetch(
    cfg: FeedConfig,
    url: str,
    limiter: RateLimiter,
    *,
    logger: JsonlLogger | None = None,
) -> bytes | None:
    """Busca uma URL. Devolve os bytes, ou None se o servidor disser 404.

    404 nao e retentado: significa "este arquivo nao existe", nao "tente de novo".
    Retentar 404 cinco vezes com backoff transformaria um fim de semana normal
    em minutos de espera inutil, multiplicados por cada arquivo do periodo.

    **Todo retry e logado.** Um retry que nao aparece em lugar nenhum torna a
    unica evidencia de que o servidor esta a recusar pedidos ser a lentidao — e
    lentidao sem causa visivel e indistinguivel de rede lenta, disco lento ou
    codigo mal escrito. Foi assim que 503 em serie passou por "download
    demorado" numa primeira execucao real.
    """
    caminho = url.split(cfg.base_url.split("//", 1)[1].split("/", 1)[0], 1)[-1]
    sessao = sessao_da_thread(cfg)

    ultima: Exception | None = None
    for tentativa in range(cfg.max_retries):
        limiter.wait()
        try:
            status, corpo = sessao.get(caminho)
            if status == 200:
                return corpo
            if status == 404:
                return None
            ultima = DukascopyError(f"HTTP {status}")
            setattr(ultima, "code", status)
            if status not in (429, 500, 502, 503, 504):
                raise DukascopyError(f"HTTP {status} em {url}")
        except DukascopyError:
            raise
        except (http.client.HTTPException, TimeoutError, OSError) as exc:
            ultima = exc

        # Backoff exponencial com jitter. O jitter existe para que uma retomada
        # de milhares de arquivos nao sincronize as tentativas num padrao que o
        # servidor leia como abuso.
        espera = min(cfg.backoff_base_s * (2**tentativa), cfg.backoff_max_s)
        espera *= 0.5 + random.random()
        if logger:
            logger.warn(
                event="retry", url=url, tentativa=tentativa + 1,
                de=cfg.max_retries, espera_s=round(espera, 2),
                causa=type(ultima).__name__,
                http=getattr(ultima, "code", None), msg=str(ultima),
            )
        time.sleep(espera)

    raise DukascopyError(f"{cfg.max_retries} tentativas falharam em {url}: {ultima}")


def write_atomic(dest: Path, payload: bytes) -> None:
    """Grava por arquivo temporario e renomeia.

    Sem isto, uma interrupcao no meio da escrita deixa um .bi5 truncado — e a
    retomada, que decide por existencia do arquivo, o trataria como completo.
    O dado corrompido sobreviveria em silencio ate o parser tropecar nele.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(payload)
    tmp.replace(dest)


def download_hour(
    cfg: FeedConfig,
    instrument: str,
    hour: datetime,
    limiter: RateLimiter,
    *,
    root: Path | None = None,
    logger: JsonlLogger | None = None,
) -> str:
    """Baixa um arquivo horario. Devolve `ja_tinha`, `ja_ausente`, `ok`, `empty` ou `absent`.

    Retomavel por construcao: se o bruto ou o marcador de ausencia ja existem,
    nao ha requisicao nenhuma.

    `ja_tinha` e `ja_ausente` foram um `skip` unico ate se descobrir que os dois
    nao sao a mesma coisa. Um diz "este arquivo ja esta no disco"; o
    outro, "o servidor ja disse que este arquivo nunca existiu". Somados, viram um numero que
    parece medir progresso e nao mede: numa retomada de dois anos, um mes inteiro
    de fim de semana marcado como ausente ficaria indistinguivel de um mes
    inteiro de dado baixado. E e justamente esse numero que decide se a corrida
    terminou, quando conferir dezessete mil arquivos a mao nao e opcao.
    """
    dest = raw_path(instrument, hour, root)
    marker = dest.with_name(dest.name + ABSENT_SUFFIX)

    if dest.exists():
        return "ja_tinha"
    if marker.exists():
        return "ja_ausente"

    payload = fetch(cfg, hour_url(cfg, instrument, hour), limiter, logger=logger)

    if payload is None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_bytes(b"")
        return "absent"

    write_atomic(dest, payload)
    return "empty" if len(payload) == 0 else "ok"


def download_range(
    instrument: str,
    start: datetime,
    end: datetime,
    *,
    cfg: FeedConfig | None = None,
    root: Path | None = None,
    logger: JsonlLogger | None = None,
    progress: bool = True,
    on_unit: Callable[[dict], None] | None = None,
) -> dict[str, int]:
    """Baixa os arquivos horarios de [start, end), em ordem cronologica reversa.

    Devolve a contagem por desfecho. Nao levanta ao fim de uma hora que falhou
    depois de todas as tentativas: registra, conta e segue — uma hora
    inalcancavel nao deve custar as outras milhares.

    `on_unit` e chamado depois de CADA arquivo horario, com o estado corrente.
    Existe para que quem observa de fora consiga distinguir "a correr devagar"
    de "travado" sem inspecionar timestamp de arquivo a mao: em dezassete mil
    arquivos isso deixa de ser possivel. Quem recebe o callback decide a
    frequencia com que persiste — aqui ele e chamado sempre, e barato.
    """
    cfg = cfg or FeedConfig.load()
    limiter = RateLimiter(cfg.min_interval_s)

    piso = cfg.history_starts.get(instrument)
    if piso is not None and start < piso:
        if logger:
            logger.warn(
                event="history_floor", instrument=instrument,
                pedido=start.isoformat(), disponivel=piso.isoformat(),
            )
        start = piso

    marcas = list(hours_reverse(start, end))
    total = len(marcas)

    # Quantos arquivos horarios ainda precisam de rede. Contado UMA vez e
    # decrementado no laco: recontar a cada impressao seria quadratico, e em
    # dezessete mil arquivos isso deixa de ser detalhe.
    pendentes = 0
    for h in marcas:
        p = raw_path(instrument, h, root)
        if not p.exists() and not p.with_name(p.name + ABSENT_SUFFIX).exists():
            pendentes += 1
    # `erro` e esgotamento de tentativas: falha de REDE. Nao deixa arquivo nem
    # marcador de proposito — o arquivo horario continua pendente e a proxima
    # passada o tenta de novo. Confundi-lo com `absent`, que e o servidor
    # dizendo que o arquivo nunca existiu, faria a corrida ou insistir para
    # sempre no que nao existe, ou desistir do que so falhou uma vez.
    tally = {"ok": 0, "empty": 0, "absent": 0, "ja_tinha": 0, "ja_ausente": 0, "erro": 0}
    bytes_novos = 0

    if logger:
        logger.info(
            event="download_start", feed=FEED, instrument=instrument,
            desde=start.isoformat(), ate=end.isoformat(),
            arquivos_horarios=total, arquivos_pendentes=pendentes,
            estimativa_piso=estimativa_restante(pendentes, cfg.min_interval_s),
            ordem="cronologica_reversa",
        )

    def uma(marca: datetime) -> tuple[datetime, str, int]:
        """Um arquivo horario. Cada thread tem a sua sessao e o seu intervalo."""
        try:
            d = download_hour(
                cfg, instrument, marca, limiter, root=root, logger=logger
            )
            b = (raw_path(instrument, marca, root).stat().st_size
                 if d in ("ok", "empty") else 0)
            return marca, d, b
        except DukascopyError as exc:
            if logger:
                logger.error(
                    "E5002", event="download_fail", instrument=instrument,
                    hora=marca.isoformat(), msg=str(exc),
                )
            return marca, "erro", 0

    # Submissao em ordem cronologica reversa mesmo com concorrencia: as marcas
    # entram na fila da mais recente para a mais antiga, e uma corrida
    # interrompida deixa o periodo util pronto. A ordem de CONCLUSAO nao e
    # estrita, e nao precisa ser — o que importa e por onde se comeca.
    if cfg.workers <= 1:
        resultados = (uma(m) for m in marcas)
    else:
        pool = ThreadPoolExecutor(max_workers=cfg.workers)
        resultados = pool.map(uma, marcas)

    n = 0
    for marca, desfecho, b in resultados:
        n += 1
        tally[desfecho] += 1
        bytes_novos += b

        # Skip nao consome rede, entao nao entra na estimativa.
        if desfecho not in ("ja_tinha", "ja_ausente"):
            pendentes -= 1

        if on_unit is not None:
            on_unit({
                "unidade": marca.isoformat(),
                "desfecho": desfecho,
                "feitos": n,
                "total": total,
                "pendentes": pendentes,
                "tally": dict(tally),
            })

        if progress and (n % 25 == 0 or n == total or desfecho == "erro"):
            pct = 100.0 * n / total if total else 100.0
            print(
                f"[{n:>6}/{total}] {pct:5.1f}%  {marca:%Y-%m-%d %Hh}  {desfecho:<10}"
                f"  ok={tally['ok']} vazios={tally['empty']} ausentes={tally['absent']}"
                f" ja_tinha={tally['ja_tinha']} ja_ausente={tally['ja_ausente']}"
                f" erros={tally['erro']}"
                f"  | faltam {pendentes} arq, ~{estimativa_restante(pendentes, cfg.min_interval_s)}",
                flush=True,
            )

    if cfg.workers > 1:
        pool.shutdown(wait=True)

    if logger:
        logger.info(
            event="download_done", feed=FEED, instrument=instrument,
            arquivos_horarios=total, arquivos_pendentes=pendentes,
            bytes_novos=bytes_novos, **tally,
        )
    return tally


def download_month(
    instrument: str,
    year: int,
    month: int,
    **kwargs: object,
) -> dict[str, int]:
    """Atalho para um mes-calendario inteiro, em UTC."""
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    end = (
        datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        if month == 12
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    )
    return download_range(instrument, start, end, **kwargs)  # type: ignore[arg-type]
