"""Verifica que o ambiente em execucao e o ambiente pinado.

Existe por causa do invariante 9: os dois PCs devem produzir resultado identico
a partir dos mesmos ticks. Divergencia de biblioteca produz divergencia de
agregacao que se parece com bug de codigo, e o custo de investigar isso e alto.

Hoje um venv errado so e detectado no `pip install`. Quem ativar o venv errado
depois disso, ou instalar sem o lock, nao recebe aviso nenhum ate os numeros
sairem diferentes.

As versoes NAO estao escritas aqui. Sao lidas do pyproject.toml e comparadas
com o que esta instalado. Copiar os numeros para dentro do teste seria uma
afirmacao sobre outro arquivo, exatamente o que a ADR 0006 proibe: no dia em
que um pin mudasse, o teste continuaria verde contra o valor antigo.
"""

from __future__ import annotations

import re
import sys
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

import pytest

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"
LOCK = Path(__file__).resolve().parent.parent / "requirements.lock.txt"

_PIN = re.compile(r"^([A-Za-z0-9_.\-]+)\s*==\s*([^\s;]+)")
_BOUND = re.compile(r"(>=|<=|==|<|>)\s*([0-9]+(?:\.[0-9]+)*)")


def _pyproject() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)


def _pins() -> dict[str, str]:
    """Todas as dependencias pinadas com ==, incluindo as de dev."""
    proj = _pyproject()["project"]
    deps = list(proj.get("dependencies", []))
    for extra in proj.get("optional-dependencies", {}).values():
        deps.extend(extra)

    pins: dict[str, str] = {}
    for dep in deps:
        m = _PIN.match(dep)
        if m:
            pins[m.group(1)] = m.group(2)
    return pins


def _as_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split("."))


def test_pyproject_pins_everything():
    """Meio ambiente pinado da a confianca de um pinado sem a propriedade."""
    proj = _pyproject()["project"]
    deps = list(proj.get("dependencies", []))
    for extra in proj.get("optional-dependencies", {}).values():
        deps.extend(extra)

    frouxas = [d for d in deps if not _PIN.match(d)]
    assert not frouxas, f"dependencias sem versao exata no pyproject: {frouxas}"


def test_interpreter_within_requires_python():
    """O teto existe: sem ele um venv mais novo resolve versoes diferentes."""
    spec = _pyproject()["project"]["requires-python"]
    bounds = _BOUND.findall(spec)
    assert bounds, f"requires-python sem limite reconhecivel: {spec!r}"

    tem_teto = any(op in ("<", "<=") for op, _ in bounds)
    assert tem_teto, (
        f"requires-python={spec!r} nao tem teto. Piso sem teto nao especifica "
        "ambiente: e permissao para o resolvedor escolher. Ver ADR 0003."
    )

    atual = sys.version_info[:3]
    for op, raw in bounds:
        alvo = _as_tuple(raw)
        n = len(alvo)
        cur = atual[:n]
        if op == ">=":
            assert cur >= alvo, f"Python {atual} < minimo {raw} exigido por {spec!r}"
        elif op == ">":
            assert cur > alvo, f"Python {atual} nao satisfaz >{raw}"
        elif op == "<":
            assert cur < alvo, (
                f"Python {atual} viola o teto <{raw} de {spec!r}. "
                "Provavelmente o venv errado esta ativo."
            )
        elif op == "<=":
            assert cur <= alvo, f"Python {atual} viola o teto <={raw}"
        elif op == "==":
            assert cur == alvo, f"Python {atual} != {raw}"


@pytest.mark.parametrize("pkg", sorted(_pins()))
def test_installed_version_matches_pin(pkg: str):
    """A versao instalada e exatamente a pinada, para toda dependencia."""
    esperada = _pins()[pkg]
    try:
        instalada = version(pkg)
    except PackageNotFoundError:
        pytest.fail(
            f"{pkg} nao esta instalado. Instale por requirements.lock.txt "
            "(invariante 9), nao por pip install solto."
        )
    assert instalada == esperada, (
        f"{pkg}: instalado {instalada}, pinado {esperada}. "
        "O venv nao corresponde ao pyproject; resultados nao sao comparaveis "
        "com os do outro PC."
    )


def test_metatrader5_importa():
    """Extensao compilada, e o pacote que restringe a versao do Python.

    Import so falha aqui se a wheel nao corresponder ao interpretador. Nao
    chama initialize(): isso exigiria um terminal aberto e este teste tem de
    passar sem MT5 em execucao.
    """
    try:
        import MetaTrader5 as mt5
    except ImportError as exc:  # pragma: no cover - so ocorre em venv errado
        pytest.fail(
            f"MetaTrader5 nao importa: {exc}. Wheel incompativel com "
            f"Python {sys.version_info.major}.{sys.version_info.minor} "
            "(5.0.45 publica cp311 e nao cp312). Ver ADR 0003."
        )
    assert getattr(mt5, "__version__", ""), "MetaTrader5 importou sem versao legivel"


def test_lock_existe_e_cobre_os_pins():
    """O lock define a realidade; o pyproject declara intencao (invariante 9)."""
    assert LOCK.exists(), (
        f"{LOCK.name} ausente. Gere com "
        "`pip freeze --exclude-editable > requirements.lock.txt`."
    )

    travadas: dict[str, str] = {}
    for linha in LOCK.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        m = _PIN.match(linha)
        if m:
            travadas[m.group(1).lower()] = m.group(2)

    assert travadas, "requirements.lock.txt nao tem nenhuma versao travada"

    for pkg, esperada in _pins().items():
        assert pkg.lower() in travadas, (
            f"{pkg} esta pinado no pyproject mas ausente do lock. "
            "O lock esta desatualizado: regenere."
        )
        assert travadas[pkg.lower()] == esperada, (
            f"{pkg}: lock diz {travadas[pkg.lower()]}, pyproject diz {esperada}. "
            "Os dois discordam; regenere o lock."
        )
