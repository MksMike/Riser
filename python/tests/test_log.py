"""Testes do envelope de log.

Um schema, duas linguagens. Se o envelope Python divergir do MQL5, os dados nao
se comparam e a validacao cross-feed perde sentido — e a divergencia nao levanta
erro nenhum, so aparece na analise.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from riser.core.log import JsonlLogger, build_hash, config_hash
from riser.core.paths import hash_account_login, logs_dir

# Envelope obrigatorio de docs/schemas/log-schema.md. Toda linha, de todo
# componente, sem excecao.
ENVELOPE = (
    "ts", "ts_srv", "run_id", "build_hash", "config_hash",
    "src", "comp", "lvl", "account_hash", "feed_id", "broker_id",
)


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    monkeypatch.setenv("RISER_DATA_ROOT", str(tmp_path))
    return tmp_path


def _linhas(path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def _unico_arquivo(comp: str, alias: str, account_hash: str | None = None):
    return next(logs_dir(comp, alias, account_hash).glob("*.jsonl"))


# ------------------------------------------------------------------ envelope


def test_toda_linha_carrega_o_envelope_completo(data_root):
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        lg.boot(feed="dukascopy")
        lg.info(event="x")
        lg.warn(event="y")
        lg.error("E5002", event="z")

    linhas = _linhas(_unico_arquivo("dukascopy", "dukascopy"))
    assert len(linhas) == 4
    for rec in linhas:
        faltando = [c for c in ENVELOPE if c not in rec]
        assert not faltando, f"campos ausentes do envelope: {faltando}"


def test_ausencia_deliberada_e_null_nunca_omissao(data_root):
    """Omitir tornaria 'nao se aplica' indistinguivel de bug."""
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        lg.info(event="x")

    rec = _linhas(_unico_arquivo("dukascopy", "dukascopy"))[0]
    for campo in ("ts_srv", "account_hash", "broker_id"):
        assert campo in rec, f"{campo} foi omitido; devia estar presente com null"
        assert rec[campo] is None


def test_feed_id_e_broker_id_sao_independentes(data_root):
    """Feed de referencia tem feed_id e broker_id null; execucao tem os dois."""
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        lg.info(event="feed")
    ref = _linhas(_unico_arquivo("dukascopy", "dukascopy"))[0]
    assert ref["feed_id"] == "dukascopy"
    assert ref["broker_id"] is None

    ah = hash_account_login(12345678)
    with JsonlLogger(
        "collector", alias="exness-standard", feed_id="exness-standard",
        broker_id="exness-standard", account_hash=ah,
    ) as lg:
        lg.info(event="exec")
    exe = _linhas(_unico_arquivo("collector", "exness-standard", ah))[0]
    assert exe["feed_id"] == exe["broker_id"] == "exness-standard"


def test_run_id_constante_na_execucao(data_root):
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        for _ in range(5):
            lg.info(event="x")
    ids = {r["run_id"] for r in _linhas(_unico_arquivo("dukascopy", "dukascopy"))}
    assert len(ids) == 1


def test_run_ids_diferentes_entre_execucoes(data_root):
    vistos = set()
    for _ in range(2):
        with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
            vistos.add(lg.run_id)
    assert len(vistos) == 2


def test_ts_em_utc_com_milissegundos(data_root):
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        lg.info(event="x")
    ts = _linhas(_unico_arquivo("dukascopy", "dukascopy"))[0]["ts"]
    assert ts.endswith("Z"), "ts precisa ser UTC explicito"
    assert len(ts.split(".")[-1]) == 4, "ts precisa de milissegundos (3 digitos + Z)"
    datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)


def test_ts_srv_preenchido_quando_ha_servidor(data_root):
    with JsonlLogger(
        "collector", alias="exness-standard", feed_id="exness-standard",
        server_tz_offset_h=3.0,
    ) as lg:
        lg.info(event="x")
    rec = _linhas(_unico_arquivo("collector", "exness-standard"))[0]
    assert rec["ts_srv"] is not None
    assert not rec["ts_srv"].endswith("Z"), "hora de servidor nao e UTC"


# --------------------------------------------------------------------- erros


def test_erro_carrega_code(data_root):
    """Erro logado sem code e frase, e frase nao e identificador."""
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        lg.error("E5002", event="falhou", msg="texto complementar")
    rec = _linhas(_unico_arquivo("dukascopy", "dukascopy"))[0]
    assert rec["code"] == "E5002"
    assert rec["lvl"] == "error"


def test_nivel_invalido_e_recusado(data_root):
    with JsonlLogger("dukascopy", alias="dukascopy", feed_id="dukascopy") as lg:
        with pytest.raises(ValueError, match="lvl invalido"):
            lg.write("critical", event="x")


# ------------------------------------------------------------------ caminho


def test_caminho_sem_conta_e_explicito(data_root):
    """Nivel ausente seria indistinguivel de esquecimento."""
    assert logs_dir("dukascopy", "dukascopy").name == "no-account"


def test_caminho_com_conta_usa_hash(data_root):
    ah = hash_account_login(12345678)
    p = logs_dir("svc", "exness-standard", ah)
    assert p.name == ah
    assert "12345678" not in str(p), "login em claro no caminho"


def test_login_nunca_em_claro():
    assert hash_account_login(12345678) != "12345678"
    assert hash_account_login(12345678) == hash_account_login("12345678")


# ------------------------------------------------------- procedencia do build


def test_build_hash_mantem_sha_quando_sujo():
    """'dirty' sozinho destroi 'sobre qual base', que e a pergunta do campo."""
    bh = build_hash()
    assert bh, "build_hash vazio"
    if bh.endswith("-dirty"):
        assert len(bh.split("-dirty")[0]) == 7, f"sha ausente em {bh!r}"
    elif bh != "nogit":
        assert len(bh) == 7


def test_config_hash_muda_com_o_conteudo(tmp_path):
    a = tmp_path / "c.yaml"
    a.write_text("x: 1", encoding="utf-8")
    antes = config_hash(a)
    a.write_text("x: 2", encoding="utf-8")
    assert config_hash(a) != antes


def test_config_hash_estavel_para_o_mesmo_conteudo(tmp_path):
    a = tmp_path / "c.yaml"
    a.write_text("x: 1", encoding="utf-8")
    assert config_hash(a) == config_hash(a)
