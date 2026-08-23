# Projeto de backtest

O lado que o orquestrador executa e onde o agente de desenvolvimento programa.

A separação entre as duas metades é o ponto todo:

```
  ARNÊS (protegido)                    ESTRATÉGIA (editável)
  ─────────────────                    ─────────────────────
  run_backtest.py   ←── executa ──→    estrategia/sinal.py
  metricas.py                          estrategia/risco.py
  dados.py
  testes/

  decide: retornos, custos,            decide: entrar, sair,
  drawdown, o que é um trade,          quanto arriscar
  a janela de tempo
```

A estratégia recebe barras e devolve sinais. Nunca vê retornos, nunca vê
métricas, nunca escolhe a janela de tempo. Por isso é que o agente pode mexer
nela à vontade: **não há caminho daí até falsificar um Sharpe**.

Se puseres tudo num ficheiro só, a lista branca do orquestrador deixa de
proteger seja o que for.

## Começar

```bash
git init && git add -A && git commit -m "inicial"   # obrigatório: ver abaixo
python3 gerar_dados_exemplo.py          # série sintética, para testar já
python3 -m unittest discover -s testes -t .
python3 run_backtest.py --params params.json \
    --start 2015-01-01 --end 2021-12-31 --out /tmp/m.json
```

Zero dependências: só biblioteca padrão.

O `git init` não é opcional. O orquestrador corre cada ensaio num `git
worktree` descartável — é assim que o teu código nunca é tocado — e recusa-se a
arrancar sobre uma pasta que não esteja sob git. Também é o que te deixa
reverter qualquer coisa que corra mal.

## Ligar ao orquestrador

No `orquestrador.py`:

```python
PROJETO = "/caminho/para/projeto-backtest"
COMANDO_BACKTEST = ("python3 run_backtest.py --params {params} "
                    "--start {inicio} --end {fim} --out {saida}")
COMANDO_TESTES = "python3 -m unittest discover -s testes -t ."
FICHEIROS_EDITAVEIS = ["estrategia"]    # ← nunca run_backtest.py nem metricas.py
PASTAS_LIGADAS = ["dados"]
FICHEIRO_PARAMS = "params.json"
```

## Os teus dados

Põe um CSV em `dados/serie.csv`. Uma barra por linha, colunas em português ou
inglês (`data`/`date`, `fecho`/`close`, ...). Só `data` e `fecho` são
obrigatórias.

```csv
data,abertura,maxima,minima,fecho,volume
2015-01-02,100.0,101.2,99.4,100.8,342000
```

Os dados gerados por `gerar_dados_exemplo.py` são **ruído sintético**. Servem
para confirmar que a maquinaria funciona. Qualquer resultado que obtenhas com
eles não diz nada sobre trading.

## O contrato das métricas

`run_backtest.py` grava:

```json
{
  "returns": [0.0012, -0.0034, ...],
  "trades": 39,
  "max_drawdown": 0.21,
  "total_return": 0.34,
  "periods_per_year": 252
}
```

`returns` é o campo que importa. Com a série, o orquestrador calcula
assimetria, curtose e número de observações, e o Deflated Sharpe fica fiável.
Sem ela seria um palpite.

## O desfasamento de uma barra

O sinal calculado com o fecho da barra `t` só é executado na barra `t+1`. Está
em `metricas.simular()`, dentro do arnês, fora do alcance do agente.

É o único motivo pelo qual os números deste backtest são credíveis. Para veres
o que está em jogo, uma estratégia que soubesse o dia seguinte:

```
  vidente (sabe o dia seguinte): Sharpe 10.4
  o mesmo sinal 1 dia atrasado : Sharpe -0.3
```

Se alguma vez vires um Sharpe perto do primeiro, não festejes — procura o bug.

## Escrever a tua estratégia

Mexe em `estrategia/sinal.py`. Mantém a assinatura:

```python
def gerar_sinais(barras, params: dict) -> list[int]:
    # um valor por barra: 1 comprado, 0 fora, -1 vendido
```

Regra: o sinal na posição `i` só pode usar informação até `barras[i]`.

O teste `test_sem_lookahead` verifica isto de forma barata e eficaz: trunca a
série em vários pontos e confirma que os sinais anteriores não mudaram. Se
mudarem, estás a usar dados do futuro.

Os parâmetros que puseres em `params.json` têm de estar declarados em
`PARAMETROS` no orquestrador, com limites — é lá que o agente fica impedido de
propor valores absurdos.

## O que este projeto não é

Um cruzamento de médias sobre custos realistas não é uma estratégia rentável, e
não está aqui a fingir que é. É um ponto de partida com a estrutura correta:
separação arnês/estratégia, sem lookahead, com custos, e testável.

O valor está na estrutura. A estratégia é tua.
