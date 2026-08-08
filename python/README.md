# Camada Python

Pesquisa e backtest. Produção é MQL5.

Essa divisão custa o harness de paridade — que existe porque o requisito de
confirmação visual no Strategy Tester tornou MQL5 a plataforma primária. É um
preço deliberado, não um fato da natureza.

```
riser/core/      contrato de sensor, unidades, envelope de log
riser/data/      coleta, alinhamento de timezone, armazenamento
riser/sensors/   implementações de referência
riser/harness/   backtest, paridade, auditoria de causalidade
```

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

Regra: nenhuma função aqui pode usar ponto ou lote. Preço em USD por onça,
tamanho em onças, risco em JPY.
