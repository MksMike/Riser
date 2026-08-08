# 0003 — `requires-python` tem teto, e o teto é 3.11

2026-08-08 | Status: aceita

## Contexto

O `pyproject.toml` declarava `requires-python = ">=3.11"` e dependências com
piso (`pandas>=2.2`, `numpy>=1.26`). Piso sem teto não é especificação de
ambiente: é permissão para o resolvedor escolher.

No PC-Home, o único interpretador instalado era o 3.14.6. O venv foi criado,
`pip install -e ".[dev]"` completou **sem erro**, e o ambiente resultante tinha
pandas 3.0.5 e numpy 2.5.1 — a série 3.x do pandas, com semântica de cópia
diferente da 2.x. Nada avisou.

O critério de saída da Camada 1 é paridade provada. Dois PCs que produzem
barras diferentes a partir dos mesmos ticks tornam a paridade impossível de
medir, porque a divergência do ambiente é indistinguível da divergência do
código.

## Decisão

`requires-python = ">=3.11,<3.12"`. Todas as dependências com versão exata,
incluindo as de desenvolvimento. `requirements.lock.txt` gerado por
`pip freeze` define a realidade instalada; o `pyproject.toml` declara apenas a
intenção.

O teto é **3.11**, não 3.12, e a fronteira foi verificada contra o índice PyPI
antes de ser escrita, não deduzida:

| pacote | cp311 | cp312 |
|---|---|---|
| pandas 2.2.3, numpy 1.26.4, pyarrow 15.0.2, duckdb 1.1.3 | wheel | wheel |
| **MetaTrader5 5.0.45** | **wheel** | **não publica** |

`MetaTrader5` é extensão compilada e é o pacote que restringe primeiro. Ele
define o teto real do projeto, e essa é a evidência que fixa a versão.

Isto virou o invariante 9 do `CLAUDE.md`.

## Consequências

Fica mais fácil: atribuir uma divergência de agregação ao código, que é o único
lugar onde ela é interessante.

Fica mais difícil: adotar biblioteca nova ou versão nova. Passa a exigir
decisão explícita, lock regenerado nos dois PCs e comparação de agregação sobre
a mesma amostra de ticks antes de aceitar.

Fica impossível: um PC instalar silenciosamente um ambiente diferente do outro.
A instalação falha em vez de resolver algo diferente — que é o comportamento
desejado, e é a razão de o teto existir em vez de só o piso.

Custo aceito: o projeto fica preso ao Python 3.11 enquanto `MetaTrader5` não
publicar wheel mais nova. Revisar esta decisão quando isso mudar, não antes.

## Alternativas descartadas

**Manter `>=3.11` sem teto.**
O que teria acontecido — e de fato aconteceu, antes de ser revertido: o venv
nasceu em 3.14.6 com pandas 3.0.5, e a instalação foi bem-sucedida. O agregador
de barras teria sido escrito e validado contra a semântica de cópia do pandas
3.x. O segundo PC, com 3.11 e pandas 2.2, produziria barras diferentes a partir
dos mesmos ticks. A divergência apareceria no teste de paridade e seria
naturalmente atribuída ao código do agregador ou à implementação MQL5 — dias de
depuração num lugar onde não há bug nenhum.

**Teto em `<3.13`.**
Foi a primeira proposta, baseada em numpy 1.26.4 e pyarrow 15.0.2, que não
publicam wheel cp313+. O que teria acontecido: um venv 3.12 passaria no teto
declarado e falharia na instalação de `MetaTrader5`, porque a fronteira real é
uma minor antes. O erro só apareceria no segundo PC, se lá houvesse 3.12 — ou
seja, num momento em que a suposição já estaria registrada no `pyproject.toml`
como se fosse fato verificado. Foi descartada por testar contra o índice em vez
de aceitar a dedução.

**Pinar só as quatro bibliotecas de dado, deixando o resto flutuar.**
O que teria acontecido: `matplotlib` e `pillow` arrastam `numpy` como
dependência transitiva e podem forçar um upgrade que anula o pin de `numpy`
justamente no pacote que mais importa para a agregação. Além disso, `ruff`
altera regras de lint entre minors: o mesmo código passaria num PC e falharia
no outro, por um motivo sem relação com o problema em investigação. Meio
ambiente pinado dá a confiança de um ambiente pinado sem a propriedade.

**Adotar `uv` ou `poetry.lock` em vez de `pip freeze`.**
Resolveriam melhor o fecho transitivo e são mais rápidos. Descartada por ora
porque acrescentam uma ferramenta ao roteiro de instalação de um PC novo, e o
`README.md` já assume `py -m venv` e `pip`. `requirements.lock.txt` com `pip` é
o menor denominador comum e não depende de nada além do interpretador. Revisar
se o fecho transitivo do `pip freeze` mostrar-se insuficiente na prática —
provável candidato quando entrar o terceiro ambiente, como um VPS de coleta.
