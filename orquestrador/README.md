# Orquestrador de backtest via Telegram

Mandas uma tarefa pelo Telegram. Um agente orquestrador delega a sub-agentes
(pesquisa, proposta, relatório), corre backtests isolados, aplica um gate
determinístico e só te pergunta quando algo sobrevive a esse gate. Nada é
aplicado ao teu código sem tu carregares no botão.

Modelos locais via **Ollama**. Sem serviços externos, sem chaves de API.

```
Telegram (tu)
    │  "reduzir o drawdown sem perder retorno"
    ▼
Orquestrador ──► Agente Pesquisa    (Llama: que hipótese testar a seguir?)
    │        └─► Agente Proponente  (Llama: que valores concretos?)
    │                                     │ validado contra limites duros
    ▼                                     ▼
Fila SQLite ──► Sandbox (git worktree, sem rede, sem segredos)
                     │  backtest em TREINO e em VALIDAÇÃO
                     ▼
              Gate determinístico  ◄── isto NÃO é um LLM
                     │
          chumbou ───┴─── passou ──► Telegram: pede-te aprovação
             │                              │
          descarta                    tu aprovas ──► ramo git novo
                                                     (tu é que fazes merge)
```

## O que este sistema recusa fazer

Estas não são limitações por falta de tempo — são o desenho.

| Recusa | Porquê |
|---|---|
| Fazer merge automático | Uma métrica que melhorou pode ser ruído. A decisão é tua, com o diff à frente. |
| Escrever no teu ramo ativo | Cada ensaio corre num `git worktree` descartável. Uma proposta aprovada vai para um ramo novo. |
| Tocar no holdout | Ensaios automáticos param antes de `holdout_start`. Só uma ordem tua expressa o corre, e só uma vez. |
| Deixar um LLM decidir | O gate é aritmética. Pedir a um modelo que julgue um número que ele ajudou a produzir não é validação. |
| Otimizar sem limite | Ao fim de `max_trials_per_study` ensaios o estudo fecha, porque o melhor de N tentativas é inflacionado por construção. |

## O que tens de fornecer

O sistema não sabe nada sobre a tua estratégia. Fala com ela por um contrato:

**1. Um comando que corre um backtest.** Configuras em `target.backtest_cmd`:

```yaml
backtest_cmd: "python run_backtest.py --params {params_file} --start {start} --end {end} --out {metrics_file}"
```

Os placeholders `{params_file}`, `{metrics_file}`, `{start}`, `{end}` e
`{workdir}` são preenchidos a cada ensaio.

**2. Um JSON de métricas.** O teu script escreve-o em `{metrics_file}`:

```json
{
  "returns": [0.0012, -0.0034, ...],
  "trades": 420,
  "max_drawdown": 0.14,
  "periods_per_year": 252
}
```

`returns` é a série de retornos por período. **Manda-a se puderes** — sem ela
não há assimetria, curtose nem número de observações, e o Deflated Sharpe passa
a ser um palpite. Se não puderes, o mínimo é `{"sharpe": 1.2, "trades": 420}` e
o sistema avisa-te em cada relatório que o número é fraco.

**3. Um repositório git.** O projeto-alvo tem de estar sob git. Sem
versionamento não há como reverter uma alteração automática, e o sistema
recusa-se a arrancar.

## Instalação

```bash
pip install -r requirements.txt
cp .env.example .env            # põe aqui o TELEGRAM_BOT_TOKEN
cp config.example.yaml config.yaml
$EDITOR config.yaml             # target.path, params_schema, protocolo, gate
python cli.py doctor            # verifica tudo antes de arrancar
python cli.py run               # bot + worker
```

`.env` e `config.yaml` estão no `.gitignore`. **O token nunca entra no YAML** —
o carregamento do config recusa-se a arrancar se encontrar um lá.

### Modelos

Um modelo por sub-agente, em `llm.models`. Vê os nomes exatos com `ollama list`.

```yaml
models:
  research: qwen2.5-coder:7b    # gera hipóteses
  proposer: qwen2.5-coder:7b    # converte hipótese em valores
  report:   gemma3:4b           # escreve uma frase de leitura
```

Com um modelo de 7B, mantém `experiment.mode: params` — o agente escolhe
valores dentro de limites que tu defines e nunca escreve código. O modo `code`
(o agente propõe um diff, validado com `git apply --check` e pelos testes do
projeto) existe para quando tiveres um modelo maior.

Se o modelo falhar todas as tentativas, o proponente cai numa amostragem dentro
dos limites em vez de parar o estudo. Um mau dia do Llama não trava a fila.

## Comandos do Telegram

| Comando | O que faz |
|---|---|
| *(texto normal)* | Vira uma tarefa: o orquestrador gera hipóteses e enfileira ensaios |
| `/estado` | Estudo atual, ensaios gastos, o que aguarda decisão tua |
| `/ensaios` | Últimos ensaios com Sharpe OOS e DSR |
| `/baseline` | Mede a estratégia atual — a referência que tudo tem de bater |
| `/estudo <objetivo>` | Fecha o estudo atual e abre um novo (reinicia a contagem de ensaios) |
| `/aprovar <id>` | Escreve os parâmetros num ramo git novo |
| `/rejeitar <id>` | Descarta |
| `/holdout <id>` | Corre o holdout — uma única vez, com confirmação |
| `/parar` | Cancela a fila |

Só os `chat_id` em `telegram.allowed_chat_ids` são atendidos. Tudo o resto é
ignorado em silêncio e registado.

## Porquê o Deflated Sharpe

É o critério que uma aprovação humana nunca conseguiria aplicar, e a razão pela
qual "eu aprovo antes de aplicar" não chega como proteção.

```
Mesmo resultado, veredito oposto:

  Sharpe OOS 1.55, depois de 3 ensaios    →  DSR 0.999  →  PASSA
  Sharpe OOS 1.55, depois de 400 ensaios  →  DSR 0.916  →  CHUMBA
```

Se testaste 400 configurações e ficaste com a melhor, o Sharpe dessa melhor
está inflacionado só por teres testado 400. O DSR (Bailey & López de Prado,
2014) pergunta qual a probabilidade de o resultado ser real *dado que foi o
melhor de N tentativas*. Olhando para "Sharpe 1.55" no Telegram, tu não terias
como ver a diferença — é o mesmo número.

O sistema conta os ensaios por ti, em SQLite, no momento em que acontecem.

## Critérios do gate

Todos têm de passar. Não há média ponderada nem "quase lá".

| Critério | Omissão | Apanha |
|---|---|---|
| `trades` | ≥ 100 | Sharpe alto com 12 trades é anedota |
| `oos_sharpe` | ≥ 0.5 | resultado fraco fora da amostra |
| `oos_drawdown` | ≤ 25% | risco inaceitável |
| `dsr` | ≥ 0.95 | o melhor de N tentativas |
| `is_oos_gap` | ≤ 1.0 | overfit: brilha no treino, morre na validação |
| `improvement` | ≥ 5% | mexer por mexer |

## Estrutura

```
orq/
  config.py         config + .env, limites dos parâmetros
  store.py          SQLite: fila durável, ensaios, contagem de trials
  sandbox.py        git worktree, sem rede, ambiente limpo, guarda do holdout
  metrics.py        Sharpe, drawdown, PSR, Deflated Sharpe
  gate.py           os critérios — sem LLM
  orchestrator.py   o ciclo: tarefa → hipótese → ensaio → gate → aprovação
  worker.py         consome a fila; sobrevive a crash
  llm/              provider Ollama, extractor de JSON tolerante, fake p/ testes
  agents/           pesquisa, proponente, relatório
  telegram/         cliente HTTP e bot
cli.py              doctor | run | bot | worker | estado
```

## Resistência a falhas

- **O worker morre a meio de um backtest** — `recover_stale` devolve à fila o
  que ficou pendurado. O estado está todo em SQLite, nada em memória.
- **O Ollama não responde** — o proponente cai na amostragem; o comentário do
  relatório é omitido; os números continuam corretos.
- **O modelo devolve lixo** — o erro concreto é reenviado ao modelo
  (*"sma_slow tem de estar entre 10 e 300, mandaste 1200"*), até 3 tentativas.
- **O modelo inventa um parâmetro** — rejeitado em Python antes de chegar ao
  backtest.
- **O Telegram recusa o Markdown** — a mensagem é reenviada em texto simples.
  Perde-se o negrito, não se perde o aviso.

## Testes

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -q     # 81 testes
```

Correm sem Ollama e sem GPU: o LLM é substituído por um provider guionado. O
resto é a sério — subprocessos, `git worktree`, SQLite em disco.

## Limites que deves conhecer

- **Isto não valida uma estratégia.** Reduz a probabilidade de te enganares
  sozinho; não a elimina. Um backtest continua a ser uma simulação com
  pressupostos teus sobre custos, slippage e liquidez.
- **O holdout gasta-se.** Depois de o veres uma vez, faz parte da tua escolha.
  Outra medição independente exige dados que ainda não existiam.
- **Abrir estudos novos para "limpar" o DSR é enganares-te a ti próprio.** A
  contagem só reinicia com honestidade se a hipótese for mesmo nova.
- **`unshare` pode não estar disponível** em todos os sistemas. Nesse caso o
  backtest corre com acesso à rede e o `doctor` avisa-te.
