# Dois programas

```
  programador.py                      orquestrador.py
  ──────────────                      ───────────────
  escreve código                      designa, mede e decide

  • lista branca de ficheiros         • recebe tarefas no Telegram
  • edições procurar/substituir       • gera hipóteses (Agente Pesquisa)
  • Agente Desenvolvimento            • corre backtests isolados
  • cliente Ollama                    • aplica o gate determinístico
                                      • pede-te aprovação
  não sabe o que é um Sharpe          não escreve uma linha de estratégia
```

**Quem mede não programa; quem programa não mede.** É por isso que o agente não
consegue melhorar a sua própria nota — não tem como chegar à régua.

O `orquestrador.py` importa o `programador.py`, portanto os dois têm de estar na
mesma pasta.

## Arrancar

```bash
pip install requests

python programador.py autoteste     # cada um verifica-se a si próprio
python orquestrador.py autoteste    # sem Ollama, sem Telegram, sem GPU
```

**Estes programas precisam de um argumento.** Correr sem nenhum — o que o botão
Run do VS Code faz — mostra-te um painel com as opções, não um erro.

### No VS Code

O botão Run não sabe que argumento passar. Duas saídas:

1. **Terminal** (`Ctrl+'`) e escreves o comando. É o mais simples.
2. **F5** com o `.vscode/launch.json` que vem nesta pasta: dá-te as opções já
   configuradas no menu de depuração ("1. Autoteste", "2. Doctor", ...).

### Windows

Testado com Python 3.10. Três coisas que o sistema trata por ti:

- **Caminhos com `\`** — os comandos são partidos com regras do Windows, senão
  `C:\Python310\python.exe` chegaria ao subprocesso como `C:Python310python.exe`.
- **Symlinks** — criar um exige Modo Programador ou administrador. Para as
  pastas de `PASTAS_LIGADAS` o sistema usa uma *junction* (`mklink /J`), que não
  precisa de privilégios. Se mesmo assim falhar, a mensagem diz-te o que fazer.
- **Isolamento de rede** — o `unshare` é do Linux e não existe aqui. O `doctor`
  avisa-te: no Windows o backtest corre com acesso à rede.

Se os dois passarem, a maquinaria está sã e o que faltar é configuração.

## Configurar

Constantes no topo de cada ficheiro. O `orquestrador.py` é o que precisa de
mais atenção:

```python
TELEGRAM_TOKEN = ""                   # ou a variável TELEGRAM_BOT_TOKEN
CHAT_ID = 6853762483
PROJETO = "/caminho/para/o/backtest"  # tem de ser a RAIZ de um repositório git
COMANDO_BACKTEST = "python3 run_backtest.py --params {params} --start {inicio} --end {fim} --out {saida}"
COMANDO_TESTES = "python3 -m unittest discover -s testes -t ."
FICHEIROS_EDITAVEIS = ["estrategia"]  # ← nunca o que calcula métricas
PASTAS_LIGADAS = ["dados"]
```

Depois:

```bash
python orquestrador.py doctor    # diz-te tudo o que falta
python orquestrador.py correr
```

## Antes de ligares o agente

Corre isto e olha bem para a resposta:

```bash
python programador.py ver --projeto /caminho/para/o/backtest --editaveis estrategia
```

```
  ✏️  EDITAVEIS (3):
        estrategia/__init__.py
        estrategia/risco.py
        estrategia/sinal.py

  🔒 PROTEGIDOS (7):
        dados.py
        metricas.py
        run_backtest.py
        ...
```

Se `metricas.py` ou `run_backtest.py` aparecerem do lado editável, **para**. Um
agente que pode reescrever a régua vai reescrever a régua — é o caminho mais
curto para "melhorar o Sharpe", e não é hipotético.

## Usar o programador sozinho

Útil para perceber porque é que o agente propôs o que propôs, sem esperar por
um ciclo completo:

```bash
python programador.py propor --projeto /caminho --hipotese "filtrar entradas por volatilidade"
```

Mostra o diff proposto e não grava nada. Com `--aplicar` grava (tem o projeto
sob git antes de o fazeres).

## O que o sistema recusa fazer

| Recusa | Porquê |
|---|---|
| Merge automático | Uma métrica que melhorou pode ser ruído. A decisão é tua, com o diff à frente. |
| Escrever no teu ramo ativo | Cada ensaio corre num `git worktree` descartável. |
| Tocar no holdout | Ensaios automáticos param antes de `HOLDOUT[0]`. Só uma ordem tua o corre, e só uma vez. |
| Deixar um LLM decidir | O gate é aritmética. Um modelo a julgar um número que ele ajudou a produzir não é validação. |
| Optimizar sem limite | Ao fim de `MAX_ENSAIOS_POR_ESTUDO` o estudo fecha: o melhor de N tentativas é inflacionado por construção. |

## Porquê o Deflated Sharpe

```
  Sharpe OOS 1.55, depois de   3 ensaios  →  DSR 0.999  →  PASSA
  Sharpe OOS 1.55, depois de 400 ensaios  →  DSR 0.916  →  CHUMBA
```

Mesmo número no ecrã, coisas diferentes na realidade. É o critério que uma
aprovação humana nunca conseguiria aplicar, e a razão pela qual "eu aprovo antes
de aplicar" não chega como proteção.
