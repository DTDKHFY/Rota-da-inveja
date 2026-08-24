#!/usr/bin/env python3
"""Orquestrador de backtest via Telegram. Um ficheiro, sem dependencias alem de requests.

    python orquestrador.py             arranca (bot + worker)
    python orquestrador.py teste       verifica que funciona, sem Ollama nem Telegram
    python orquestrador.py configurar  olha para o teu projeto e propoe as definicoes
    python orquestrador.py doctor      verifica a TUA configuracao
    python orquestrador.py ver         que ficheiros o agente pode e nao pode tocar

Dois agentes servidos por Ollama:

    PESQUISA          decide o que investigar a seguir
    DESENVOLVIMENTO   escreve o codigo que testa a hipotese

E um gate que NAO e um agente: sao contas. Quem decide se um ensaio prestou tem
de ser codigo deterministico, senao estamos a pedir a um modelo que julgue um
numero que ele proprio ajudou a produzir.

O agente escreve codigo mas nao consegue melhorar a sua propria nota: a lista
branca (FICHEIROS_EDITAVEIS) impede-o de tocar no que calcula as metricas.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import re
import shlex
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from statistics import NormalDist
from typing import Callable, Sequence

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca requests.  Corre:  pip install requests")


# ===========================================================================
#  CONFIGURACAO — e so isto que tens de mexer
# ===========================================================================

# --- Telegram --------------------------------------------------------------
# Gera/revoga em https://t.me/BotFather -> /mybots -> API Token
# Preferes nao ter o token no ficheiro?  Deixa "" e usa a variavel de ambiente:
#     export TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_TOKEN = ""
CHAT_ID = 6853762483          # so este chat pode dar ordens

# --- Modelos (ve os nomes exatos com: ollama list) -------------------------
# Um modelo por agente. MODELO_DESENVOLVIMENTO e passado ao programador.py.
OLLAMA_URL = "http://localhost:11434"
MODELO_PESQUISA = "minimax-m3:cloud"       # decide o que investigar
MODELO_DESENVOLVIMENTO = "dcxglm-5.2:cloud"  # escreve o codigo (via programador.py)
MODELO_RELATORIO = "gemma4:26b"
MODELO_PARAMS = "qwen2.5-coder:7b"      # so usado em MODO = "params"
TIMEOUT_MODELO = 300
TENTATIVAS_JSON = 3

# --- O teu projeto de backtest --------------------------------------------
# Tem de ser um repositorio git. Ha um projeto de exemplo pronto a usar, com a
# separacao arnes/estrategia ja feita, em ../projeto-backtest/
PROJETO = "/caminho/para/o/teu/backtest"

# Placeholders disponiveis: {params} {saida} {inicio} {fim}
COMANDO_BACKTEST = "python3 run_backtest.py --params {params} --start {inicio} --end {fim} --out {saida}"

# Testes do teu projeto. Correm depois de alterar o codigo e ANTES do backtest:
# um erro de sintaxe apanhado em 2s poupa 40 minutos. Poe "" se nao tiveres.
COMANDO_TESTES = ""      # exemplo: "python3 -m unittest discover -s testes -t ."

FICHEIRO_PARAMS = "params.json"      # onde vivem os parametros em producao
PASTAS_LIGADAS = ["dados"]           # dados fora do git, ligados por symlink
BACKTEST_COM_REDE = False            # True so se o teu backtest precisar mesmo

# --- Modo de trabalho ------------------------------------------------------
MODO = "code"        # "code" = o agente altera codigo | "params" = so valores

# A GUARDA MAIS IMPORTANTE DO MODO "code".
# Os UNICOS ficheiros que o agente de desenvolvimento pode ver e alterar.
# Deixa de fora tudo o que corre e mede o backtest: um agente cuja tarefa e
# melhorar o Sharpe tem um atalho obvio, que e reescrever a funcao que o
# calcula. Nao e rebuscado — e o caminho de menor resistencia.
FICHEIROS_EDITAVEIS = ["estrategia"]   # nunca run_backtest.py nem metricas.py
MAX_LINHAS_EDICAO = 120              # travao contra reescritas

# Limites dos parametros (usados em MODO="params"; o agente nunca sai daqui)
PARAMETROS = {
    "sma_fast":        {"tipo": "int",   "min": 2,     "max": 50},
    "sma_slow":        {"tipo": "int",   "min": 10,    "max": 300},
    "stop_atr":        {"tipo": "float", "min": 0.5,   "max": 6.0},
    "risco_por_trade": {"tipo": "float", "min": 0.001, "max": 0.02},
}

# --- Protocolo: as janelas de tempo ---------------------------------------
TREINO    = ("2015-01-01", "2021-12-31")   # o agente otimiza aqui
VALIDACAO = ("2022-01-01", "2023-12-31")   # e medido aqui
HOLDOUT   = ("2024-01-01", "2025-12-31")   # NUNCA automaticamente. So tu.
MAX_ENSAIOS_POR_ESTUDO = 200               # trava de multiple testing

# --- Gate: todos os criterios tem de passar --------------------------------
MIN_TRADES = 100
MIN_SHARPE_OOS = 0.5
MAX_DRAWDOWN_OOS = 0.25
MIN_DSR = 0.95              # Deflated Sharpe: probabilidade minima
MIN_MELHORIA_PCT = 5.0      # tem de bater a baseline
MAX_GAP_TREINO_VALIDACAO = 1.0

# Teto do contexto enviado ao modelo, em caracteres.
LIMITE_CONTEXTO = 60_000

# --- Onde guardar o estado -------------------------------------------------
BASE = Path(__file__).resolve().parent
BD = BASE / "orq.db"
WORKTREES = BASE / "worktrees"

# ===========================================================================
#  fim da configuracao
# ===========================================================================

log = logging.getLogger("orq")
EULER = 0.5772156649015329
_NORMAL = NormalDist()
_EPS = 1e-12


def token() -> str:
    return (TELEGRAM_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()


def novo_id(prefixo: str) -> str:
    return f"{prefixo}_{uuid.uuid4().hex[:12]}"


# ===========================================================================
#  ESTATISTICA E METRICAS
# ===========================================================================

def media(v: Sequence[float]) -> float:
    return sum(v) / len(v) if v else 0.0


def desvio(v: Sequence[float], ddof: int = 1) -> float:
    n = len(v)
    if n - ddof <= 0:
        return 0.0
    m = media(v)
    return math.sqrt(sum((x - m) ** 2 for x in v) / (n - ddof))


def _degenerado(sigma: float, v: Sequence[float]) -> bool:
    """O desvio-padrao e indistinguivel de zero a precisao do float?

    Uma serie constante nao da desvio exatamente zero: da ~1e-18 de erro
    acumulado. Comparar com zero exato deixa passar esse residuo e a divisao
    seguinte devolve um Sharpe de 1e15. Nao e academico — uma configuracao que
    nao abre trades nenhuns produz exatamente essa serie, e seria a de melhor
    Sharpe de todo o estudo.
    """
    escala = max((abs(x) for x in v), default=0.0)
    return sigma <= max(escala, 1.0) * _EPS


def assimetria(v: Sequence[float]) -> float:
    n, s = len(v), desvio(v, 0)
    if n < 3 or _degenerado(s, v):
        return 0.0
    m = media(v)
    return sum(((x - m) / s) ** 3 for x in v) / n


def curtose(v: Sequence[float]) -> float:
    """Nao-excedente (normal = 3), que e a convencao da formula do PSR."""
    n, s = len(v), desvio(v, 0)
    if n < 4 or _degenerado(s, v):
        return 3.0
    m = media(v)
    return sum(((x - m) / s) ** 4 for x in v) / n


def sharpe(v: Sequence[float], periodos_ano: int | None = None) -> float:
    s = desvio(v)
    if _degenerado(s, v):
        return 0.0
    r = media(v) / s
    return r * math.sqrt(periodos_ano) if periodos_ano else r


def drawdown_maximo(equity: Sequence[float]) -> float:
    if not equity:
        return 0.0
    pico, pior = equity[0], 0.0
    for x in equity:
        pico = max(pico, x)
        if pico > 0:
            pior = max(pior, (pico - x) / pico)
    return pior


def psr(observado: float, referencia: float, n_obs: int,
        skew: float = 0.0, kurt: float = 3.0) -> float:
    """P(Sharpe verdadeiro > referencia), corrigido por assimetria e caudas.

    Ambos os Sharpes tem de estar NAO anualizados. Misturar as duas escalas aqui
    e o erro classico e da um numero bonito e errado.
    """
    if n_obs < 2:
        return 0.0
    denom = 1.0 - skew * observado + ((kurt - 1.0) / 4.0) * observado ** 2
    if denom <= 0:
        return 0.0
    return _NORMAL.cdf(((observado - referencia) * math.sqrt(n_obs - 1)) / math.sqrt(denom))


def sharpe_maximo_esperado(n_ensaios: int, variancia: float) -> float:
    """Sharpe que obterias por puro acaso testando N estrategias sem valor.

    E o patamar que o teu melhor ensaio tem de bater para significar alguma
    coisa. Cresce com o numero de tentativas — por isso "testei 500 combinacoes
    e a melhor deu Sharpe 2" e uma frase quase vazia.
    """
    if n_ensaios < 2 or variancia <= 0:
        return 0.0
    s = math.sqrt(variancia)
    a = _NORMAL.inv_cdf(1.0 - 1.0 / n_ensaios)
    b = _NORMAL.inv_cdf(1.0 - 1.0 / (n_ensaios * math.e))
    return s * ((1.0 - EULER) * a + EULER * b)


def dsr(observado: float, n_obs: int, n_ensaios: int, variancia: float,
        skew: float = 0.0, kurt: float = 3.0) -> float:
    """Deflated Sharpe Ratio (Bailey & Lopez de Prado, 2014).

    Qual a probabilidade deste Sharpe ser real, DADO que foi o melhor de N
    tentativas. E o criterio que uma aprovacao humana nunca conseguiria aplicar:
    Sharpe 1.55 depois de 3 ensaios e Sharpe 1.55 depois de 400 sao o mesmo
    numero no ecra e coisas completamente diferentes na realidade.
    """
    return psr(observado, sharpe_maximo_esperado(n_ensaios, variancia), n_obs, skew, kurt)


@dataclass(frozen=True)
class Janela:
    """Metricas de uma janela de tempo."""
    sharpe: float          # por periodo, nao anualizado
    drawdown: float
    trades: int
    n_obs: int
    skew: float = 0.0
    kurt: float = 3.0
    retorno_total: float | None = None
    periodos_ano: int | None = None

    @property
    def sharpe_anual(self) -> float:
        return self.sharpe * math.sqrt(self.periodos_ano) if self.periodos_ano else self.sharpe


def ler_metricas(bruto: dict) -> Janela:
    """Le o JSON que o teu backtest escreveu.

    Preferivel: dares `returns` (a serie de retornos por periodo). Sem ela nao
    ha assimetria, curtose nem numero de observacoes, e o DSR passa a ser um
    palpite. O minimo aceitavel e {"sharpe": ..., "trades": ...}.
    """
    if not isinstance(bruto, dict):
        raise ValueError("as metricas tem de ser um objeto JSON")
    rets = bruto.get("returns") or []
    if rets and not all(isinstance(r, (int, float)) for r in rets):
        raise ValueError("`returns` tem de ser uma lista de numeros")
    periodos = bruto.get("periods_per_year") or bruto.get("periodos_ano")

    if rets:
        sr, n = sharpe(rets), len(rets)
        sk, ku = assimetria(rets), curtose(rets)
        dd = bruto.get("max_drawdown")
        if dd is None:
            eq = [1.0]
            for r in rets:
                eq.append(eq[-1] * (1 + r))
            dd = drawdown_maximo(eq)
        total = bruto.get("total_return")
        if total is None:
            total = math.prod(1.0 + r for r in rets) - 1.0
    else:
        if "sharpe" not in bruto:
            raise ValueError(
                "faltam `returns` e `sharpe`. Manda pelo menos um — de "
                "preferencia `returns`, senao o Deflated Sharpe fica fraco."
            )
        sr = float(bruto["sharpe"])
        if periodos:
            sr /= math.sqrt(periodos)      # normaliza para por-periodo
        n = int(bruto.get("n_obs", 0))
        sk, ku = float(bruto.get("skew", 0.0)), float(bruto.get("kurtosis", 3.0))
        dd, total = bruto.get("max_drawdown", 0.0), bruto.get("total_return")

    if "trades" not in bruto:
        raise ValueError("falta `trades` nas metricas: sem numero de trades nao ha gate")

    return Janela(
        sharpe=sr, drawdown=float(dd or 0.0), trades=int(bruto["trades"]), n_obs=n,
        skew=sk, kurt=ku,
        retorno_total=None if total is None else float(total),
        periodos_ano=int(periodos) if periodos else None,
    )


# ===========================================================================
#  GATE — os criterios. Nao ha LLM nenhum nesta seccao, de proposito.
# ===========================================================================

@dataclass(frozen=True)
class Criterio:
    nome: str
    passou: bool
    detalhe: str

    def linha(self) -> str:
        return f"{'✅' if self.passou else '❌'} {self.detalhe}"


@dataclass
class Veredito:
    passou: bool
    criterios: list[Criterio] = field(default_factory=list)
    dsr: float = 0.0
    n_ensaios: int = 0
    avisos: list[str] = field(default_factory=list)

    @property
    def falhas(self) -> list[Criterio]:
        return [c for c in self.criterios if not c.passou]

    def resumo(self) -> str:
        cab = "PASSOU no gate" if self.passou else "CHUMBOU no gate"
        n_ok = len(self.criterios) - len(self.falhas)
        linhas = [f"{cab} ({n_ok}/{len(self.criterios)} criterios)"]
        linhas += [c.linha() for c in self.criterios]
        linhas += [f"⚠️ {a}" for a in self.avisos]
        return "\n".join(linhas)

    def dict(self) -> dict:
        return {
            "passou": self.passou, "dsr": self.dsr, "n_ensaios": self.n_ensaios,
            "avisos": self.avisos,
            "criterios": [{"nome": c.nome, "passou": c.passou, "detalhe": c.detalhe}
                          for c in self.criterios],
        }


def avaliar(treino: Janela, validacao: Janela, *, n_ensaios: int,
            sharpes_anteriores: list[float], baseline: Janela | None) -> Veredito:
    """Todos os criterios tem de passar. Sem media ponderada, sem 'quase la'.

    Um criterio flexivel deixa passar ruido sempre que o ruido for simpatico, e
    o proposito do gate e exatamente nao te mandar ruido.
    """
    criterios: list[Criterio] = []
    avisos: list[str] = []

    var = desvio(sharpes_anteriores, 1) ** 2 if len(sharpes_anteriores) >= 2 else 0.0
    d = dsr(validacao.sharpe, validacao.n_obs, max(n_ensaios, 1), var,
            validacao.skew, validacao.kurt)

    if validacao.n_obs == 0:
        avisos.append("sem `returns` nas metricas: o DSR abaixo e uma estimativa fraca")
    if len(sharpes_anteriores) < 2:
        avisos.append("menos de 2 ensaios anteriores: o DSR vai parecer otimista")

    oos = validacao.sharpe_anual

    # 1. Volume. Sharpe alto com 12 trades e uma anedota, nao um edge.
    criterios.append(Criterio(
        "trades", validacao.trades >= MIN_TRADES,
        f"trades na validacao: {validacao.trades} (minimo {MIN_TRADES})"))

    # 2. Resultado fora da amostra.
    criterios.append(Criterio(
        "sharpe_oos", oos >= MIN_SHARPE_OOS,
        f"Sharpe out-of-sample: {oos:.2f} (minimo {MIN_SHARPE_OOS:.2f})"))

    # 3. Risco.
    criterios.append(Criterio(
        "drawdown_oos", validacao.drawdown <= MAX_DRAWDOWN_OOS,
        f"drawdown out-of-sample: {validacao.drawdown * 100:.1f}% "
        f"(maximo {MAX_DRAWDOWN_OOS * 100:.1f}%)"))

    # 4. O Sharpe sobrevive a ter sido o melhor de N tentativas?
    criterios.append(Criterio(
        "dsr", d >= MIN_DSR,
        f"Deflated Sharpe: {d:.3f} apos {n_ensaios} ensaios (minimo {MIN_DSR:.2f})"))

    # 5. Gap treino/validacao: o sinal de overfit mais directo que existe.
    gap = treino.sharpe_anual - oos
    criterios.append(Criterio(
        "gap", gap <= MAX_GAP_TREINO_VALIDACAO,
        f"queda treino->validacao: {gap:+.2f} de Sharpe "
        f"({treino.sharpe_anual:.2f} -> {oos:.2f}, maximo {MAX_GAP_TREINO_VALIDACAO:.2f})"))

    # 6. Bater a baseline. Sem isto o sistema aceitaria mexer por mexer.
    if baseline is not None:
        base = baseline.sharpe_anual
        melhoria = (100.0 if oos > 0 else 0.0) if abs(base) < 1e-9 else (oos - base) / abs(base) * 100.0
        criterios.append(Criterio(
            "melhoria", melhoria >= MIN_MELHORIA_PCT,
            f"melhoria sobre a baseline: {melhoria:+.1f}% "
            f"({base:.2f} -> {oos:.2f}, minimo {MIN_MELHORIA_PCT:+.1f}%)"))
    else:
        avisos.append("sem baseline: corre /baseline antes de aceitares seja o que for")

    return Veredito(all(c.passou for c in criterios), criterios, d, n_ensaios, avisos)


# ===========================================================================
#  ESTADO DURAVEL (SQLite)
#
#  O orquestrador vai morrer a meio de um backtest de 40 minutos — falta de luz,
#  OOM, Ctrl+C sem querer. Nada pode viver so em memoria.
# ===========================================================================

ESQUEMA = """
CREATE TABLE IF NOT EXISTS estudos (
    id TEXT PRIMARY KEY, objetivo TEXT NOT NULL, estado TEXT NOT NULL DEFAULT 'aberto',
    baseline TEXT, criado REAL NOT NULL, fechado REAL, motivo TEXT);
CREATE TABLE IF NOT EXISTS tarefas (
    id TEXT PRIMARY KEY, chat INTEGER NOT NULL, texto TEXT NOT NULL,
    estado TEXT NOT NULL DEFAULT 'fila', criado REAL NOT NULL,
    inicio REAL, fim REAL, pulso REAL, erro TEXT);
CREATE TABLE IF NOT EXISTS ensaios (
    id TEXT PRIMARY KEY, estudo TEXT NOT NULL, tarefa TEXT, hipotese TEXT,
    params TEXT NOT NULL, alteracao TEXT,
    estado TEXT NOT NULL DEFAULT 'fila', aprovacao TEXT NOT NULL DEFAULT 'nenhuma',
    metricas TEXT, holdout TEXT, veredito TEXT, saida TEXT, erro TEXT,
    criado REAL NOT NULL, inicio REAL, fim REAL, pulso REAL);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tipo TEXT NOT NULL, ref TEXT,
    dados TEXT, criado REAL NOT NULL);
CREATE TABLE IF NOT EXISTS kv (chave TEXT PRIMARY KEY, valor TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS i_ensaios ON ensaios(estado, criado);
CREATE INDEX IF NOT EXISTS i_tarefas ON tarefas(estado, criado);
"""


class Estado:
    def __init__(self, caminho: Path | str = None):
        self.caminho = Path(caminho or BD)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.c = sqlite3.connect(self.caminho, timeout=30, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(ESQUEMA)

    def fechar(self):
        self.c.close()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.fechar()

    def evento(self, tipo: str, ref: str | None = None, **dados):
        self.c.execute("INSERT INTO eventos (tipo, ref, dados, criado) VALUES (?,?,?,?)",
                       (tipo, ref, json.dumps(dados, ensure_ascii=False), time.time()))

    def eventos(self, n=50):
        return list(self.c.execute("SELECT * FROM eventos ORDER BY id DESC LIMIT ?", (n,)))

    def kv_ler(self, chave, omissao=None):
        r = self.c.execute("SELECT valor FROM kv WHERE chave=?", (chave,)).fetchone()
        return r["valor"] if r else omissao

    def kv_gravar(self, chave, valor):
        self.c.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(chave) DO UPDATE SET valor=?",
                       (chave, valor, valor))

    # -- estudos ---------------------------------------------------------
    def criar_estudo(self, objetivo: str) -> str:
        eid = novo_id("est")
        self.c.execute("INSERT INTO estudos (id, objetivo, criado) VALUES (?,?,?)",
                       (eid, objetivo, time.time()))
        self.evento("estudo.criado", eid, objetivo=objetivo)
        return eid

    def estudo(self, eid):
        return self.c.execute("SELECT * FROM estudos WHERE id=?", (eid,)).fetchone()

    def estudo_aberto(self):
        return self.c.execute(
            "SELECT * FROM estudos WHERE estado='aberto' ORDER BY criado DESC LIMIT 1").fetchone()

    def fechar_estudo(self, eid, motivo):
        self.c.execute("UPDATE estudos SET estado='fechado', fechado=?, motivo=? WHERE id=?",
                       (time.time(), motivo, eid))
        self.evento("estudo.fechado", eid, motivo=motivo)

    def gravar_baseline(self, eid, metricas: dict):
        self.c.execute("UPDATE estudos SET baseline=? WHERE id=?",
                       (json.dumps(metricas, ensure_ascii=False), eid))

    def n_ensaios(self, eid) -> int:
        """Quantos ensaios ja correram. E este numero que entra no DSR."""
        return int(self.c.execute(
            "SELECT COUNT(*) n FROM ensaios WHERE estudo=? AND estado='feito'",
            (eid,)).fetchone()["n"])

    # -- fila ------------------------------------------------------------
    def _tx(self):
        return self.c

    def nova_tarefa(self, chat: int, texto: str) -> str:
        tid = novo_id("tar")
        self.c.execute("INSERT INTO tarefas (id, chat, texto, criado) VALUES (?,?,?,?)",
                       (tid, chat, texto, time.time()))
        self.evento("tarefa.fila", tid, texto=texto)
        return tid

    def reclamar_tarefa(self):
        """Atomico: dois workers nunca apanham a mesma linha."""
        self.c.execute("BEGIN IMMEDIATE")
        try:
            r = self.c.execute(
                "SELECT * FROM tarefas WHERE estado='fila' ORDER BY criado LIMIT 1").fetchone()
            if r is None:
                self.c.execute("COMMIT")
                return None
            agora = time.time()
            self.c.execute("UPDATE tarefas SET estado='a_correr', inicio=?, pulso=? WHERE id=?",
                           (agora, agora, r["id"]))
            saida = self.c.execute("SELECT * FROM tarefas WHERE id=?", (r["id"],)).fetchone()
            self.c.execute("COMMIT")
            return saida
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def acabar_tarefa(self, tid, estado, erro=None):
        self.c.execute("UPDATE tarefas SET estado=?, fim=?, erro=? WHERE id=?",
                       (estado, time.time(), erro, tid))

    def tarefas(self, n=10):
        return list(self.c.execute("SELECT * FROM tarefas ORDER BY criado DESC LIMIT ?", (n,)))

    def cancelar_fila(self) -> int:
        cur = self.c.execute("UPDATE tarefas SET estado='cancelada', fim=? WHERE estado='fila'",
                             (time.time(),))
        return cur.rowcount

    # -- ensaios ---------------------------------------------------------
    def novo_ensaio(self, estudo, params: dict, hipotese="", tarefa=None,
                    alteracao: str | None = None) -> str:
        eid = novo_id("ens")
        self.c.execute(
            "INSERT INTO ensaios (id, estudo, tarefa, hipotese, params, alteracao, criado) "
            "VALUES (?,?,?,?,?,?,?)",
            (eid, estudo, tarefa, hipotese,
             json.dumps(params, ensure_ascii=False, sort_keys=True), alteracao, time.time()))
        self.evento("ensaio.fila", eid, hipotese=hipotese)
        return eid

    def reclamar_ensaio(self):
        self.c.execute("BEGIN IMMEDIATE")
        try:
            r = self.c.execute(
                "SELECT * FROM ensaios WHERE estado='fila' ORDER BY criado LIMIT 1").fetchone()
            if r is None:
                self.c.execute("COMMIT")
                return None
            agora = time.time()
            self.c.execute("UPDATE ensaios SET estado='a_correr', inicio=?, pulso=? WHERE id=?",
                           (agora, agora, r["id"]))
            saida = self.c.execute("SELECT * FROM ensaios WHERE id=?", (r["id"],)).fetchone()
            self.c.execute("COMMIT")
            return saida
        except BaseException:
            self.c.execute("ROLLBACK")
            raise

    def pulso(self, eid):
        self.c.execute("UPDATE ensaios SET pulso=? WHERE id=?", (time.time(), eid))

    def acabar_ensaio(self, eid, *, estado, metricas=None, veredito=None,
                      aprovacao=None, saida=None, erro=None):
        campos, valores = ["estado=?", "fim=?"], [estado, time.time()]
        for nome, valor in (("metricas", metricas), ("veredito", veredito)):
            if valor is not None:
                campos.append(f"{nome}=?")
                valores.append(json.dumps(valor, ensure_ascii=False))
        for nome, valor in (("aprovacao", aprovacao), ("saida", saida), ("erro", erro)):
            if valor is not None:
                campos.append(f"{nome}=?")
                valores.append(valor)
        valores.append(eid)
        self.c.execute(f"UPDATE ensaios SET {', '.join(campos)} WHERE id=?", valores)

    def aprovar(self, eid, valor):
        self.c.execute("UPDATE ensaios SET aprovacao=? WHERE id=?", (valor, eid))
        self.evento("ensaio.aprovacao", eid, valor=valor)

    def gravar_holdout(self, eid, metricas: dict):
        self.c.execute("UPDATE ensaios SET holdout=? WHERE id=?",
                       (json.dumps(metricas, ensure_ascii=False), eid))

    def ensaio(self, eid):
        return self.c.execute("SELECT * FROM ensaios WHERE id=?", (eid,)).fetchone()

    def ensaios(self, estudo=None, n=10):
        if estudo:
            return list(self.c.execute(
                "SELECT * FROM ensaios WHERE estudo=? ORDER BY criado DESC LIMIT ?", (estudo, n)))
        return list(self.c.execute("SELECT * FROM ensaios ORDER BY criado DESC LIMIT ?", (n,)))

    def por_decidir(self):
        return list(self.c.execute(
            "SELECT * FROM ensaios WHERE aprovacao='pendente' ORDER BY criado"))

    def recuperar(self, limite_seg: float) -> dict:
        """Devolve a fila o que ficou preso de um worker que morreu.

        Sem isto, um crash deixa a linha em 'a_correr' para sempre e a fila para
        em silencio.
        """
        corte = time.time() - limite_seg
        self.c.execute("BEGIN IMMEDIATE")
        try:
            e = self.c.execute(
                "UPDATE ensaios SET estado='fila', inicio=NULL, pulso=NULL "
                "WHERE estado='a_correr' AND (pulso IS NULL OR pulso < ?)", (corte,)).rowcount
            t = self.c.execute(
                "UPDATE tarefas SET estado='fila', inicio=NULL, pulso=NULL "
                "WHERE estado='a_correr' AND (pulso IS NULL OR pulso < ?)", (corte,)).rowcount
            self.c.execute("COMMIT")
        except BaseException:
            self.c.execute("ROLLBACK")
            raise
        if e or t:
            self.evento("recuperado", None, ensaios=e, tarefas=t)
        return {"ensaios": e, "tarefas": t}


# ===========================================================================
#  ERROS
# ===========================================================================

class ErroModelo(Exception):
    """O modelo nao respondeu, ou respondeu algo inutilizavel."""


class ErroAgente(Exception):
    """O agente nao produziu nada valido dentro das tentativas permitidas."""


class ErroEdicao(ValueError):
    """Edicao invalida. A mensagem e escrita para o modelo se poder corrigir."""


class CaminhoProibido(ErroEdicao):
    """Tentativa de tocar num ficheiro fora da lista branca."""


# ===========================================================================
#  LISTA BRANCA
#
#  A guarda central do sistema. Um agente cuja tarefa e melhorar uma
#  metrica tem um atalho obvio: reescrever o codigo que a calcula. Nao e um
#  cenario rebuscado — e o caminho de menor resistencia.
# ===========================================================================

def caminho_permitido(rel: str, padroes: Sequence[str]) -> bool:
    """O ficheiro esta coberto pela lista branca?

    Aceita caminho exato (`estrategia/sinal.py`), prefixo de pasta
    (`estrategia` cobre tudo la dentro) e glob (`estrategia/**/*.py`).

    Nao uso `fnmatch`: la o `*` atravessa `/`, e portanto `*.py` casaria com
    `qualquer/pasta/run_backtest.py`. Numa lista branca isso e um buraco — o
    padrao que escreveste a pensar na raiz do projeto passaria a cobrir o
    ficheiro de metricas dentro de qualquer subpasta. Aqui o `*` para no
    separador; so `**` o atravessa.
    """
    if not padroes:
        return False
    alvo = str(PurePosixPath(rel))
    if alvo.startswith("/") or ".." in PurePosixPath(alvo).parts:
        return False
    for bruto in padroes:
        p = str(PurePosixPath(str(bruto).strip().rstrip("/")))
        if alvo == p or alvo.startswith(p + "/"):
            return True
        regex = (re.escape(p).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*")
                 .replace(r"\*", "[^/]*").replace(r"\?", "[^/]"))
        if re.fullmatch(regex, alvo):
            return True
    return False


def exigir_permitido(rel: str, padroes: Sequence[str]) -> None:
    if not caminho_permitido(rel, padroes):
        raise CaminhoProibido(
            f"`{rel}` nao esta na lista de ficheiros editaveis. So podes alterar: "
            f"{', '.join(padroes) or '(nada)'}. Os ficheiros que correm e medem o "
            f"backtest sao intocaveis.")


def listar_editaveis(projeto: Path, padroes: Sequence[str],
                     limite_bytes: int = 400_000) -> dict[str, str]:
    """Le do disco os ficheiros que a lista branca permite.

    Usado quando este programa corre sozinho. Quando e o orquestrador a chamar,
    e ele que passa os ficheiros — lidos de dentro de um worktree descartavel.
    """
    projeto = Path(projeto)
    ficheiros: dict[str, str] = {}
    for caminho in sorted(projeto.rglob("*")):
        if not caminho.is_file() or "__pycache__" in caminho.parts or ".git" in caminho.parts:
            continue
        rel = caminho.relative_to(projeto).as_posix()
        if not caminho_permitido(rel, padroes):
            continue
        if caminho.stat().st_size > limite_bytes:
            continue
        try:
            ficheiros[rel] = caminho.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    return ficheiros


# ===========================================================================
#  EDICOES POR PROCURAR/SUBSTITUIR
#
#  Porque nao diff unificado: para produzir um diff valido o modelo tem de
#  acertar em numeros de linha e contagens de contexto, e falha opacamente
#  ("patch does not apply"). Blocos ancorados no conteudo falham de forma
#  diagnosticavel, e essa mensagem volta para o modelo, que corrige.
# ===========================================================================

def validar_edicoes(bruto: object) -> list[dict]:
    if not isinstance(bruto, list) or not bruto:
        raise ErroEdicao("`edicoes` tem de ser uma lista nao vazia")
    saida = []
    for i, e in enumerate(bruto):
        if not isinstance(e, dict):
            raise ErroEdicao(f"edicao {i} nao e um objeto")
        for chave in ("procurar", "substituir"):
            if chave not in e:
                raise ErroEdicao(f"edicao {i} nao tem a chave `{chave}`")
            if not isinstance(e[chave], str):
                raise ErroEdicao(f"edicao {i}: `{chave}` tem de ser texto")
        if not e["procurar"].strip():
            raise ErroEdicao(
                f"edicao {i}: `procurar` esta vazio. Para acrescentar codigo, "
                "procura uma linha vizinha e devolve-a junto com o codigo novo.")
        saida.append({"procurar": e["procurar"], "substituir": e["substituir"]})
    return saida


def aplicar_edicoes(conteudo: str, edicoes: list[dict]) -> str:
    """Aplica por ordem. Cada bloco tem de aparecer exatamente uma vez.

    A exigencia de unicidade e deliberada: se um bloco aparece duas vezes, o
    modelo nao disse qual queria, e adivinhar seria alterar codigo ao acaso.
    """
    atual = conteudo
    for i, e in enumerate(validar_edicoes(edicoes)):
        procurar = e["procurar"]
        n = atual.count(procurar)
        if n == 0:
            raise ErroEdicao(
                f"edicao {i}: o bloco a procurar nao aparece no ficheiro."
                f"{_pista(atual, procurar)} Copia o texto exatamente como esta, "
                "incluindo a indentacao.")
        if n > 1:
            raise ErroEdicao(
                f"edicao {i}: o bloco aparece {n} vezes e nao sei qual queres. "
                "Inclui mais linhas de contexto a volta para o tornar unico.")
        atual = atual.replace(procurar, e["substituir"], 1)
    return atual


def _pista(conteudo: str, procurado: str) -> str:
    """Ajuda o modelo a perceber porque falhou, quando da para perceber."""
    linhas = procurado.strip().splitlines()
    primeira = linhas[0].strip() if linhas else ""
    if primeira and primeira in conteudo:
        return (f" A primeira linha (`{primeira[:60]}`) existe, portanto o que "
                "difere e a indentacao ou as linhas seguintes.")
    if primeira and primeira.replace(" ", "") in conteudo.replace(" ", ""):
        return " O texto existe mas com espacamento diferente."
    return ""


def tamanho_edicoes(edicoes: list[dict]) -> int:
    return sum(len(e.get("procurar", "").splitlines()) +
               len(e.get("substituir", "").splitlines()) for e in edicoes)


def pre_visualizar(ficheiro: str, edicoes: list[dict]) -> str:
    """Renderiza a alteracao em formato de diff, para leitura humana."""
    linhas = [f"--- {ficheiro}"]
    for e in edicoes:
        linhas += [f"- {l}" for l in e["procurar"].rstrip("\n").splitlines()]
        linhas += [f"+ {l}" for l in e["substituir"].rstrip("\n").splitlines()]
        linhas.append("")
    return "\n".join(linhas).rstrip()


# ===========================================================================
#  O MODELO
# ===========================================================================

_CERCA = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extrair_json(texto: str):
    """Tira o JSON de uma resposta que pode vir suja.

    Um modelo raramente devolve so o JSON: vem com "Claro! Aqui esta:" antes,
    explicacao depois, cercas de markdown a volta, ou tudo junto. Em vez de
    exigir limpeza ao modelo — que ele nao consegue dar de forma fiavel —
    limpo eu.
    """
    texto = (texto or "").strip()
    if not texto:
        raise ErroModelo("resposta vazia do modelo")
    for cand in _candidatos(texto):
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    raise ErroModelo(f"nao encontrei JSON valido na resposta: {texto[:300]}")


def _candidatos(texto: str):
    yield texto
    for bloco in _CERCA.findall(texto):
        yield bloco.strip()
    for abre, fecha in (("{", "}"), ("[", "]")):
        ini = texto.find(abre)
        if ini == -1:
            continue
        prof, em_texto, escapou = 0, False, False
        for i in range(ini, len(texto)):
            ch = texto[i]
            if em_texto:
                if escapou:
                    escapou = False
                elif ch == "\\":
                    escapou = True
                elif ch == '"':
                    em_texto = False
                continue
            if ch == '"':
                em_texto = True
            elif ch == abre:
                prof += 1
            elif ch == fecha:
                prof -= 1
                if prof == 0:
                    yield texto[ini:i + 1]
                    break


class Ollama:
    """Cliente HTTP do Ollama. Sem SDK — a API sao dois endpoints."""

    def __init__(self, url: str = OLLAMA_URL, timeout: int = TIMEOUT_MODELO):
        self.url = url.rstrip("/")
        self.timeout = timeout

    def conversar(self, sistema: str, utilizador: str, *, modelo: str,
                  json_mode: bool = True) -> str:
        carga = {
            "model": modelo,
            "messages": [{"role": "system", "content": sistema},
                         {"role": "user", "content": utilizador}],
            "stream": False,
            "options": {"temperature": 0.2, "num_ctx": 8192},
        }
        if json_mode:
            carga["format"] = "json"
        try:
            r = requests.post(f"{self.url}/api/chat", json=carga, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            raise ErroModelo(f"nao consegui falar com o Ollama em {self.url}. "
                             "Esta a correr? Testa com `ollama list`.") from e
        except requests.exceptions.Timeout as e:
            raise ErroModelo(f"o modelo {modelo} nao respondeu em {self.timeout}s.") from e
        if r.status_code == 404:
            raise ErroModelo(f"o Ollama nao conhece {modelo!r}. Corre `ollama pull {modelo}`.")
        if not r.ok:
            raise ErroModelo(f"Ollama devolveu {r.status_code}: {r.text[:300]}")
        try:
            return r.json()["message"]["content"]
        except (ValueError, KeyError) as e:
            raise ErroModelo(f"resposta do Ollama inesperada: {r.text[:300]}") from e

    def modelos(self) -> list[str]:
        try:
            r = requests.get(f"{self.url}/api/tags", timeout=15)
            r.raise_for_status()
            return [m["name"] for m in r.json().get("models", [])]
        except (requests.RequestException, ValueError, KeyError):
            return []


class ModeloFalso:
    """Respostas guionadas, para os autotestes correrem sem Ollama nem GPU."""

    def __init__(self, respostas: list[str]):
        self.respostas = list(respostas)
        self.chamadas: list[dict] = []

    def conversar(self, sistema, utilizador, *, modelo, json_mode=True) -> str:
        self.chamadas.append({"sistema": sistema, "utilizador": utilizador, "modelo": modelo})
        if not self.respostas:
            raise ErroModelo("ModeloFalso ficou sem respostas guionadas")
        return self.respostas.pop(0)


def correr_agente(llm, *, papel: str, modelo: str, sistema: str, prompt: str,
                  validar: Callable, tentativas: int = TENTATIVAS_JSON,
                  json_mode: bool = True):
    """O ciclo que torna modelos imperfeitos utilizaveis.

    O que faz a diferenca nao e o prompt — e devolver ao modelo a mensagem de
    erro concreta ("sma_slow tem de estar entre 10 e 300, mandaste 1200"). Com
    isso ele corrige quase sempre a tentativa seguinte. Sem isso, precisarias
    de um modelo muito maior para a mesma taxa de sucesso.
    """
    ultimo = None
    for _ in range(max(1, tentativas)):
        msg = prompt if ultimo is None else (
            f"{prompt}\n\n--- A TUA RESPOSTA ANTERIOR FOI REJEITADA ---\n"
            f"Motivo: {ultimo}\n"
            f"Corrige e devolve APENAS o JSON no formato pedido, sem texto a volta.")
        try:
            bruto = llm.conversar(sistema, msg, modelo=modelo, json_mode=json_mode)
            return validar(extrair_json(bruto) if json_mode else bruto)
        except (ErroModelo, ValueError) as e:
            ultimo = str(e)
    raise ErroAgente(f"[{papel}] o modelo {modelo} falhou {tentativas} tentativas. "
                     f"Ultimo erro: {ultimo}")


# ===========================================================================
#  O AGENTE DE DESENVOLVIMENTO
# ===========================================================================

SISTEMA = """Es um programador a trabalhar numa estrategia de trading.

Recebes uma hipotese e o conteudo de ficheiros. Devolves alteracoes a UM
ficheiro, na forma de blocos procurar/substituir.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- O bloco "procurar" tem de ser copiado EXATAMENTE do ficheiro, com a mesma
  indentacao. E procurado como texto literal, nao como padrao.
- O bloco "procurar" tem de ser unico no ficheiro. Se o trecho se repetir,
  inclui linhas de contexto a volta ate ser unico.
- Altera o minimo necessario para testar a hipotese. Nao reformates, nao
  reorganizes, nao "melhores" codigo que nao faz parte da hipotese.
- So podes alterar os ficheiros que te forem mostrados.
- Nao alteres nada que calcule ou registe metricas.

Formato exato da resposta:
{"ficheiro": "caminho/relativo.py",
 "edicoes": [{"procurar": "texto exato", "substituir": "texto novo"}],
 "justificacao": "uma ou duas frases"}
"""


def render_ficheiros(ficheiros: dict[str, str], limite: int = LIMITE_CONTEXTO) -> str:
    """Junta os ficheiros com numeros de linha, para o modelo se orientar."""
    partes, gasto = [], 0
    for caminho, conteudo in sorted(ficheiros.items()):
        corpo = "\n".join(f"{n:>4} | {l}" for n, l in enumerate(conteudo.splitlines(), 1))
        bloco = f"===== {caminho} =====\n{corpo}\n"
        if gasto + len(bloco) > limite:
            partes.append(f"===== {caminho} =====\n[omitido: contexto esgotado. "
                          "Restringe a lista de ficheiros editaveis.]\n")
            continue
        gasto += len(bloco)
        partes.append(bloco)
    return "\n".join(partes)


def propor_alteracao(ficheiros: dict[str, str], hipotese: dict, *,
                     editaveis: Sequence[str] | None = None,
                     max_linhas: int | None = None,
                     modelo: str = MODELO_DESENVOLVIMENTO, llm=None,
                     tentativas: int = TENTATIVAS_JSON) -> dict:
    """Transforma uma hipotese numa alteracao concreta e validada.

    A validacao corre em Python, nao no modelo, e a edicao e experimentada em
    memoria antes de sair daqui: se um bloco nao encaixa, o modelo recebe a
    razao exata e tenta outra vez. Assim nao gastamos um worktree e quarenta
    minutos de backtest para descobrir que o patch nao aplicava.

    Devolve: {ficheiro, edicoes, conteudo_novo, linhas, justificacao}
    """
    editaveis = tuple(editaveis if editaveis is not None else FICHEIROS_EDITAVEIS)
    max_linhas = MAX_LINHAS_EDICAO if max_linhas is None else max_linhas
    if not ficheiros:
        raise ErroAgente("nao ha ficheiros editaveis: verifica a lista branca")
    llm = llm or Ollama()

    prompt = (
        f"HIPOTESE A IMPLEMENTAR:\n{hipotese['nome']} — {hipotese.get('raciocinio', '')}\n\n"
        f"FICHEIROS QUE PODES ALTERAR:\n{', '.join(editaveis)}\n\n"
        f"CONTEUDO (os numeros de linha sao so para te orientares — nao os "
        f"incluas nos blocos):\n{render_ficheiros(ficheiros)}\n\n"
        f"Limite: no maximo {max_linhas} linhas tocadas no total.\n\n"
        f"Devolve as edicoes.")

    def validar(dados) -> dict:
        if not isinstance(dados, dict):
            raise ErroEdicao("a resposta tem de ser um objeto JSON")
        for chave in ("ficheiro", "edicoes"):
            if chave not in dados:
                raise ErroEdicao(f"falta a chave `{chave}`")

        ficheiro = str(dados["ficheiro"]).strip().lstrip("./")
        exigir_permitido(ficheiro, editaveis)          # a guarda, antes de tudo
        if ficheiro not in ficheiros:
            raise ErroEdicao(f"`{ficheiro}` nao esta entre os ficheiros que te mostrei. "
                             f"Escolhe um de: {', '.join(sorted(ficheiros))}")

        edicoes = validar_edicoes(dados["edicoes"])
        tam = tamanho_edicoes(edicoes)
        if tam > max_linhas:
            raise ErroEdicao(f"a proposta toca {tam} linhas, o maximo e {max_linhas}. "
                             "Reduz a alteracao ao minimo que testa a hipotese.")

        novo = aplicar_edicoes(ficheiros[ficheiro], edicoes)
        if novo == ficheiros[ficheiro]:
            raise ErroEdicao("as edicoes nao mudam nada no ficheiro. Se a hipotese nao "
                             "se consegue implementar aqui, di-lo em `justificacao`.")

        return {"ficheiro": ficheiro, "edicoes": edicoes, "conteudo_novo": novo,
                "linhas": tam, "justificacao": str(dados.get("justificacao", ""))[:400]}

    return correr_agente(llm, papel="programador", modelo=modelo, sistema=SISTEMA,
                         prompt=prompt, validar=validar, tentativas=tentativas)


def escrever_alteracao(projeto: Path, proposta: dict,
                       editaveis: Sequence[str]) -> Path:
    """Grava a alteracao no disco, revalidando a lista branca.

    Revalidar aqui parece redundante — ja foi validada em `propor_alteracao`.
    Nao e: entre uma coisa e a outra a proposta pode ter passado por uma base
    de dados, por um ficheiro, ou por outro processo. O custo de verificar
    outra vez e nenhum comparado com o de deixar passar uma edicao ao ficheiro
    de metricas.
    """
    exigir_permitido(proposta["ficheiro"], editaveis)
    destino = Path(projeto) / proposta["ficheiro"]
    if not destino.is_file():
        raise ErroEdicao(f"`{proposta['ficheiro']}` nao existe em {projeto}")
    destino.write_text(aplicar_edicoes(destino.read_text(encoding="utf-8"),
                                       proposta["edicoes"]), encoding="utf-8")
    return destino




# ===========================================================================
#  SANDBOX — execucao isolada
#
#  1. O codigo que corre a serio nunca e tocado: cada ensaio vive num git
#     worktree descartavel.
#  2. O subprocesso nao ve os segredos: o ambiente e construido de raiz.
#  3. Sem rede por omissao.
# ===========================================================================

class ErroSandbox(Exception):
    pass


class ViolacaoHoldout(Exception):
    """Alguem tentou correr um ensaio automatico sobre o holdout.

    E sempre um bug, nunca uma condicao normal: o holdout so vale enquanto for
    visto uma unica vez, no fim, por decisao humana.
    """


@dataclass(frozen=True)
class Resultado:
    ok: bool
    codigo: int
    saida: str
    segundos: float
    expirou: bool = False

    @property
    def resumo(self) -> str:
        if self.expirou:
            return f"timeout ao fim de {self.segundos:.0f}s"
        return f"saiu com codigo {self.codigo} em {self.segundos:.0f}s"


def cortar(texto: str, inicio=2000, fim=4000) -> str:
    if len(texto) <= inicio + fim:
        return texto
    return f"{texto[:inicio]}\n\n[... {len(texto)-inicio-fim} caracteres omitidos ...]\n\n{texto[-fim:]}"


@lru_cache(maxsize=1)
def _prefixo_sem_rede() -> tuple:
    """`unshare -rn` corta a rede sem precisar de root. Nem todos os sistemas deixam."""
    for cand in (("unshare", "-rn"), ("unshare", "-n")):
        if shutil.which(cand[0]) is None:
            continue
        try:
            if subprocess.run([*cand, "true"], capture_output=True, timeout=10,
                              check=False).returncode == 0:
                return cand
        except (OSError, subprocess.TimeoutExpired):
            continue
    return ()


def sem_rede_disponivel() -> bool:
    return bool(_prefixo_sem_rede())


def ambiente_limpo() -> dict:
    """Construido de raiz, nao herdado.

    O processo do orquestrador tem o token do Telegram carregado; o backtest nao
    tem nada que ver com isso.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", "C.UTF-8"),
        "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
    }


def citar(caminho: str) -> str:
    """Cita um caminho para entrar num comando, conforme o sistema.

    `shlex.quote` usa aspas simples, que no Windows nao sao aspas — sao
    caracteres normais que iriam parar ao nome do ficheiro.
    """
    if os.name == "nt":
        return f'"{caminho}"' if (" " in caminho or not caminho) else caminho
    return shlex.quote(caminho)


def dividir_comando(comando: str) -> list[str]:
    """Parte um comando em argumentos, conforme o sistema.

    No modo POSIX o `shlex` trata `\\` como escape, e portanto
    `C:\\Python310\\python.exe` chega ao subprocesso como
    `C:Python310python.exe` — um caminho que nao existe, com um erro que nao
    explica nada. No Windows uso o modo nao-POSIX, que preserva as barras, e
    tiro as aspas a mao.
    """
    if os.name == "nt":
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
                for t in shlex.split(comando, posix=False)]
    return shlex.split(comando)


def git(repo, *args, check=True):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=check, timeout=120)


def raiz_git(caminho) -> Path | None:
    """A raiz do repositorio a que este caminho pertence, ou None."""
    try:
        r = git(caminho, "rev-parse", "--show-toplevel", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else None


def e_repo_git(caminho) -> bool:
    """O caminho e a RAIZ de um repositorio git?

    Nao basta pertencer a um. Se o projeto estiver dentro de outro repositorio
    — o que acontece com facilidade — `git worktree add` cria a arvore do
    repositorio de fora, e o backtest vai procurar os seus ficheiros num sitio
    onde eles estao uma pasta mais abaixo. O erro que sai daí ("can't open file
    run_backtest.py") nao aponta para a causa nenhuma.
    """
    raiz = raiz_git(caminho)
    return raiz is not None and raiz == Path(caminho).resolve()


def ligar(origem: Path, destino: Path) -> None:
    """Liga um ficheiro ou pasta para dentro do worktree, sem copiar.

    No Windows, criar um symlink exige Modo Programador ligado ou privilegios de
    administrador — a maioria das pessoas nao tem nenhum dos dois. Para pastas ha
    alternativa: uma junction (`mklink /J`), que nao precisa de privilegios
    nenhuns e serve exatamente o mesmo proposito aqui.

    Copiar nao e alternativa: um worktree por ensaio a copiar quatro gigabytes de
    candles enche o disco ao decimo ensaio.
    """
    try:
        destino.symlink_to(origem, target_is_directory=origem.is_dir())
        return
    except OSError as erro_symlink:
        if os.name != "nt":
            raise ErroSandbox(
                f"nao consegui ligar {origem} para {destino}: {erro_symlink}") from erro_symlink

    if origem.is_dir():
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(destino), str(origem)],
                           capture_output=True, text=True, check=False, timeout=60)
        if r.returncode == 0:
            return
    raise ErroSandbox(
        f"nao consegui ligar {origem} para dentro do worktree.\n"
        "No Windows isto costuma resolver-se de uma destas formas:\n"
        "  1. Definicoes -> Privacidade e seguranca -> Para programadores -> "
        "ligar o Modo Programador\n"
        "  2. tirar essa pasta de PASTAS_LIGADAS e por no teu backtest o caminho "
        "absoluto dos dados")


class Sandbox:
    """Um worktree descartavel para um ensaio. Usar com `with`."""

    def __init__(self, ensaio_id: str, projeto: Path | None = None,
                 worktrees: Path | None = None, timeout: int = 1800):
        self.projeto = Path(projeto or PROJETO)
        self.raiz = Path(worktrees or WORKTREES) / ensaio_id
        self.timeout = timeout
        self.criado = False

    def __enter__(self):
        self.criar()
        return self

    def __exit__(self, *a):
        self.limpar()

    def criar(self) -> Path:
        if not self.projeto.is_dir():
            raise ErroSandbox(f"PROJETO nao existe: {self.projeto}")
        if not e_repo_git(self.projeto):
            externa = raiz_git(self.projeto)
            if externa is not None:
                raise ErroSandbox(
                    f"{self.projeto} esta dentro do repositorio {externa}, mas nao e a "
                    f"raiz dele. Precisa de ser um repositorio proprio:\n"
                    f"    cd {self.projeto} && git init && git add -A && "
                    f'git commit -m "inicial"')
            raise ErroSandbox(
                f"{self.projeto} nao e um repositorio git. Faz `git init` e um commit: "
                "sem versionamento nao ha como reverter uma alteracao automatica, e "
                "este sistema recusa-se a trabalhar assim.")
        self.raiz.parent.mkdir(parents=True, exist_ok=True)
        if self.raiz.exists():
            self.limpar()
        try:
            git(self.projeto, "worktree", "add", "--detach", str(self.raiz), "HEAD")
        except subprocess.CalledProcessError as e:
            raise ErroSandbox(f"git worktree add falhou: {e.stderr.strip()}") from e
        self.criado = True
        # Symlink e nao copia: um worktree por ensaio a copiar 4 GB de candles
        # enche o disco ao decimo ensaio.
        #
        # Se a pasta ja existe no worktree — o caso comum, uma pasta versionada
        # com um .gitkeep e o conteudo no .gitignore — ligar a pasta inteira e
        # impossivel. Nesse caso ligo o conteudo, ficheiro a ficheiro. Sem isto
        # a pasta chega vazia ao backtest e ele nao encontra os dados.
        for rel in PASTAS_LIGADAS:
            origem = (self.projeto / rel).resolve()
            destino = self.raiz / rel
            if not origem.exists():
                continue
            if not destino.exists() and not destino.is_symlink():
                destino.parent.mkdir(parents=True, exist_ok=True)
                ligar(origem, destino)
            elif destino.is_dir() and origem.is_dir():
                for filho in origem.iterdir():
                    alvo = destino / filho.name
                    if not alvo.exists() and not alvo.is_symlink():
                        ligar(filho, alvo)
        return self.raiz

    def limpar(self):
        if not self.raiz.exists() and not self.criado:
            return
        try:
            git(self.projeto, "worktree", "remove", "--force", str(self.raiz), check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        if self.raiz.exists():
            shutil.rmtree(self.raiz, ignore_errors=True)
        try:
            git(self.projeto, "worktree", "prune", check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        self.criado = False

    def correr(self, comando: str, timeout: int | None = None) -> Resultado:
        """Sem shell, de proposito: `&&` e `|` nao funcionam. Usa um script."""
        if not self.criado:
            raise ErroSandbox("sandbox nao criado")
        argv = dividir_comando(comando)
        if not argv:
            raise ErroSandbox("comando vazio")
        prefixo = () if BACKTEST_COM_REDE else _prefixo_sem_rede()
        t0 = time.monotonic()
        try:
            p = subprocess.run([*prefixo, *argv], cwd=self.raiz, env=ambiente_limpo(),
                               capture_output=True, text=True,
                               timeout=timeout or self.timeout, check=False)
        except subprocess.TimeoutExpired as e:
            parcial = e.stdout or b""
            if isinstance(parcial, bytes):
                parcial = parcial.decode("utf-8", "replace")
            return Resultado(False, -1, cortar(parcial), time.monotonic() - t0, True)
        except FileNotFoundError as e:
            raise ErroSandbox(f"comando nao encontrado: {argv[0]}") from e
        junto = p.stdout + (("\n[stderr]\n" + p.stderr) if p.stderr else "")
        return Resultado(p.returncode == 0, p.returncode, cortar(junto), time.monotonic() - t0)

    def ficheiros_versionados(self) -> list[str]:
        r = git(self.raiz, "ls-files", check=False)
        return [l for l in r.stdout.splitlines() if l] if r.returncode == 0 else []

    def ler_editaveis(self) -> dict[str, str]:
        """Le so o que o agente pode alterar. O que ele nao ve, nao pode editar."""
        saida = {}
        for rel in self.ficheiros_versionados():
            if not caminho_permitido(rel, FICHEIROS_EDITAVEIS):
                continue
            try:
                saida[rel] = (self.raiz / rel).read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
        return saida

    def aplicar(self, ficheiro: str, edicoes: list[dict]) -> tuple[bool, str]:
        """Aplica a alteracao, revalidando a lista branca.

        Ja foi validada quando a proposta foi aceite. E validada outra vez aqui
        de proposito: pelo meio passou por SQLite, e reverificar nao custa nada
        comparado com deixar passar uma edicao ao arnes de metricas.
        """
        try:
            exigir_permitido(ficheiro, FICHEIROS_EDITAVEIS)
            alvo = self.raiz / ficheiro
            if not alvo.is_file():
                return False, f"`{ficheiro}` nao existe no worktree"
            novo = aplicar_edicoes(alvo.read_text(encoding="utf-8"), edicoes)
        except (ValueError, KeyError, TypeError) as e:
            return False, str(e)
        alvo.write_text(novo, encoding="utf-8")
        return True, f"{ficheiro} alterado"

    def correr_testes(self) -> Resultado | None:
        if not COMANDO_TESTES:
            return None
        return self.correr(COMANDO_TESTES, timeout=min(self.timeout, 600))

    def backtest(self, params: dict, inicio: str, fim: str,
                 permitir_holdout: bool = False) -> tuple[dict | None, Resultado]:
        """Corre o backtest e le as metricas.

        A janela e verificada aqui, no ponto mais estreito por onde todo o
        ensaio passa. Um ensaio automatico que tocasse no holdout queimava-o em
        silencio, e depois nao havia forma de saber se o resultado final
        significava alguma coisa.
        """
        if not permitir_holdout and fim >= HOLDOUT[0]:
            raise ViolacaoHoldout(
                f"ensaio tentou correr ate {fim}, dentro do holdout que comeca em "
                f"{HOLDOUT[0]}. Ensaios automaticos param antes disso.")
        pasta = self.raiz / ".orq"
        pasta.mkdir(exist_ok=True)
        f_params, f_saida = pasta / "params.json", pasta / "metricas.json"
        f_params.write_text(json.dumps(params, indent=2, ensure_ascii=False, sort_keys=True),
                            encoding="utf-8")
        f_saida.unlink(missing_ok=True)
        r = self.correr(COMANDO_BACKTEST.format(
            params=citar(str(f_params)), saida=citar(str(f_saida)),
            inicio=inicio, fim=fim))
        if not r.ok:
            return None, r
        if not f_saida.is_file():
            return None, Resultado(False, r.codigo, cortar(
                r.saida + "\n\n[orq] o backtest correu bem mas nao escreveu o ficheiro de "
                "metricas. O teu script tem de gravar o JSON no caminho que recebe em "
                "{saida} do COMANDO_BACKTEST."), r.segundos)
        try:
            return json.loads(f_saida.read_text(encoding="utf-8")), r
        except json.JSONDecodeError as e:
            return None, Resultado(False, r.codigo,
                                   cortar(r.saida + f"\n\n[orq] metricas.json invalido: {e}"),
                                   r.segundos)


# ===========================================================================
#  OS DOIS AGENTES
#
#  O truque que torna modelos imperfeitos utilizaveis nao e o prompt — e o
#  ciclo. Quando a resposta falha a validacao, devolvemos ao modelo a mensagem
#  de erro concreta ("sma_slow tem de estar entre 10 e 300, mandaste 1200") e
#  ele corrige quase sempre a tentativa seguinte.
# ===========================================================================

SISTEMA_PESQUISA = """Es um analista quantitativo.

A tua unica funcao e propor HIPOTESES para o proximo ensaio, olhando para o
historico. Nao escolhes valores concretos — isso e feito por outro agente.
Dizes O QUE investigar e PORQUE.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- Se o historico mostrar que uma direcao ja foi tentada e piorou, nao a repitas.
- Se nao tiveres base para uma hipotese, diz "explorar" em vez de inventar.

Formato exato:
{"hipoteses": [{"nome": "...", "raciocinio": "...", "direcao": "aumentar"}]}

"direcao" so pode ser: "aumentar", "diminuir" ou "explorar".
"""

SISTEMA_PARAMS = """Escolhes valores de parametros para um backtest.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- Inclui TODOS os parametros da lista, nenhum a mais, nenhum a menos.
- Nunca proponhas um valor fora dos limites indicados.

Formato exato:
{"params": {"nome": valor, ...}, "justificacao": "uma frase"}
"""


def descrever_limites() -> str:
    return "\n".join(
        f"- {n}: {'inteiro' if e['tipo'] == 'int' else 'decimal'} entre {e['min']:g} e {e['max']:g}"
        for n, e in PARAMETROS.items())


def descrever_historico(hist: list[dict], limite=12) -> str:
    if not hist:
        return "(ainda nao ha ensaios neste estudo)"
    linhas = []
    for h in hist[-limite:]:
        p = ", ".join(f"{k}={v:g}" for k, v in sorted(h["params"].items())) or "(sem params)"
        s = h.get("sharpe")
        linhas.append(f"- {p} -> " + ("falhou" if s is None else f"Sharpe OOS {s:+.2f}"))
    return "\n".join(linhas)


def validar_params(propostos) -> dict:
    """A fronteira onde o modelo deixa de poder inventar."""
    if not isinstance(propostos, dict):
        raise ValueError("`params` tem de ser um objeto")
    desconhecidos = set(propostos) - set(PARAMETROS)
    if desconhecidos:
        raise ValueError(f"parametros desconhecidos: {sorted(desconhecidos)}")
    em_falta = set(PARAMETROS) - set(propostos)
    if em_falta:
        raise ValueError(f"parametros em falta: {sorted(em_falta)}")
    saida = {}
    for nome, esp in PARAMETROS.items():
        v = propostos[nome]
        if isinstance(v, bool):
            raise ValueError(f"{nome}: booleano nao e um valor numerico")
        try:
            n = int(v) if esp["tipo"] == "int" else float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{nome}: {v!r} nao converte para {esp['tipo']}") from None
        if not (esp["min"] <= n <= esp["max"]):
            raise ValueError(f"{nome}: {n} fora dos limites [{esp['min']}, {esp['max']}]")
        saida[nome] = n
    return saida


def params_aleatorios(rng: random.Random) -> dict:
    saida = {}
    for nome, e in PARAMETROS.items():
        saida[nome] = (rng.randint(int(e["min"]), int(e["max"])) if e["tipo"] == "int"
                       else round(rng.uniform(e["min"], e["max"]), 6))
    return saida


class Agentes:
    def __init__(self, modelo_llm, estado: Estado):
        self.llm = modelo_llm
        self.estado = estado

    # -- Agente Pesquisa -------------------------------------------------
    def pesquisar(self, objetivo: str, historico: list[dict], n=3) -> list[dict]:
        contexto = (f"PARAMETROS DISPONIVEIS:\n{descrever_limites()}\n\n"
                    if MODO == "params" else "")
        prompt = (f"OBJETIVO DO ESTUDO:\n{objetivo}\n\n{contexto}"
                  f"ENSAIOS JA FEITOS:\n{descrever_historico(historico)}\n\n"
                  f"Propoe exatamente {n} hipoteses distintas para o proximo ensaio.")

        def validar(dados):
            if not isinstance(dados, dict) or "hipoteses" not in dados:
                raise ValueError("falta a chave `hipoteses` no objeto de topo")
            lista = dados["hipoteses"]
            if not isinstance(lista, list) or not lista:
                raise ValueError("`hipoteses` tem de ser uma lista nao vazia")
            saida = []
            for i, h in enumerate(lista):
                if not isinstance(h, dict):
                    raise ValueError(f"hipotese {i} nao e um objeto")
                for c in ("nome", "raciocinio"):
                    if c not in h:
                        raise ValueError(f"hipotese {i} nao tem a chave `{c}`")
                d = str(h.get("direcao", "explorar")).lower().strip()
                if d not in {"aumentar", "diminuir", "explorar"}:
                    raise ValueError(f"hipotese {i}: direcao {d!r} invalida "
                                     "(usa aumentar, diminuir ou explorar)")
                saida.append({"nome": str(h["nome"])[:120],
                              "raciocinio": str(h["raciocinio"])[:600], "direcao": d})
            return saida

        return correr_agente(self.llm, papel="pesquisa", modelo=MODELO_PESQUISA,
                              sistema=SISTEMA_PESQUISA, prompt=prompt, validar=validar,
                              tentativas=TENTATIVAS_JSON)

    # -- Agente Desenvolvimento: delegado ao programador.py --------------
    def desenvolver(self, hipotese: dict, ficheiros: dict[str, str]) -> dict:
        """Transforma a hipotese numa alteracao de codigo ja validada.

        A separacao que importa nao e entre ficheiros — e entre o que o agente
        pode tocar e o que nao pode. Essa e a lista branca, la em cima, e nao
        depende de nada estar noutro ficheiro.
        """
        return propor_alteracao(
            ficheiros, hipotese,
            editaveis=tuple(FICHEIROS_EDITAVEIS),
            max_linhas=MAX_LINHAS_EDICAO,
            modelo=MODELO_DESENVOLVIMENTO,
            llm=self.llm,
            tentativas=TENTATIVAS_JSON,
        )

    # -- Agente Desenvolvimento (modo params) ----------------------------
    def propor_params(self, hipotese: dict, atuais: dict, historico: list[dict],
                      rng: random.Random) -> dict:
        atual_txt = ", ".join(f"{k}={v:g}" for k, v in sorted(atuais.items())) or "(nenhum)"
        prompt = (f"HIPOTESE:\n{hipotese['nome']} — {hipotese['raciocinio']}\n"
                  f"Direcao sugerida: {hipotese['direcao']}\n\n"
                  f"VALORES ATUAIS:\n{atual_txt}\n\n"
                  f"LIMITES (obrigatorio respeitar):\n{descrever_limites()}\n\n"
                  f"ENSAIOS ANTERIORES:\n{descrever_historico(historico, 8)}\n\n"
                  f"Devolve os valores para o proximo ensaio.")

        def validar(dados):
            if not isinstance(dados, dict) or "params" not in dados:
                raise ValueError("falta a chave `params`")
            return {"params": validar_params(dados["params"]),
                    "justificacao": str(dados.get("justificacao", ""))[:400]}

        try:
            return correr_agente(self.llm, papel="params", modelo=MODELO_PARAMS,
                                  sistema=SISTEMA_PARAMS, prompt=prompt, validar=validar,
                                  tentativas=TENTATIVAS_JSON)
        except ErroAgente:
            # Rede de seguranca: um mau dia do modelo nao trava o estudo.
            # (Nao ha equivalente no modo code: uma alteracao de codigo
            # amostrada ao acaso nao e uma hipotese.)
            return {"params": params_aleatorios(rng), "justificacao":
                    "o modelo nao devolveu proposta valida; usei amostragem nos limites",
                    "recurso": True}

    # -- comentario de leitura (opcional) --------------------------------
    def comentar(self, veredito: Veredito, hipotese: str) -> str | None:
        falhas = "; ".join(c.detalhe for c in veredito.falhas) or "nenhum criterio falhou"
        sistema = ("Comentas resultados de backtest em portugues, numa frase, no maximo duas. "
                   "Direto, sem entusiasmo. NUNCA repitas numeros — eles ja aparecem na "
                   "mensagem, e se te enganares induzes uma decisao errada. Comenta o "
                   "significado: solido, frageil, cheira a overfit. So a frase.")
        prompt = (f"Hipotese: {hipotese or '(nenhuma)'}\n"
                  f"Resultado: {'passou' if veredito.passou else 'chumbou'} apos "
                  f"{veredito.n_ensaios} ensaios.\nCriterios falhados: {falhas}\n"
                  f"Deflated Sharpe: {veredito.dsr:.3f}\n\nEscreve a tua frase.")
        try:
            return correr_agente(
                self.llm, papel="relatorio", modelo=MODELO_RELATORIO, sistema=sistema,
                prompt=prompt, validar=lambda t: str(t).strip().strip('"')[:400] or
                (_ for _ in ()).throw(ValueError("vazio")),
                tentativas=1, json_mode=False)
        except ErroAgente:
            return None   # o relatorio nunca falha por causa do comentario


# ===========================================================================
#  RELATORIO
#
#  Os NUMEROS da mensagem que te pede aprovacao sao gerados por codigo, nunca
#  pelo modelo. Um modelo a reescrever "Sharpe 1.24" como "Sharpe 1.42" e um
#  erro plausivel e silencioso, e serias tu a carregar em Aprovar com base nele.
# ===========================================================================

def _fmt_janela(nome: str, j: Janela) -> str:
    linhas = [f"*{nome}*", f"  Sharpe: {j.sharpe_anual:.2f}"]
    if j.retorno_total is not None:
        linhas.append(f"  Retorno: {j.retorno_total * 100:+.1f}%")
    linhas += [f"  Drawdown: {j.drawdown * 100:.1f}%", f"  Trades: {j.trades}"]
    return "\n".join(linhas)


def _fmt_alteracao(alteracao: dict | None, limite=1200) -> str:
    """Mostra o codigo que muda.

    Aprovar uma alteracao de codigo sem a ver e pior do que aprovar parametros
    sem os ver: uma linha muda comportamento de formas que uma metrica agregada
    nao revela.
    """
    if not alteracao:
        return ""
    linhas, gasto = [f"*Codigo* — `{alteracao['ficheiro']}`"], 0
    edicoes = alteracao.get("edicoes", [])
    for i, e in enumerate(edicoes):
        bloco = "```\n" + "\n".join(
            [*(f"- {l}" for l in e["procurar"].rstrip("\n").splitlines()),
             *(f"+ {l}" for l in e["substituir"].rstrip("\n").splitlines())]) + "\n```"
        if gasto + len(bloco) > limite:
            linhas.append(f"_(+{len(edicoes)-i} alteracao(oes) nao cabem aqui — "
                          "ve o ramo git depois de aprovares)_")
            break
        gasto += len(bloco)
        linhas.append(bloco)
    return "\n".join(linhas)


def mensagem_aprovacao(*, ensaio_id: str, hipotese: str, params: dict,
                       treino: Janela, validacao: Janela, veredito: Veredito,
                       alteracao: dict | None = None, comentario: str | None = None) -> str:
    cab = "🟢 Proposta passou no gate" if veredito.passou else "🔴 Proposta chumbou no gate"
    partes = [f"{cab}\n`{ensaio_id}`"]
    if hipotese:
        partes.append(f"\n*Hipotese*\n{hipotese}")
    if alteracao:
        partes.append(f"\n{_fmt_alteracao(alteracao)}")
    elif params:
        partes.append("\n*Parametros*\n" + "\n".join(
            f"  {k}: {v:g}" for k, v in sorted(params.items())))
    partes += [f"\n{_fmt_janela('Treino (in-sample)', treino)}",
               f"\n{_fmt_janela('Validacao (out-of-sample)', validacao)}",
               f"\n*Gate*\n{veredito.resumo()}"]
    if comentario:
        partes.append(f"\n_{comentario}_")
    partes.append(f"\n⚠️ O holdout NAO foi tocado. Estes numeros sao de validacao, "
                  f"depois de {veredito.n_ensaios} ensaios neste estudo.")
    return "\n".join(partes)


# ===========================================================================
#  ORQUESTRADOR
# ===========================================================================

class Aviso:
    """Notificador de mentira, para o autoteste e o modo seco."""

    def __init__(self):
        self.enviadas: list[str] = []
        self.aprovacoes: list[tuple[str, str]] = []

    def enviar(self, texto: str):
        self.enviadas.append(texto)

    def pedir_aprovacao(self, texto: str, ensaio_id: str):
        self.aprovacoes.append((ensaio_id, texto))


class Orquestrador:
    def __init__(self, estado: Estado, modelo_llm, aviso, rng: random.Random | None = None):
        self.estado = estado
        self.agentes = Agentes(modelo_llm, estado)
        self.aviso = aviso
        self.rng = rng or random.Random()

    # -- estudos ---------------------------------------------------------
    def garantir_estudo(self, objetivo: str):
        est = self.estado.estudo_aberto()
        if est is not None:
            return est
        eid = self.estado.criar_estudo(objetivo)
        self.aviso.enviar(f"📚 Estudo novo: `{eid}`\nObjetivo: {objetivo}\n"
                          f"Orcamento: {MAX_ENSAIOS_POR_ESTUDO} ensaios.")
        return self.estado.estudo(eid)

    def _historico(self, estudo_id: str, n=30) -> list[dict]:
        saida = []
        for r in reversed(self.estado.ensaios(estudo_id, n)):
            if r["estado"] != "feito":
                continue
            m = json.loads(r["metricas"]) if r["metricas"] else {}
            saida.append({"params": json.loads(r["params"]),
                          "sharpe": m.get("validacao", {}).get("sharpe_anual")})
        return saida

    def _sharpes(self, estudo_id: str) -> list[float]:
        return [h["sharpe"] for h in self._historico(estudo_id, 500) if h["sharpe"] is not None]

    def _melhores_params(self, estudo_id: str) -> dict:
        melhor, top = {}, float("-inf")
        for h in self._historico(estudo_id, 500):
            if h["sharpe"] is not None and h["sharpe"] > top:
                melhor, top = h["params"], h["sharpe"]
        return melhor

    def _params_vivos(self) -> dict:
        c = Path(PROJETO) / FICHEIRO_PARAMS
        if not c.is_file():
            return {}
        try:
            return json.loads(c.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _baseline(self, estudo_id: str) -> Janela | None:
        est = self.estado.estudo(estudo_id)
        if not est or not est["baseline"]:
            return None
        try:
            return ler_metricas(json.loads(est["baseline"]))
        except (ValueError, json.JSONDecodeError):
            return None

    # -- tarefas ---------------------------------------------------------
    def tratar_tarefa(self, tarefa) -> int:
        objetivo = tarefa["texto"]
        est = self.garantir_estudo(objetivo)
        eid = est["id"]

        usados = self.estado.n_ensaios(eid)
        if usados >= MAX_ENSAIOS_POR_ESTUDO:
            self.estado.fechar_estudo(eid, "orcamento esgotado")
            self.aviso.enviar(
                f"🛑 Estudo `{eid}` fechado: {usados}/{MAX_ENSAIOS_POR_ESTUDO} ensaios.\n\n"
                "Nao e uma falha tecnica — e a trava de multiple testing. Ao fim de "
                "tantas tentativas o melhor resultado ja e explicavel por acaso. Se "
                "queres continuar, abre um estudo novo com /estudo, e comeca com uma "
                "hipotese nova, nao com a continuacao desta busca.")
            return 0

        historico = self._historico(eid)
        try:
            hipoteses = self.agentes.pesquisar(objetivo, historico)
        except ErroAgente as e:
            self.aviso.enviar(f"⚠️ O agente de pesquisa nao produziu hipoteses: {e}")
            return 0

        n, nomes = (self._fila_codigo(eid, tarefa, hipoteses) if MODO == "code"
                    else self._fila_params(eid, tarefa, hipoteses, historico))
        if n == 0:
            self.aviso.enviar(f"⚠️ Nenhuma das {len(hipoteses)} hipoteses deu proposta "
                              "aplicavel. Nada foi para a fila.")
            return 0
        self.aviso.enviar(f"🧪 {n} ensaios em fila para: _{objetivo}_\n" +
                          "\n".join(f"• {x}" for x in nomes))
        return n

    def _fila_params(self, eid, tarefa, hipoteses, historico):
        base = self._melhores_params(eid) or self._params_vivos()
        n, nomes = 0, []
        for h in hipoteses:
            if self.estado.n_ensaios(eid) + n >= MAX_ENSAIOS_POR_ESTUDO:
                break
            p = self.agentes.propor_params(h, base, historico, self.rng)
            ens = self.estado.novo_ensaio(eid, p["params"],
                                          f"{h['nome']} — {h['raciocinio']}", tarefa["id"])
            n += 1
            nomes.append(h["nome"])
            if p.get("recurso"):
                self.estado.evento("params.recurso", ens)
        return n, nomes

    def _fila_codigo(self, eid, tarefa, hipoteses):
        """Le os ficheiros uma unica vez: todas as hipoteses partem do mesmo HEAD."""
        params = self._params_vivos()
        with Sandbox(f"{tarefa['id']}_leitura") as sb:
            ficheiros = sb.ler_editaveis()
        if not ficheiros:
            self.aviso.enviar(
                f"⚠️ Nenhum ficheiro versionado corresponde a FICHEIROS_EDITAVEIS "
                f"({', '.join(FICHEIROS_EDITAVEIS)}). O agente nao tem onde mexer.")
            return 0, []
        n, nomes = 0, []
        for h in hipoteses:
            if self.estado.n_ensaios(eid) + n >= MAX_ENSAIOS_POR_ESTUDO:
                break
            try:
                prop = self.agentes.desenvolver(h, ficheiros)
            except ErroAgente as e:
                self.estado.evento("desenvolvimento.falhou", None, hipotese=h["nome"])
                self.aviso.enviar(f"⚠️ Nao consegui implementar _{h['nome']}_: {e}")
                continue
            self.estado.novo_ensaio(
                eid, params, f"{h['nome']} — {h['raciocinio']}", tarefa["id"],
                alteracao=json.dumps({"ficheiro": prop["ficheiro"], "edicoes": prop["edicoes"]},
                                     ensure_ascii=False))
            n += 1
            nomes.append(f"{h['nome']} ({prop['linhas']} linhas)")
        return n, nomes

    # -- ensaios ---------------------------------------------------------
    def correr_ensaio(self, ensaio) -> bool:
        eid, params = ensaio["id"], json.loads(ensaio["params"])
        try:
            with Sandbox(eid) as sb:
                self.estado.pulso(eid)
                if ensaio["alteracao"]:
                    prop = json.loads(ensaio["alteracao"])
                    ok, det = sb.aplicar(prop["ficheiro"], prop["edicoes"])
                    if not ok:
                        return self._falhar(eid, f"alteracao rejeitada: {det}")
                    # Antes do backtest: um erro de sintaxe apanhado em 2s
                    # poupa 40 minutos.
                    testes = sb.correr_testes()
                    if testes is not None and not testes.ok:
                        return self._falhar(eid, "os testes do projeto falharam depois da "
                                            "alteracao", testes.saida)

                bruto_treino, r1 = sb.backtest(params, *TREINO)
                if bruto_treino is None:
                    return self._falhar(eid, "backtest de treino falhou", r1.saida)
                self.estado.pulso(eid)
                bruto_val, r2 = sb.backtest(params, *VALIDACAO)
                if bruto_val is None:
                    return self._falhar(eid, "backtest de validacao falhou", r2.saida)
        except ViolacaoHoldout as e:
            self.aviso.enviar(f"🚨 VIOLACAO DE HOLDOUT em `{eid}`: {e}")
            return self._falhar(eid, f"violacao de holdout: {e}")
        except ErroSandbox as e:
            return self._falhar(eid, f"sandbox: {e}")

        try:
            treino, validacao = ler_metricas(bruto_treino), ler_metricas(bruto_val)
        except ValueError as e:
            return self._falhar(eid, f"metricas invalidas: {e}")

        estudo_id = ensaio["estudo"]
        veredito = avaliar(treino, validacao,
                           n_ensaios=self.estado.n_ensaios(estudo_id) + 1,
                           sharpes_anteriores=self._sharpes(estudo_id),
                           baseline=self._baseline(estudo_id))

        self.estado.acabar_ensaio(
            eid, estado="feito",
            metricas={"treino": self._dict(treino), "validacao": self._dict(validacao)},
            veredito=veredito.dict(),
            aprovacao="pendente" if veredito.passou else "nenhuma")

        if veredito.passou:
            self.aviso.pedir_aprovacao(mensagem_aprovacao(
                ensaio_id=eid, hipotese=ensaio["hipotese"] or "", params=params,
                treino=treino, validacao=validacao, veredito=veredito,
                alteracao=json.loads(ensaio["alteracao"]) if ensaio["alteracao"] else None,
                comentario=self.agentes.comentar(veredito, ensaio["hipotese"] or "")), eid)
        else:
            self.aviso.enviar(
                f"❌ `{eid}` chumbou ({', '.join(c.nome for c in veredito.falhas)}) — "
                f"Sharpe OOS {validacao.sharpe_anual:.2f}, DSR {veredito.dsr:.3f}")
        return True

    @staticmethod
    def _dict(j: Janela) -> dict:
        return {"sharpe_periodo": j.sharpe, "sharpe_anual": j.sharpe_anual,
                "drawdown": j.drawdown, "trades": j.trades, "n_obs": j.n_obs,
                "skew": j.skew, "kurtosis": j.kurt, "retorno_total": j.retorno_total,
                "periodos_ano": j.periodos_ano}

    def _falhar(self, eid, erro, saida=None) -> bool:
        self.estado.acabar_ensaio(eid, estado="falhou", erro=erro, saida=saida)
        self.aviso.enviar(f"⚠️ `{eid}` falhou: {erro}")
        return False

    # -- baseline e holdout ----------------------------------------------
    def medir_baseline(self, estudo_id: str) -> Janela:
        params = self._params_vivos()
        with Sandbox("baseline") as sb:
            bruto, r = sb.backtest(params, *VALIDACAO)
        if bruto is None:
            raise ErroSandbox(f"baseline falhou: {r.resumo}\n{r.saida[-800:]}")
        self.estado.gravar_baseline(estudo_id, bruto)
        return ler_metricas(bruto)

    def correr_holdout(self, ensaio_id: str) -> Janela:
        """So por ordem humana, e so uma vez.

        Cada vez que se corre o holdout ele perde valor: passa a fazer parte do
        processo de selecao.
        """
        e = self.estado.ensaio(ensaio_id)
        if e is None:
            raise ValueError(f"ensaio {ensaio_id} nao existe")
        if e["holdout"]:
            raise ValueError(
                f"o holdout de {ensaio_id} ja foi corrido. Correr outra vez nao te da "
                "informacao nova — da-te a ilusao de confirmacao. Para outra medicao "
                "independente precisas de dados que ainda nao existiam.")
        with Sandbox(f"{ensaio_id}_holdout") as sb:
            if e["alteracao"]:
                prop = json.loads(e["alteracao"])
                ok, det = sb.aplicar(prop["ficheiro"], prop["edicoes"])
                if not ok:
                    raise ErroSandbox(f"a alteracao ja nao aplica: {det}")
            bruto, r = sb.backtest(json.loads(e["params"]), *HOLDOUT, permitir_holdout=True)
        if bruto is None:
            raise ErroSandbox(f"holdout falhou: {r.resumo}\n{r.saida[-800:]}")
        self.estado.gravar_holdout(ensaio_id, bruto)
        return ler_metricas(bruto)

    # -- aplicar ---------------------------------------------------------
    def aplicar_aprovado(self, ensaio_id: str) -> str:
        """Escreve num ramo git novo. Nao faz merge, nao toca no ramo ativo."""
        e = self.estado.ensaio(ensaio_id)
        if e is None:
            raise ValueError(f"ensaio {ensaio_id} nao existe")
        if e["aprovacao"] != "aprovado":
            raise ValueError(f"ensaio {ensaio_id} nao esta aprovado ({e['aprovacao']})")

        ramo = f"orq/{ensaio_id}"
        with Sandbox(f"{ensaio_id}_aplicar") as sb:
            if e["alteracao"]:
                prop = json.loads(e["alteracao"])
                ok, det = sb.aplicar(prop["ficheiro"], prop["edicoes"])
                if not ok:
                    raise ErroSandbox(f"a alteracao ja nao aplica: {det}")
                alvo = prop["ficheiro"]
            else:
                destino = sb.raiz / FICHEIRO_PARAMS
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_text(json.dumps(json.loads(e["params"]), indent=2,
                                              ensure_ascii=False, sort_keys=True) + "\n",
                                   encoding="utf-8")
                alvo = FICHEIRO_PARAMS
            v = json.loads(e["veredito"]) if e["veredito"] else {}
            msg = (f"{'codigo' if e['alteracao'] else 'params'}: proposta {ensaio_id}\n\n"
                   f"{e['hipotese'] or 'sem hipotese registada'}\n\n"
                   f"Deflated Sharpe: {v.get('dsr', 0):.3f} apos {v.get('n_ensaios', 0)} ensaios\n"
                   f"Holdout: NAO corrido\n")
            for args in (("checkout", "-b", ramo), ("add", alvo),
                         ("-c", "user.email=orq@local", "-c", "user.name=orquestrador",
                          "commit", "-m", msg)):
                r = subprocess.run(["git", "-C", str(sb.raiz), *args],
                                   capture_output=True, text=True, check=False, timeout=120)
                if r.returncode != 0:
                    raise ErroSandbox(f"git {args[0]} falhou: {r.stderr.strip()}")
        # O worktree e descartado, mas os ramos sao partilhados: o ramo fica.
        self.estado.evento("ensaio.aplicado", ensaio_id, ramo=ramo)
        return ramo


# ===========================================================================
#  WORKER
# ===========================================================================

class Worker:
    def __init__(self, orq: Orquestrador, estado: Estado, pausa=2.0, parar=None):
        self.orq, self.estado = orq, estado
        self.pausa = pausa
        self.parar = parar or threading.Event()

    def recuperar(self):
        r = self.estado.recuperar(1800 * 3)
        if r["ensaios"] or r["tarefas"]:
            self.orq.aviso.enviar(f"♻️ Retomei depois de uma paragem: {r['ensaios']} ensaios "
                                  f"e {r['tarefas']} tarefas voltaram a fila.")

    def passo(self) -> bool:
        """Tarefas antes de ensaios: uma ordem tua nova vale mais do que acabar
        a busca anterior."""
        t = self.estado.reclamar_tarefa()
        if t is not None:
            try:
                self.orq.tratar_tarefa(t)
                self.estado.acabar_tarefa(t["id"], "feita")
            except Exception as e:              # o worker nao morre por uma tarefa ma
                log.exception("tarefa %s rebentou", t["id"])
                self.estado.acabar_tarefa(t["id"], "falhou", str(e))
                self.orq.aviso.enviar(f"⚠️ Tarefa falhou: {e}")
            return True
        e = self.estado.reclamar_ensaio()
        if e is not None:
            try:
                self.orq.correr_ensaio(e)
            except Exception as exc:
                log.exception("ensaio %s rebentou", e["id"])
                self.estado.acabar_ensaio(e["id"], estado="falhou", erro=str(exc))
                self.orq.aviso.enviar(f"⚠️ Ensaio `{e['id']}` rebentou: {exc}")
            return True
        return False

    def correr(self):
        self.recuperar()
        log.info("worker a correr")
        while not self.parar.is_set():
            try:
                if not self.passo():
                    self.parar.wait(self.pausa)
            except Exception:
                log.exception("erro no ciclo do worker")
                self.parar.wait(self.pausa)


# ===========================================================================
#  TELEGRAM
# ===========================================================================

AJUDA = """*Orquestrador de backtest*

Manda-me uma tarefa em texto normal, por exemplo:
_reduzir o drawdown sem perder mais de 10% de retorno_

*Comandos*
/estado — estudo atual, fila, ensaios gastos
/ensaios — ultimos ensaios e o que deram
/baseline — mede a estrategia atual (referencia de comparacao)
/estudo <objetivo> — fecha o atual e abre um novo
/aprovar <id> — aplica uma proposta (cria um ramo git)
/rejeitar <id> — descarta
/holdout <id> — corre o holdout (uma unica vez, pensa antes)
/parar — cancela o que estiver em fila

*O que eu nao faco*
Nao faco merge. Uma proposta aprovada vai para um ramo git novo; es tu que
olhas para o diff e decides. Nao corro nada sobre o holdout sem ordem tua.
"""


class Telegram:
    def __init__(self, tok: str, timeout=40):
        if not tok or ":" not in tok:
            raise ValueError(
                "token do Telegram invalido. Deve ser algo como 123456:ABC-DEF... "
                "Poe em TELEGRAM_TOKEN no topo do ficheiro, ou na variavel de "
                "ambiente TELEGRAM_BOT_TOKEN.")
        self.base = f"https://api.telegram.org/bot{tok}"
        self.timeout = timeout
        self.s = requests.Session()

    def _chamar(self, metodo, **carga):
        try:
            r = self.s.post(f"{self.base}/{metodo}", json=carga, timeout=self.timeout)
            corpo = r.json()
        except requests.RequestException as e:
            raise RuntimeError(f"{metodo}: falha de rede: {e}") from e
        except ValueError as e:
            raise RuntimeError(f"{metodo}: resposta nao-JSON") from e
        if not corpo.get("ok"):
            raise RuntimeError(f"{metodo}: {corpo.get('description', corpo)}")
        return corpo.get("result")

    def enviar(self, chat, texto, botoes=None, markdown=True):
        """Com Markdown, e sem ele se o Telegram recusar.

        O parser rebenta com asteriscos e underscores soltos, e parte do texto
        vem de um modelo. Perde-se o negrito; nao se perde a mensagem.
        """
        carga = {"chat_id": chat, "text": texto[:4096], "disable_web_page_preview": True}
        if botoes:
            carga["reply_markup"] = botoes
        if markdown:
            carga["parse_mode"] = "Markdown"
        try:
            return self._chamar("sendMessage", **carga)
        except RuntimeError:
            if not markdown:
                raise
            carga.pop("parse_mode", None)
            return self._chamar("sendMessage", **carga)

    def tirar_botoes(self, chat, msg_id):
        try:
            self._chamar("editMessageReplyMarkup", chat_id=chat, message_id=msg_id,
                         reply_markup={"inline_keyboard": []})
        except RuntimeError:
            pass

    def responder_botao(self, cb_id, texto="", alerta=False):
        try:
            self._chamar("answerCallbackQuery", callback_query_id=cb_id,
                         text=texto[:200], show_alert=alerta)
        except RuntimeError:
            pass

    def atualizacoes(self, offset, timeout):
        carga = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            carga["offset"] = offset
        try:
            corpo = self.s.post(f"{self.base}/getUpdates", json=carga,
                                timeout=timeout + 15).json()
        except (requests.RequestException, ValueError) as e:
            raise RuntimeError(f"getUpdates: {e}") from e
        if not corpo.get("ok"):
            raise RuntimeError(f"getUpdates: {corpo.get('description')}")
        return corpo.get("result", [])

    def quem_sou(self):
        return self._chamar("getMe")


def botoes_aprovacao(eid):
    return {"inline_keyboard": [
        [{"text": "✅ Aplicar (ramo novo)", "callback_data": f"ap:{eid}"},
         {"text": "❌ Descartar", "callback_data": f"rj:{eid}"}],
        [{"text": "🔍 Correr holdout", "callback_data": f"ho:{eid}"}]]}


def botoes_holdout(eid):
    return {"inline_keyboard": [
        [{"text": "Sim, queimar o holdout", "callback_data": f"hc:{eid}"},
         {"text": "Cancelar", "callback_data": f"hx:{eid}"}]]}


class AvisoTelegram:
    def __init__(self, tg: Telegram, chat: int):
        self.tg, self.chat = tg, chat
        self._lock = threading.Lock()

    def enviar(self, texto):
        with self._lock:
            try:
                self.tg.enviar(self.chat, texto)
            except RuntimeError as e:
                log.error("nao consegui enviar: %s", e)

    def pedir_aprovacao(self, texto, eid):
        with self._lock:
            try:
                self.tg.enviar(self.chat, texto, botoes_aprovacao(eid))
            except RuntimeError as e:
                log.error("nao consegui pedir aprovacao: %s", e)


class Bot:
    def __init__(self, estado: Estado, orq: Orquestrador, tg: Telegram, parar=None):
        self.estado, self.orq, self.tg = estado, orq, tg
        self.parar = parar or threading.Event()

    def correr(self):
        gravado = self.estado.kv_ler("offset")
        offset = int(gravado) if gravado else None
        log.info("bot a ouvir")
        while not self.parar.is_set():
            try:
                lote = self.tg.atualizacoes(offset, 30)
            except RuntimeError as e:
                log.warning("getUpdates falhou: %s", e)
                self.parar.wait(5)
                continue
            for u in lote:
                offset = u["update_id"] + 1
                self.estado.kv_gravar("offset", str(offset))
                try:
                    self._tratar(u)
                except Exception:
                    log.exception("erro a tratar update")

    def _autorizado(self, chat) -> bool:
        """Allowlist. Um bot do Telegram e descoberto por acidente mais vezes do
        que se pensa, e do outro lado esta quem sabe mexer no teu codigo."""
        if chat == CHAT_ID:
            return True
        log.warning("ignorado chat nao autorizado: %s", chat)
        self.estado.evento("nao_autorizado", None, chat=chat)
        return False

    def _tratar(self, u):
        if "callback_query" in u:
            return self._botao(u["callback_query"])
        m = u.get("message")
        if not m or "text" not in m:
            return
        if not self._autorizado(m["chat"]["id"]):
            return
        self._texto(m["chat"]["id"], m["text"].strip())

    def _resp(self, chat, texto, botoes=None):
        try:
            self.tg.enviar(chat, texto, botoes)
        except RuntimeError as e:
            log.error("falha a responder: %s", e)

    def _texto(self, chat, texto):
        if not texto.startswith("/"):
            tid = self.estado.nova_tarefa(chat, texto)
            return self._resp(chat, f"📥 Tarefa aceite: `{tid}`\nVou pensar e enfileirar ensaios.")
        cmd, _, arg = texto.partition(" ")
        cmd, arg = cmd.lstrip("/").split("@")[0].lower(), arg.strip()

        if cmd in ("start", "ajuda", "help"):
            self._resp(chat, AJUDA)
        elif cmd in ("estado", "status"):
            self._estado(chat)
        elif cmd == "ensaios":
            self._ensaios(chat)
        elif cmd == "tarefa":
            if arg:
                self._resp(chat, f"📥 Tarefa aceite: `{self.estado.nova_tarefa(chat, arg)}`")
            else:
                self._resp(chat, "Usa: /tarefa <o que queres investigar>")
        elif cmd == "estudo":
            self._novo_estudo(chat, arg)
        elif cmd == "baseline":
            self._baseline(chat)
        elif cmd == "aprovar":
            self._decidir(chat, arg, True)
        elif cmd == "rejeitar":
            self._decidir(chat, arg, False)
        elif cmd == "holdout":
            if arg:
                self._resp(chat, self._aviso_holdout(arg), botoes_holdout(arg))
            else:
                self._resp(chat, "Usa: /holdout <id do ensaio>")
        elif cmd == "parar":
            self._resp(chat, f"🛑 {self.estado.cancelar_fila()} tarefas canceladas. "
                             "O ensaio que ja estava a correr vai ate ao fim.")
        else:
            self._resp(chat, f"Nao conheco /{cmd}. Manda /ajuda.")

    def _estado(self, chat):
        est = self.estado.estudo_aberto()
        linhas = ["*Estado*"]
        if est is None:
            linhas.append("Nenhum estudo aberto. Manda-me uma tarefa.")
        else:
            linhas += [f"Estudo: `{est['id']}`", f"Objetivo: {est['objetivo']}",
                       f"Ensaios: {self.estado.n_ensaios(est['id'])}/{MAX_ENSAIOS_POR_ESTUDO}",
                       f"Baseline: {'definida' if est['baseline'] else 'POR DEFINIR (/baseline)'}"]
        fila = [t for t in self.estado.tarefas(50) if t["estado"] in ("fila", "a_correr")]
        pend = self.estado.por_decidir()
        linhas += [f"Modo: {MODO}", f"Tarefas em curso: {len(fila)}",
                   f"A aguardar decisao tua: {len(pend)}"]
        linhas += [f"  • `{p['id']}`" for p in pend[:5]]
        linhas.append(f"\nHoldout: {HOLDOUT[0]} a {HOLDOUT[1]} — intocado.")
        self._resp(chat, "\n".join(linhas))

    def _ensaios(self, chat):
        est = self.estado.estudo_aberto()
        lista = self.estado.ensaios(est["id"] if est else None, 10)
        if not lista:
            return self._resp(chat, "Ainda nao ha ensaios.")
        linhas = ["*Ultimos ensaios*"]
        for e in lista:
            if e["estado"] != "feito":
                linhas.append(f"`{e['id']}` — {e['estado']}")
                continue
            m = json.loads(e["metricas"]) if e["metricas"] else {}
            v = json.loads(e["veredito"]) if e["veredito"] else {}
            s = m.get("validacao", {}).get("sharpe_anual")
            linhas.append(f"{'🟢' if v.get('passou') else '🔴'} `{e['id']}` "
                          f"Sharpe OOS {s:.2f} DSR {v.get('dsr', 0):.2f} [{e['aprovacao']}]"
                          if s is not None else f"`{e['id']}` sem metricas")
        self._resp(chat, "\n".join(linhas))

    def _novo_estudo(self, chat, arg):
        if not arg:
            return self._resp(chat, "Usa: /estudo <objetivo do novo estudo>")
        atual = self.estado.estudo_aberto()
        if atual:
            self.estado.fechar_estudo(atual["id"], "fechado por ti")
        eid = self.estado.criar_estudo(arg)
        self._resp(chat, f"📚 Estudo novo: `{eid}`\nContagem de ensaios reiniciada.\n\n"
                         "Reiniciar a contagem so e honesto se a hipotese for mesmo nova. "
                         "Se e a mesma busca a continuar, o DSR do estudo novo esta a "
                         "mentir-te por omissao.")

    def _baseline(self, chat):
        est = self.estado.estudo_aberto()
        if est is None:
            return self._resp(chat, "Nao ha estudo aberto. Manda-me uma tarefa primeiro.")
        self._resp(chat, "⏳ A medir a baseline na janela de validacao...")
        try:
            j = self.orq.medir_baseline(est["id"])
        except (ErroSandbox, ValueError) as e:
            return self._resp(chat, f"⚠️ Baseline falhou: {e}")
        self._resp(chat, f"📏 *Baseline definida*\nSharpe OOS: {j.sharpe_anual:.2f}\n"
                         f"Drawdown: {j.drawdown * 100:.1f}%\nTrades: {j.trades}\n\n"
                         f"E este numero que qualquer proposta tem de bater em pelo menos "
                         f"{MIN_MELHORIA_PCT:.0f}%.")

    def _aviso_holdout(self, eid) -> str:
        return (f"⚠️ *Correr o holdout em* `{eid}`\n\nJanela: {HOLDOUT[0]} a {HOLDOUT[1]}\n\n"
                "Isto so se faz uma vez. A partir do momento em que olhas para o "
                "resultado, o holdout passou a fazer parte da tua escolha e deixa de ser "
                "uma medida independente. Nao ha como o repor.\n\n"
                "So o faz quando ja tiveres decidido que e este o candidato.")

    def _decidir(self, chat, eid, aprovar):
        if not eid:
            return self._resp(chat, "Usa: /aprovar <id> ou /rejeitar <id>")
        e = self.estado.ensaio(eid)
        if e is None:
            return self._resp(chat, f"Nao encontro `{eid}`.")
        if e["aprovacao"] not in ("pendente", "aprovado"):
            return self._resp(chat, f"`{eid}` nao esta a aguardar decisao ({e['aprovacao']}).")
        if not aprovar:
            self.estado.aprovar(eid, "rejeitado")
            return self._resp(chat, f"❌ `{eid}` descartado.")
        self.estado.aprovar(eid, "aprovado")
        try:
            ramo = self.orq.aplicar_aprovado(eid)
        except (ErroSandbox, ValueError) as exc:
            return self._resp(chat, f"⚠️ Nao consegui aplicar: {exc}")
        self._resp(chat, f"✅ Escrito no ramo `{ramo}`.\n\nNao fiz merge. Ve o diff e decide:\n"
                         f"`git diff main..{ramo}`\n`git merge {ramo}`")

    def _botao(self, cb):
        chat, msg_id, cb_id = cb["message"]["chat"]["id"], cb["message"]["message_id"], cb["id"]
        if not self._autorizado(chat):
            return self.tg.responder_botao(cb_id, "Nao autorizado.", True)
        acao, _, eid = cb.get("data", "").partition(":")
        if acao == "ap":
            self.tg.responder_botao(cb_id, "A aplicar...")
            self.tg.tirar_botoes(chat, msg_id)
            self._decidir(chat, eid, True)
        elif acao == "rj":
            self.tg.responder_botao(cb_id, "Descartado.")
            self.tg.tirar_botoes(chat, msg_id)
            self._decidir(chat, eid, False)
        elif acao == "ho":
            self.tg.responder_botao(cb_id)
            self._resp(chat, self._aviso_holdout(eid), botoes_holdout(eid))
        elif acao == "hx":
            self.tg.responder_botao(cb_id, "Cancelado.")
            self.tg.tirar_botoes(chat, msg_id)
        elif acao == "hc":
            self.tg.responder_botao(cb_id, "A correr o holdout...")
            self.tg.tirar_botoes(chat, msg_id)
            self._holdout(chat, eid)
        else:
            self.tg.responder_botao(cb_id, "Botao desconhecido.")

    def _holdout(self, chat, eid):
        try:
            j = self.orq.correr_holdout(eid)
        except (ErroSandbox, ValueError) as e:
            return self._resp(chat, f"⚠️ Holdout nao correu: {e}")
        e_row = self.estado.ensaio(eid)
        val = json.loads(e_row["metricas"]).get("validacao", {}) if e_row["metricas"] else {}
        antes = val.get("sharpe_anual", 0.0)
        queda = j.sharpe_anual - antes
        self._resp(chat, f"🔍 *Holdout de* `{eid}` *(queimado)*\n"
                         f"Sharpe: {j.sharpe_anual:.2f} (validacao era {antes:.2f}, {queda:+.2f})\n"
                         f"Drawdown: {j.drawdown * 100:.1f}%\nTrades: {j.trades}\n\n"
                         f"Leitura: {'aguentou' if queda > -0.5 else 'caiu face a validacao — desconfia'}.\n\n"
                         f"Este holdout esta gasto. Para outra medicao independente precisas "
                         f"de dados que ainda nao existiam quando fizeste esta busca.")


# ===========================================================================
#  DOCTOR — verifica tudo antes de arrancar
# ===========================================================================

def doctor() -> int:
    problemas = 0

    def ok(m):
        print(f"  ✅ {m}")

    def erro(m):
        nonlocal problemas
        problemas += 1
        print(f"  ❌ {m}")

    def aviso(m):
        print(f"  ⚠️  {m}")

    print("\nTelegram")
    if not token():
        erro("sem token. Poe em TELEGRAM_TOKEN no topo, ou em TELEGRAM_BOT_TOKEN.")
    else:
        try:
            eu = Telegram(token(), timeout=15).quem_sou()
            ok(f"ligado como @{eu.get('username')}")
        except (ValueError, RuntimeError) as e:
            erro(str(e))
    ok(f"chat autorizado: {CHAT_ID}")

    print("\nProjeto")
    p = Path(PROJETO)
    problema_caminho = diagnosticar_caminho(PROJETO)
    if problema_caminho:
        erro(f"PROJETO — {problema_caminho}")
    elif not p.is_dir():
        erro(f"PROJETO nao existe: {p}")
    elif not e_repo_git(p):
        erro(f"{p} nao e repositorio git (faz `git init` + commit)")
    else:
        ok(f"{p} e repositorio git")
        (ok if (p / FICHEIRO_PARAMS).is_file() else aviso)(
            f"FICHEIRO_PARAMS: {FICHEIRO_PARAMS}" +
            ("" if (p / FICHEIRO_PARAMS).is_file() else " (em falta — /baseline nao funciona)"))

    print("\nIsolamento")
    if BACKTEST_COM_REDE:
        aviso("BACKTEST_COM_REDE = True — o backtest tem acesso a rede")
    elif sem_rede_disponivel():
        ok("o backtest corre sem rede (unshare disponivel)")
    else:
        aviso("nao consigo cortar a rede neste sistema; o backtest vai ter acesso")

    print("\nProtocolo")
    ok(f"treino     {TREINO[0]} → {TREINO[1]}")
    ok(f"validacao  {VALIDACAO[0]} → {VALIDACAO[1]}")
    ok(f"holdout    {HOLDOUT[0]} → {HOLDOUT[1]}  (intocado)")
    if not (TREINO[1] < VALIDACAO[0] < HOLDOUT[0]):
        erro("as janelas tem de ser cronologicas e sem sobreposicao. Um holdout que "
             "se sobrepoe ao treino nao e um holdout.")
    ok(f"orcamento  {MAX_ENSAIOS_POR_ESTUDO} ensaios por estudo")

    print(f"\nModo: {MODO}")
    if MODO == "code":
        if not FICHEIROS_EDITAVEIS:
            erro("FICHEIROS_EDITAVEIS vazio: o modo code nao pode arrancar assim. Sem "
                 "lista branca o agente pode editar o codigo que calcula as metricas.")
        elif e_repo_git(p):
            try:
                with Sandbox("doctor") as sb:
                    todos = sb.ficheiros_versionados()
                editaveis = [f for f in todos if caminho_permitido(f, FICHEIROS_EDITAVEIS)]
                protegidos = [f for f in todos if f not in editaveis]
                if not editaveis:
                    erro("nenhum ficheiro versionado corresponde a FICHEIROS_EDITAVEIS")
                else:
                    ok(f"{len(editaveis)} ficheiro(s) ao alcance do agente")
                    for f in editaveis[:8]:
                        print(f"      ✏️  {f}")
                    print(f"      🔒 {len(protegidos)} protegido(s), entre eles:")
                    for f in protegidos[:5]:
                        print(f"         {f}")
            except Exception as e:
                erro(f"nao consegui listar os ficheiros: {e}")
        (ok if COMANDO_TESTES else aviso)(
            f"testes do projeto: {COMANDO_TESTES}" if COMANDO_TESTES else
            "sem COMANDO_TESTES: codigo partido so vai ser apanhado pelo backtest")
        ok(f"limite por proposta: {MAX_LINHAS_EDICAO} linhas")
    else:
        if not PARAMETROS:
            erro("PARAMETROS vazio: nao ha nada para o agente propor")
        for n, e in PARAMETROS.items():
            ok(f"{n}: {e['tipo']} [{e['min']:g}, {e['max']:g}]")

    print("\nModelos")
    disponiveis = Ollama().modelos()
    if not disponiveis:
        erro(f"o Ollama nao respondeu em {OLLAMA_URL} (esta a correr?)")
    else:
        ok(f"Ollama tem: {', '.join(disponiveis)}")
        precisos = [("pesquisa", MODELO_PESQUISA), ("relatorio", MODELO_RELATORIO)]
        precisos.append(("desenvolvimento", MODELO_DESENVOLVIMENTO) if MODO == "code"
                        else ("params", MODELO_PARAMS))
        for papel, m in precisos:
            if m in disponiveis or any(d.startswith(m) for d in disponiveis):
                nuvem = " (na nuvem — o teu codigo sai da maquina)" if ":cloud" in m else ""
                ok(f"{papel}: {m}{nuvem}")
            else:
                erro(f"{papel}: {m} nao esta instalado (`ollama pull {m}`)")

    print(f"\n{'Tudo pronto.' if problemas == 0 else f'{problemas} problema(s) a resolver.'}\n")
    return 0 if problemas == 0 else 1


# ===========================================================================
#  CONFIGURAR — olha para o teu projeto e preenche-se a si proprio
# ===========================================================================

# Nomes que denunciam o que cada ficheiro faz. Nao e adivinhacao cega: e uma
# proposta que tu confirmas, e o programa diz sempre no que nao teve certeza.
PISTAS_ARNES = ("metric", "backtest", "resultado", "score", "avalia", "engine",
                "simula", "executa", "dados", "data", "loader", "carrega", "test")
PISTAS_ESTRATEGIA = ("estrateg", "strateg", "sinal", "signal", "indicador",
                     "indicator", "regra", "rule", "entrada", "entry", "setup",
                     "risco", "risk", "filtro", "filter")

# Como os argumentos do teu script se mapeiam nos meus marcadores.
MAPA_ARGUMENTOS = {
    "{params}": ("params", "parametros", "config", "cfg", "parameters"),
    "{inicio}": ("start", "inicio", "start-date", "data-inicio", "from", "de"),
    "{fim}": ("end", "fim", "end-date", "data-fim", "to", "ate"),
    "{saida}": ("out", "output", "saida", "resultado", "metrics", "metricas", "o"),
}


def _ficheiros_python(projeto: Path) -> list[Path]:
    return [c for c in sorted(projeto.rglob("*.py"))
            if not any(parte in (".git", "__pycache__", ".venv", "venv", "worktrees",
                                 "node_modules", ".orq")
                       for parte in c.parts)
            and c.name != Path(__file__).name]


def _detetar_entrada(projeto: Path) -> tuple[Path | None, str | None]:
    """Descobre o script que corre o backtest e monta o comando a partir dos
    argumentos que ele proprio declara."""
    candidatos = []
    for caminho in _ficheiros_python(projeto):
        try:
            texto = caminho.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "__main__" not in texto or "add_argument" not in texto:
            continue
        nome = caminho.stem.lower()
        pontos = sum(30 for p in ("run_backtest", "backtest", "run", "main") if p in nome)
        pontos -= 20 * len(caminho.relative_to(projeto).parts[:-1])   # raiz vale mais
        candidatos.append((pontos, caminho, texto))
    if not candidatos:
        return None, None

    _, entrada, texto = max(candidatos, key=lambda c: c[0])
    flags = re.findall(r"add_argument\(\s*['\"]--([a-zA-Z0-9_-]+)", texto)
    partes, em_falta = [], []
    for marcador, alternativas in MAPA_ARGUMENTOS.items():
        achado = next((f for f in flags
                       if f.lower().replace("_", "-") in alternativas), None)
        if achado:
            partes.append(f"--{achado} {marcador}")
        else:
            em_falta.append(marcador)

    rel = entrada.relative_to(projeto).as_posix()
    comando = f"python {rel} " + " ".join(partes)
    return entrada, (comando if not em_falta else comando + "   # FALTAM: " + " ".join(em_falta))


def _classificar(projeto: Path) -> tuple[list[str], list[str], list[str]]:
    """Separa estrategia de arnes. Devolve (estrategia, arnes, duvidosos)."""
    estrategia, arnes, duvidosos = [], [], []
    for caminho in _ficheiros_python(projeto):
        rel = caminho.relative_to(projeto).as_posix()
        alvo = rel.lower()
        e_estrategia = any(p in alvo for p in PISTAS_ESTRATEGIA)
        e_arnes = any(p in alvo for p in PISTAS_ARNES)
        if e_estrategia and not e_arnes:
            estrategia.append(rel)
        elif e_arnes:
            arnes.append(rel)
        else:
            duvidosos.append(rel)
    return estrategia, arnes, duvidosos


def _agrupar(caminhos: list[str]) -> list[str]:
    """Se todos os ficheiros estao na mesma pasta, a lista branca e a pasta."""
    if not caminhos:
        return []
    pastas = {c.rsplit("/", 1)[0] for c in caminhos if "/" in c}
    if len(pastas) == 1 and all("/" in c for c in caminhos):
        return sorted(pastas)
    return sorted(caminhos)


def _substituir_constante(texto: str, nome: str, valor: str) -> tuple[str, bool]:
    padrao = re.compile(rf"^{nome} = .*$", re.MULTILINE)
    if not padrao.search(texto):
        return texto, False
    return padrao.sub(f"{nome} = {valor}", texto, count=1), True


def cmd_configurar(escrever: bool) -> int:
    projeto = Path(PROJETO)
    problema = diagnosticar_caminho(PROJETO)
    if problema:
        print(f"\n❌ PROJETO — {problema}\n")
        return 1
    if not projeto.is_dir():
        print(f"\n❌ A pasta {PROJETO} nao existe.\n"
              f"   Corrige PROJETO no topo do ficheiro e corre outra vez.\n")
        return 1

    print(f"\nA olhar para {projeto}\n")
    entrada, comando = _detetar_entrada(projeto)
    estrategia, arnes, duvidosos = _classificar(projeto)
    editaveis = _agrupar(estrategia)

    if entrada:
        print(f"  Script de backtest : {entrada.relative_to(projeto).as_posix()}")
    else:
        print("  Script de backtest : NAO ENCONTRADO")
        print("     Procurei um .py com `__main__` e `add_argument`. Se o teu")
        print("     backtest corre de outra maneira, escreve o COMANDO_BACKTEST a mao.")

    print(f"\n  ✏️  Proponho como EDITAVEL ({len(estrategia)} ficheiro(s)):")
    for f in estrategia or ["   (nenhum — ver abaixo)"]:
        print(f"        {f}")
    print(f"\n  🔒 Fica PROTEGIDO ({len(arnes)}):")
    for f in arnes[:12]:
        print(f"        {f}")
    if duvidosos:
        print(f"\n  ❓ Nao consegui classificar ({len(duvidosos)}):")
        for f in duvidosos[:12]:
            print(f"        {f}")
        print("     Ficam de fora da lista branca. Se algum deles for estrategia,")
        print("     acrescenta-o a mao a FICHEIROS_EDITAVEIS.")

    if not estrategia:
        print("\n❌ Nao identifiquei nenhum ficheiro de estrategia pelo nome.")
        print("   Sem lista branca o modo `code` nao arranca — e ainda bem, porque")
        print("   um agente sem lista branca pode reescrever o codigo que o avalia.")
        print("   Escreve tu FICHEIROS_EDITAVEIS com os teus ficheiros de estrategia.")

    pastas_dados = sorted({d.name for d in projeto.iterdir()
                           if d.is_dir() and d.name.lower() in
                           ("dados", "data", "csv", "series", "historico", "cache")})

    print("\n" + "─" * 62)
    print("Proposta de configuracao:\n")
    linhas = []
    if comando:
        linhas.append(f'COMANDO_BACKTEST = "{comando}"')
    if editaveis:
        linhas.append(f"FICHEIROS_EDITAVEIS = {editaveis!r}")
    if pastas_dados:
        linhas.append(f"PASTAS_LIGADAS = {pastas_dados!r}")
    for l in linhas:
        print(f"  {l}")

    if not escrever:
        print("\nPara eu escrever isto no ficheiro:")
        print(f"  python {Path(__file__).name} configurar --escrever\n")
        return 0

    origem = Path(__file__)
    texto = origem.read_text(encoding="utf-8")
    copia = origem.with_suffix(".py.bak")
    copia.write_text(texto, encoding="utf-8")

    aplicadas = []
    for nome, valor in (("COMANDO_BACKTEST", f'"{comando}"' if comando else None),
                        ("FICHEIROS_EDITAVEIS", repr(editaveis) if editaveis else None),
                        ("PASTAS_LIGADAS", repr(pastas_dados) if pastas_dados else None)):
        if valor is None:
            continue
        texto, ok = _substituir_constante(texto, nome, valor)
        if ok:
            aplicadas.append(nome)
    origem.write_text(texto, encoding="utf-8")

    print(f"\n✅ Escrevi {len(aplicadas)} definicao(oes): {', '.join(aplicadas)}")
    print(f"   Copia do ficheiro anterior em {copia.name}")
    if comando and "FALTAM" in comando:
        print("\n⚠️  O COMANDO_BACKTEST ficou incompleto — o teu script nao declara")
        print("   todos os argumentos de que preciso. Ve a linha e completa-a.")
    print(f"\nAgora:  python {origem.name} doctor\n")
    return 0


# ===========================================================================
#  AUTOTESTE — prova que o ficheiro funciona, sem Ollama e sem Telegram
# ===========================================================================

PROJETO_FALSO = '''\
import argparse, json, math, pathlib, random, sys
sys.path.insert(0, str(pathlib.Path(__file__).parent))
from estrategia.sinal import forca

ap = argparse.ArgumentParser()
for f in ("params", "start", "end", "out"):
    ap.add_argument(f"--{f}", required=True)
a = ap.parse_args()
p = json.loads(pathlib.Path(a.params).read_text())
# Optimo em sma_fast=20; fora disso o retorno esperado fica negativo.
d = abs(p.get("sma_fast", 10) - 20) / 20.0
mu = 0.0010 * (1.0 - d) * forca()
if a.start < "2022-01-01":
    mu *= 1.4
rng = random.Random(int(round(mu, 6) * 10**7) + sum(ord(c) for c in a.start))
rets = [rng.gauss(mu, 0.01) for _ in range(600)]
eq, pico, dd = 1.0, 1.0, 0.0
for r in rets:
    eq *= 1 + r
    pico = max(pico, eq)
    dd = max(dd, (pico - eq) / pico)
pathlib.Path(a.out).write_text(json.dumps({
    "returns": rets, "trades": 420, "max_drawdown": dd,
    "periods_per_year": 252, "janela": [a.start, a.end]}))
print("backtest ok", a.start, a.end)
'''

ESTRATEGIA_FALSA = '''\
def forca():
    """Multiplicador do sinal. E este ficheiro que o agente pode alterar."""
    return 1.0
'''


def autoteste() -> int:
    """Monta um projeto de mentira e corre o ciclo completo com um modelo guionado."""
    global PROJETO, WORKTREES, BD, COMANDO_BACKTEST, COMANDO_TESTES
    global FICHEIROS_EDITAVEIS, PASTAS_LIGADAS, MODO, MIN_DSR

    falhas = []

    def verificar(condicao, descricao):
        print(f"  {'✅' if condicao else '❌'} {descricao}")
        if not condicao:
            falhas.append(descricao)

    tmp = Path(tempfile.mkdtemp(prefix="orq_autoteste_"))
    projeto = tmp / "projeto"
    (projeto / "estrategia").mkdir(parents=True)
    (projeto / "data").mkdir()
    (projeto / "run_backtest.py").write_text(PROJETO_FALSO, encoding="utf-8")
    (projeto / "estrategia" / "__init__.py").write_text("", encoding="utf-8")
    (projeto / "estrategia" / "sinal.py").write_text(ESTRATEGIA_FALSA, encoding="utf-8")
    (projeto / "params.json").write_text('{"sma_fast": 8}\n', encoding="utf-8")
    (projeto / ".gitignore").write_text("data/\n", encoding="utf-8")
    git(projeto, "init", "-q")
    git(projeto, "add", "run_backtest.py", "estrategia", "params.json", ".gitignore")
    git(projeto, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "inicial")

    PROJETO = str(projeto)
    WORKTREES = tmp / "wt"
    BD = tmp / "orq.db"
    COMANDO_BACKTEST = ("python3 run_backtest.py --params {params} --start {inicio} "
                        "--end {fim} --out {saida}")
    COMANDO_TESTES = ""      # exemplo: "python3 -m unittest discover -s testes -t ."
    FICHEIROS_EDITAVEIS = ["estrategia"]
    PASTAS_LIGADAS = ["data"]
    MODO = "code"
    MIN_DSR = 0.90

    print("\n=== 1. Contas: o Deflated Sharpe aperta com o numero de ensaios ===")
    rng = random.Random(7)
    serie = [rng.gauss(0.0008, 0.01) for _ in range(1260)]
    sr = sharpe(serie)
    valores = [dsr(sr, len(serie), n, 0.0004, assimetria(serie), curtose(serie))
               for n in (1, 10, 100, 1000)]
    for n, v in zip((1, 10, 100, 1000), valores):
        print(f"     Sharpe anual {sr*252**0.5:.2f} apos {n:>4} ensaios -> DSR {v:.3f}")
    verificar(valores == sorted(valores, reverse=True), "o DSR cai a medida que os ensaios sobem")
    verificar(sharpe([0.01] * 100) == 0.0, "serie constante da Sharpe 0 (e nao 1e15)")

    print("\n=== 2. Lista branca ===")
    verificar(caminho_permitido("estrategia/sinal.py", ["estrategia"]), "estrategia/ e editavel")
    verificar(not caminho_permitido("run_backtest.py", ["estrategia"]),
              "run_backtest.py esta protegido")
    verificar(not caminho_permitido("a/run_backtest.py", ["*.py"]),
              "o padrao *.py nao atravessa pastas")
    verificar(not caminho_permitido("../../etc/passwd", ["estrategia"]),
              "nao da para escapar da raiz")

    print("\n=== 3. Edicoes de codigo ===")
    verificar(aplicar_edicoes("a\nb\nc\n", [{"procurar": "b", "substituir": "B"}]) == "a\nB\nc\n",
              "uma edicao simples aplica")
    for descricao, edicoes, conteudo, esperado in [
        ("bloco inexistente da erro util", [{"procurar": "z", "substituir": "Z"}], "a\nb\n", "nao aparece"),
        ("bloco ambiguo pede contexto", [{"procurar": "x", "substituir": "y"}], "x\nx\nx\n", "aparece 3 vezes"),
    ]:
        try:
            aplicar_edicoes(conteudo, edicoes)
            verificar(False, descricao)
        except ErroEdicao as exc:
            verificar(esperado in str(exc), descricao)

    print("\n=== 4. JSON vindo de respostas sujas ===")
    for texto, esperado, descricao in [
        ('{"a": 1}', {"a": 1}, "JSON limpo"),
        ('Claro!\n```json\n{"a": 2}\n```\nEspero que ajude', {"a": 2}, "cerca markdown com tagarelice"),
        ('proponho {"a": 3} porque sim', {"a": 3}, "JSON no meio de prosa"),
        ('{"t": "aspas \\" e {chaveta}", "n": 4}', {"t": 'aspas " e {chaveta}', "n": 4},
         "aspas e chavetas dentro de string"),
    ]:
        try:
            verificar(extrair_json(texto) == esperado, descricao)
        except ErroModelo:
            verificar(False, descricao)

    print("\n=== 5. O agente de desenvolvimento ===")
    fich = {"estrategia/sinal.py": "def forca():\n    return 1.0\n"}
    hip = {"nome": "dobrar a forca", "raciocinio": "o filtro corta demais"}

    def _prop(ficheiro="estrategia/sinal.py", procurar="    return 1.0", substituir="    return 2.0"):
        return json.dumps({"ficheiro": ficheiro,
                           "edicoes": [{"procurar": procurar, "substituir": substituir}]})

    falso = ModeloFalso([_prop(ficheiro="metricas.py", procurar="sharpe"), _prop()])
    saida = propor_alteracao(fich, hip, editaveis=["estrategia"], llm=falso,
                             modelo="falso", tentativas=3)
    verificar(saida["ficheiro"] == "estrategia/sinal.py",
              "tentativa de editar metricas.py recusada e corrigida")
    verificar("intocaveis" in falso.chamadas[1]["utilizador"],
              "o modelo recebeu o motivo concreto da recusa")

    falso = ModeloFalso([_prop(substituir="\n".join(f"l{i}" for i in range(60)))] * 3)
    try:
        propor_alteracao(fich, hip, editaveis=["estrategia"], llm=falso, modelo="falso",
                         max_linhas=20, tentativas=2)
        verificar(False, "alteracao desproporcionada devia ser recusada")
    except ErroAgente:
        verificar("o maximo e 20" in falso.chamadas[1]["utilizador"],
                  "alteracao desproporcionada recusada com o limite explicito")

    print("\n=== 6. Sandbox ===")
    with Sandbox("teste") as sb:
        verificar((sb.raiz / "estrategia" / "sinal.py").is_file(), "worktree criado")
        verificar((sb.raiz / "data").is_symlink(), "dados nao versionados ligados")
        vistos = sb.ler_editaveis()
        verificar("run_backtest.py" not in vistos, "o arnes nunca e mostrado ao agente")
        (sb.raiz / "estrategia" / "sinal.py").write_text("estragado", encoding="utf-8")
        ok_, det = sb.aplicar("run_backtest.py", [{"procurar": "import", "substituir": "x"}])
        verificar(not ok_, "o sandbox recusa editar fora da lista branca")
        try:
            sb.backtest({}, "2015-01-01", "2025-01-01")
            verificar(False, "a guarda de holdout devia ter disparado")
        except ViolacaoHoldout:
            verificar(True, "ensaio automatico nao consegue tocar no holdout")
    original = (projeto / "estrategia" / "sinal.py").read_text(encoding="utf-8")
    verificar("estragado" not in original, "o projeto original ficou intacto")

    print("\n=== 7. Ciclo completo (tarefa -> agentes -> gate -> aprovacao) ===")
    pesquisa = json.dumps({"hipoteses": [
        {"nome": "Dobrar a forca do sinal", "raciocinio": "o filtro corta demais",
         "direcao": "aumentar"}]})
    # A primeira resposta tenta editar o arnes: tem de ser recusada e corrigida.
    ataque = json.dumps({"ficheiro": "run_backtest.py",
                         "edicoes": [{"procurar": '"trades": 420', "substituir": '"trades": 99999'}]})
    correcao = json.dumps({"ficheiro": "estrategia/sinal.py",
                           "edicoes": [{"procurar": "    return 1.0", "substituir": "    return 2.0"}],
                           "justificacao": "duplica a forca"})
    modelo = ModeloFalso([pesquisa, ataque, correcao, "Resultado consistente."])

    estado = Estado(BD)
    aviso = Aviso()
    orq = Orquestrador(estado, modelo, aviso)
    est = orq.garantir_estudo("objetivo de teste")
    orq.medir_baseline(est["id"])
    estado.nova_tarefa(CHAT_ID, "aumentar o retorno mexendo no sinal")

    worker = Worker(orq, estado, pausa=0)
    passos = 0
    while worker.passo() and passos < 20:
        passos += 1

    tentou_arnes = any("intocaveis" in c["utilizador"] for c in modelo.chamadas)
    verificar(tentou_arnes, "a tentativa de editar o arnes foi recusada e explicada ao modelo")

    ensaios = estado.ensaios(est["id"], 10)
    verificar(len(ensaios) == 1 and ensaios[0]["estado"] == "feito",
              f"o ensaio correu ate ao fim ({ensaios[0]['erro'] if ensaios else 'nenhum ensaio'})")
    alteracao = json.loads(ensaios[0]["alteracao"])
    verificar(alteracao["ficheiro"] == "estrategia/sinal.py",
              "a alteracao ficou no ficheiro de estrategia")
    verificar(bool(aviso.aprovacoes), "a proposta chegou para aprovacao")

    print("\n=== 8. Aprovar cria um ramo, sem fazer merge ===")
    eid = aviso.aprovacoes[0][0]
    estado.aprovar(eid, "aprovado")
    ramo = orq.aplicar_aprovado(eid)
    no_ramo = subprocess.run(["git", "-C", str(projeto), "show", f"{ramo}:estrategia/sinal.py"],
                             capture_output=True, text=True, check=False).stdout
    verificar("return 2.0" in no_ramo, f"a alteracao esta no ramo {ramo}")
    verificar("return 1.0" in (projeto / "estrategia" / "sinal.py").read_text(encoding="utf-8"),
              "o ficheiro vivo NAO foi alterado (nao ha merge automatico)")

    print("\n=== 9. Holdout: uma vez e so uma ===")
    orq.correr_holdout(eid)
    verificar(estado.ensaio(eid)["holdout"] is not None, "o holdout correu por ordem expressa")
    try:
        orq.correr_holdout(eid)
        verificar(False, "a segunda corrida devia ter sido recusada")
    except ValueError:
        verificar(True, "a segunda corrida do holdout foi recusada")

    print("\n=== 10. Sobrevive a um crash ===")
    ens = estado.novo_ensaio(est["id"], {})
    estado.reclamar_ensaio()
    estado.c.execute("UPDATE ensaios SET pulso=? WHERE id=?", (time.time() - 99999, ens))
    verificar(estado.recuperar(60)["ensaios"] == 1, "o que ficou preso voltou a fila")

    estado.fechar()
    shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 60)
    if falhas:
        print(f"❌ {len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"   - {f}")
        return 1
    print("✅ Tudo a funcionar. Podes configurar o topo do ficheiro e correr `doctor`.")
    return 0


# ===========================================================================
#  ARRANQUE
# ===========================================================================

def correr(com_bot=True, com_worker=True) -> int:
    if not token():
        print("\n❌ Sem token do Telegram. Preenche TELEGRAM_TOKEN no topo do ficheiro,\n"
              "   ou define a variavel de ambiente TELEGRAM_BOT_TOKEN.\n", file=sys.stderr)
        return 2
    parar = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: (print("\na parar..."), parar.set()))
    signal.signal(signal.SIGTERM, lambda *a: parar.set())

    tg = Telegram(token())
    aviso = AvisoTelegram(tg, CHAT_ID)
    threads = []
    if com_worker:
        # Uma ligacao SQLite nao atravessa threads: cada uma tem a sua.
        e1 = Estado(BD)
        threads.append(threading.Thread(
            target=Worker(Orquestrador(e1, Ollama(), aviso), e1, parar=parar).correr,
            name="worker", daemon=True))
    if com_bot:
        e2 = Estado(BD)
        threads.append(threading.Thread(
            target=Bot(e2, Orquestrador(e2, Ollama(), aviso), tg, parar=parar).correr,
            name="bot", daemon=True))
    for t in threads:
        t.start()
    print(f"a correr: {', '.join(t.name for t in threads)} — Ctrl+C para parar")
    try:
        while not parar.is_set() and any(t.is_alive() for t in threads):
            parar.wait(1)
    except KeyboardInterrupt:
        parar.set()
    for t in threads:
        t.join(timeout=10)
    return 0


def estado_cli() -> int:
    with Estado(BD) as e:
        est = e.estudo_aberto()
        if est is None:
            print("Nenhum estudo aberto.")
        else:
            print(f"Estudo:   {est['id']}\nObjetivo: {est['objetivo']}")
            print(f"Ensaios:  {e.n_ensaios(est['id'])}/{MAX_ENSAIOS_POR_ESTUDO}")
            print(f"Baseline: {'definida' if est['baseline'] else 'POR DEFINIR'}")
        pend = e.por_decidir()
        print(f"A aguardar decisao: {len(pend)}")
        for p in pend:
            print(f"  {p['id']}")
    return 0


def cmd_ver(projeto: Path, editaveis: Sequence[str]) -> int:
    """Mostra o que este programa consegue e nao consegue tocar.

    Vale a pena correr isto antes de ligar o agente. E a forma mais rapida de
    descobrir que a lista branca esta mal desenhada — por exemplo, que o
    ficheiro de metricas esta dentro da pasta editavel.
    """
    ficheiros = listar_editaveis(projeto, editaveis)
    print(f"\nProjeto: {projeto}")
    print(f"Lista branca: {', '.join(editaveis)}\n")

    if not ficheiros:
        print("  ❌ Nenhum ficheiro corresponde a lista branca.")
        print("     O agente nao tem onde mexer. Confere os caminhos.")
        return 1

    print(f"  ✏️  EDITAVEIS ({len(ficheiros)}):")
    for rel in sorted(ficheiros):
        print(f"        {rel}  ({len(ficheiros[rel].splitlines())} linhas)")

    protegidos = []
    for caminho in sorted(Path(projeto).rglob("*.py")):
        if "__pycache__" in caminho.parts or ".git" in caminho.parts:
            continue
        rel = caminho.relative_to(projeto).as_posix()
        if rel not in ficheiros:
            protegidos.append(rel)
    if protegidos:
        print(f"\n  🔒 PROTEGIDOS ({len(protegidos)}):")
        for rel in protegidos[:15]:
            print(f"        {rel}")

    suspeitos = [r for r in ficheiros if any(
        p in r.lower() for p in ("metric", "backtest", "resultado", "score", "avalia"))]
    if suspeitos:
        print("\n  ⚠️  Estes ficheiros editaveis tem nomes que sugerem que calculam")
        print(f"      resultados: {', '.join(suspeitos)}")
        print("      Se algum deles mede o desempenho, tira-o da lista branca. Um")
        print("      agente que pode reescrever a regua vai reescrever a regua.")
    return 0



ESCAPES_ACIDENTAIS = {"\t": "\\t", "\n": "\\n", "\r": "\\r",
                      "\b": "\\b", "\f": "\\f", "\v": "\\v", "\a": "\\a"}


def diagnosticar_caminho(caminho: str) -> str | None:
    """Deteta o erro classico de escrever caminhos do Windows em Python.

    `"C:\\codigo\\teste backtest"` sem o prefixo `r` faz o `\\t` virar um TAB. O
    caminho resultante nao existe e a mensagem de erro so diz "nao existe", o
    que manda a pessoa procurar no sitio errado — na pasta, em vez de na linha
    que a escreveu.
    """
    encontrados = [nome for char, nome in ESCAPES_ACIDENTAIS.items() if char in caminho]
    if not encontrados:
        return None
    return (f"o caminho tem caracteres de escape ({', '.join(encontrados)}), o que "
            f"quer dizer que as barras invertidas foram interpretadas.\n"
            f"     Poe um `r` antes das aspas:  PROJETO = r\"C:\\...\"\n"
            f"     ou usa barras normais:       PROJETO = \"C:/...\"")


def pronto_para_arrancar() -> list[str]:
    """O que falta configurar. Lista vazia = pode arrancar."""
    faltas = []
    if not token():
        faltas.append("TELEGRAM_TOKEN — o token do teu bot (@BotFather no Telegram)")
    if not CHAT_ID:
        faltas.append("CHAT_ID — o teu chat (fala com @userinfobot para o saberes)")
    problema = diagnosticar_caminho(PROJETO)
    projeto = Path(PROJETO)
    if problema:
        faltas.append(f"PROJETO — {problema}")
    elif not projeto.is_dir():
        faltas.append(f"PROJETO — a pasta {PROJETO} nao existe")
    elif not e_repo_git(projeto):
        faltas.append(f"PROJETO — {PROJETO} tem de ser um repositorio git "
                      f'(cd para la e corre: git init && git add -A && git commit -m inicial)')
    if MODO == "code" and not FICHEIROS_EDITAVEIS:
        faltas.append("FICHEIROS_EDITAVEIS — sem lista branca o agente podia reescrever "
                      "o codigo que calcula as metricas")
    return faltas


def avisar_o_que_falta(faltas: list[str]) -> int:
    """Antes de arrancar, dizer exatamente o que preencher — e onde."""
    print(f"""
Falta configurar {len(faltas)} coisa(s) no topo deste ficheiro
({Path(__file__).name}, na seccao CONFIGURACAO):
""")
    for f in faltas:
        print(f"  ❌ {f}")
    print(f"""
Depois de preencheres o PROJETO, eu descubro o resto sozinho:

    python {Path(__file__).name} configurar --escrever
    python {Path(__file__).name} doctor
    python {Path(__file__).name}            arranca

Entretanto podes ver se a maquinaria esta sa, sem configurar nada:

    python {Path(__file__).name} teste
""")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="orquestrador", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("comando", nargs="?", default="correr",
                    choices=["correr", "teste", "doctor", "configurar", "ver", "estado",
                             "bot", "worker"],
                    help="sem argumento nenhum: arranca")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--escrever", action="store_true",
                    help="com `configurar`: grava as definicoes neste ficheiro")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if a.comando == "teste":
        return autoteste()
    if a.comando == "doctor":
        return doctor()
    if a.comando == "configurar":
        return cmd_configurar(a.escrever)
    if a.comando == "ver":
        return cmd_ver(Path(PROJETO), FICHEIROS_EDITAVEIS)
    if a.comando == "estado":
        return estado_cli()

    faltas = pronto_para_arrancar()
    if faltas:
        return avisar_o_que_falta(faltas)
    return correr(com_bot=a.comando in ("correr", "bot"),
                  com_worker=a.comando in ("correr", "worker"))


if __name__ == "__main__":
    # `sys.exit(0)` levanta SystemExit, e o depurador do VS Code mostra isso como
    # "Exception has occurred" mesmo quando correu tudo bem. So saio com codigo
    # quando ha mesmo um erro.
    _codigo = main()
    if _codigo:
        sys.exit(_codigo)
