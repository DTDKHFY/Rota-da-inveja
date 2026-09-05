#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agente de trading ao vivo. Um ficheiro, sem dependencias alem de requests.

    python3 agente.py             corre (vigia + bot)
    python3 agente.py verificar   liga, autentica, diz o host, a conta e o saldo
    python3 agente.py contexto    escreve a fotografia do mercado agora, e sai
    python3 agente.py teste       autoteste: sem broker, sem Ollama, sem Telegram

O AGENTE E O CEREBRO
--------------------
Nao ha um unico `if` de estrategia neste ficheiro. O codigo mede e executa; quem
decide e o modelo. Se o quiseres mais agressivo ou mais seletivo, mudas o texto
dos prompts SISTEMA la em baixo — nao ha logica de mercado para mexer, porque
nao existe nenhuma.

A divisao e a mesma que ja protege o resto do repositorio:

    o codigo calcula todos os numeros  ->  o modelo escolhe entre eles
    o codigo verifica o que ele escolheu  ->  o codigo envia a ordem

E ha um guarda mecanico: qualquer numero que ele escreva e que nao tenha nascido
das contas e rejeitado, nomeado ("3.500 nao esta nos dados"), e ele corrige-se.
Melhor ainda — ELE NUNCA ESCREVE UM PRECO. Escolhe um nivel pelo nome e um
afastamento em ATR; o preco sai daqui. Um erro de digitacao deixa de poder por
um stop a 30 ATR, e obriga-o a ancorar a decisao num nivel que existe.

OS QUATRO MOMENTOS
------------------
    OBSERVA  -> arma um nivel, ou espera. Esperar e uma decisao completa.
    VIGIA    -> so codigo, zero modelo. O preco tocou? Pela MAXIMA e pela
                MINIMA de M1, nunca pelo fecho: esperar pelo fecho da vela e
                chegar tarde a metade dos toques.
    TOCOU    -> acorda o modelo outra vez, com o mercado DESSE instante (nao o
                de quando armou), e ele escolhe stop e take, ou desiste.
    DENTRO   -> aguenta, mexe, ou sai.

A fotografia que ele le, por escala (M15/H1/H4/D1), agregada de M1 e alinhada ao
relogio UTC — com a barra que ainda se esta a formar DESCARTADA, senao estarias
a medir um maximo provisorio. Cada escala traz ATR de Wilder, lambda de Kyle em
percentil (liquidez), e o percurso: compressao, pos% e sentido. Mais a estrutura
medida (FVG, BOS, CHoCH), as sessoes em UTC, e a regua de niveis.

DEMO AO VIVO
------------
Ordens verdadeiras, precos verdadeiros, latencia verdadeira, rejeicoes
verdadeiras — dinheiro simulado. E o unico sitio onde se descobre que o
stepVolume estava errado sem pagar para o aprender.

A passagem a live nao e uma constante que se troca. Sao tres fechaduras:

    1. CONTA = "demo"  -> o host sai daqui, num sitio so
    2. CONTA = "live"  -> exige tambem AGENTE_PERMITIR_LIVE=1 no ambiente
    3. cada mensagem vai carimbada [DEMO] ou [LIVE]

CREDENCIAIS
-----------
Nunca no ficheiro. Vai buscar ao ambiente:

    export CTRADER_CLIENT_ID=...          da tua aplicacao em connect.spotware.com
    export CTRADER_CLIENT_SECRET=...
    export CTRADER_ACCESS_TOKEN=...       do fluxo OAuth da tua aplicacao
    export CTRADER_ACCOUNT_ID=...         ctidTraderAccountId da conta demo
    export TELEGRAM_BOT_TOKEN=...         opcional: sem isto corre sem Telegram

O ficheiro NAO fala protobuf. A Open API tambem serve JSON, na porta 5036, com
um prefixo de comprimento de 4 bytes big-endian a frente de cada mensagem. Sao
trinta linhas de socket, e poupa o protobuf, o Twisted e o SDK.
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import socket
import sqlite3
import ssl
import statistics
import struct
import sys
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timezone
from html import unescape as desescapar_html
from pathlib import Path
from typing import Callable, Sequence

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca requests.  Corre:  pip install requests")

log = logging.getLogger("agente")


# ===========================================================================
#  CONFIGURACAO — e so isto que tens de mexer
# ===========================================================================

# --- Conta -----------------------------------------------------------------
# "demo" ou "live". O host sai daqui e de mais lado nenhum. Por "live" a mao
# nao chega: e precisa a variavel de ambiente AGENTE_PERMITIR_LIVE=1, para que
# um ficheiro copiado de outra maquina, ou uma edicao distraida, parem com a
# razao escrita em vez de comecarem a operar a serio.
CONTA = "demo"

HOSTS = {"demo": "demo.ctraderapi.com", "live": "live.ctraderapi.com"}
PORTA_JSON = 5036          # JSON. A 5035 e protobuf, e nao e o que falamos.

# O mercado. O nome tem de bater com o do teu broker — o `verificar` diz-te
# quais e que existem se este nao bater.
SIMBOLO = "ETHUSD"

# --- Telegram --------------------------------------------------------------
# Gera/revoga em https://t.me/BotFather -> /mybots -> API Token.
# Deixa "" e usa TELEGRAM_BOT_TOKEN no ambiente. Sem token, corre sem Telegram
# e escreve tudo no terminal — o agente nao para por falta de mensageiro.
TELEGRAM_TOKEN = ""
CHAT_ID = 0                # so este chat recebe e da ordens

# --- Modelo (ve os nomes exatos com: ollama list) --------------------------
OLLAMA_URL = "http://localhost:11434"
MODELO = "minimax-m3:cloud"
TIMEOUT_MODELO = 300
TENTATIVAS_JSON = 3

# Janela de contexto, SO para modelos locais. Um modelo de nuvem decide o
# contexto do lado dele, e impor-lhe um valor pode so atrasa-lo.
JANELA_MODELO = {"qwen2.5-coder:7b": 32_768}

# --- Ferramentas do agente -------------------------------------------------
# Ele pesquisa noticias e le paginas quando quiser, com orcamento. O que leu
# fica no registo, para tu poderes conferir a leitura contra a fonte.
MAX_FERRAMENTAS = 8              # consultas por turno, antes de ter de responder
ORCAMENTO_FERRAMENTAS = 24_000   # caracteres de resultados acumulados
WEB_TIMEOUT = 20
WEB_MAX_CARACTERES = 8_000

# --- Escalas e indicadores -------------------------------------------------
# Minutos por escala. A ordem e a que ele ve na tabela.
ESCALAS = (("M15", 15), ("H1", 60), ("H4", 240), ("D1", 1440))

ATR_PERIODOS = 14          # Wilder. A definicao vai escrita ao lado do numero.

# Quantas barras da propria escala entram no percurso (compressao, pos%,
# sentido). 20 barras de H4 sao pouco mais de tres dias; 20 de D1 sao um mes.
PERCURSO_BARRAS = 20

# Janela da regressao de Kyle, em barras da escala. Curta de mais e ruido;
# longa de mais deixa de dizer o que esta a acontecer agora.
KYLE_JANELA = 20

# Quantas barras de historia para os percentis de cada escala.
HISTORIA_BARRAS = 400

# Abaixo disto nao se diz uma percentagem. Dizer "acontece em 8% dos casos"
# com quinze casos e inventar precisao.
MINIMO_AMOSTRA = 100

# Quantos dias de M1 pedir. Tudo — as quatro escalas — sai destas mesmas M1,
# agregadas ao relogio UTC. Por isso o D1 so tem tantas barras quantos dias
# aqui estiverem: com 30 dias o ATR de D1 existe (precisa de 15) mas o
# percentil do lambda de D1 nao vai chegar ao MINIMO_AMOSTRA, e vai sair vazio
# com o n a frente — que e a resposta honesta, e nao um numero inventado para
# encher a coluna. Subir isto da mais historia e custa mais pedidos.
DIAS_DE_M1 = 30

# A Open API limita o alcance de cada pedido de M1, por isso a descarga vai em
# pedacos de uma semana. Nao e otimizacao: um pedido de 30 dias de M1 volta
# truncado, e um historico truncado em silencio e pior do que um erro.
M1_POR_PEDIDO_DIAS = 7

# --- Estrutura (FVG, BOS, CHoCH) -------------------------------------------
# Um pivo e um extremo com PIVO_BARRAS velas mais baixas de cada lado. Subir
# isto da menos pivos e mais significativos; descer da mais ruido.
PIVO_BARRAS = 2

# Quantos objetos de cada tipo, por escala, vao na fotografia. O resto e
# historia que nao cabe na janela de contexto e nao decide nada.
MAX_FVG = 4
MAX_EVENTOS = 4

# --- Sessoes, em horas UTC -------------------------------------------------
# Todas por aritmetica de epoch, que e UTC por construcao.
SESSOES = (("Asia", 0, 7), ("Londres", 7, 16), ("Nova Iorque", 12, 21))

# --- Os quatro momentos ----------------------------------------------------
MINUTOS_OBSERVA = 15       # de quanto em quanto tempo ele olha para armar
SEGUNDOS_VIGIA = 20        # de quanto em quanto tempo o CODIGO ve se tocou
MINUTOS_DENTRO = 15        # de quanto em quanto tempo ele gere a posicao aberta
SEGUNDOS_SINCRONIA = 60    # de quanto em quanto tempo se pergunta ao broker o que existe

# Quanto tempo um nivel armado vale, se ele nao disser outra coisa. O modelo
# pode pedir menos ou mais; isto e o tecto e a omissao.
VALIDADE_OMISSAO_MIN = 240
VALIDADE_MAX_MIN = 1440

# Afastamento maximo, em ATR, que ele pode pedir para um gatilho ou para um
# stop. Nao e uma opiniao sobre mercado: e o alcance dentro do qual um numero
# ainda quer dizer alguma coisa. Fora disto e um erro de digitacao.
MAX_AFASTAMENTO_ATR = 5.0

# --- Dimensionamento e cerca -----------------------------------------------
# NAO e uma trave: e a entrada de que o codigo precisa para calcular o volume
# a partir da distancia ao stop. Sem um numero aqui nao ha ordem nenhuma.
RISCO_POR_TRADE_PCT = 0.5

# A cerca. Verificada em codigo, e confirmada ao broker com um reconcile antes
# de cada ordem — acreditar na memoria do proprio processo e como se descobre,
# tarde, que ha duas posicoes abertas.
MAX_POSICOES_ABERTAS = 1

# Um stop so se move a favor da posicao. Isto parece um `if` de estrategia e
# nao e: afastar o stop depois de a posicao estar dimensionada para o stop
# original e aumentar, retroactivamente, um risco que ja foi assumido. Se
# discordares, poe False e ele move-o para onde quiser.
STOP_SO_A_FAVOR = True

# --- Onde guardar o estado -------------------------------------------------
BASE = Path(__file__).resolve().parent
BD = BASE / "agente.db"

# ===========================================================================
#  fim da configuracao
# ===========================================================================


class ErroAgente(Exception):
    """Falha do lado do modelo: nao respondeu, ou respondeu fora do formato."""


class ErroModelo(Exception):
    """Falha a falar com o Ollama. `permanente` diz se repetir ia mudar algo."""

    def __init__(self, mensagem, *, permanente: bool = False, pista: str = ""):
        super().__init__(mensagem)
        self.permanente = permanente
        self.pista = pista


class ErroBroker(Exception):
    """Falha do lado do cTrader: rede, autenticacao, ou um ProtoOAErrorRes."""

    def __init__(self, mensagem, *, codigo: str = ""):
        super().__init__(mensagem)
        self.codigo = codigo


class ErroWeb(Exception):
    """Nao consegui ler a pagina, ou o que veio de la nao era texto."""


def novo_id(prefixo: str) -> str:
    return f"{prefixo}_{uuid.uuid4().hex[:10]}"


def agora_utc_min() -> int:
    """O minuto corrente desde a epoch. UTC por construcao, sem fuso nenhum."""
    return int(time.time()) // 60


def carimbo() -> str:
    """[DEMO] ou [LIVE]. Vai em cada mensagem, para nunca ser preciso ir ver."""
    return f"[{CONTA.upper()}]"


# ===========================================================================
#  AS CONTAS
#
#  Tudo o que sai daqui e aritmetica sobre OHLCV, sem uma unica opiniao —
#  porque do outro lado ha um guarda que rejeita qualquer numero que o modelo
#  escreva e que nao tenha vindo destas contas. Se ele vai precisar de um
#  numero, e aqui que nasce.
#
#  Uma vela e sempre o tuplo (ts_minutos, abertura, maxima, minima, fecho,
#  volume). Seis campos, nesta ordem, em todo o ficheiro.
# ===========================================================================

T, O, H, L, C, V = 0, 1, 2, 3, 4, 5


def mediana(v: Sequence[float]) -> float | None:
    dados = [float(x) for x in v if x is not None]
    return statistics.median(dados) if dados else None


def agrupar(barras, minutos: int) -> list[tuple]:
    """Junta barras M1 em velas de `minutos`, alinhadas ao relogio UTC.

    Alinhadas ao relogio, e nao a ultima barra, porque "a maxima das ultimas 4
    horas" tem de ser a maxima de quatro horas de relogio para toda a gente que
    olha para o mesmo grafico que tu.

    Nao descarta a vela em formacao. Quem descarta e `fechadas`, e e de
    proposito que sao duas funcoes: assim nao ha caminho por onde uma vela por
    fechar chegue a um indicador sem alguem ter escrito que a queria.
    """
    fora: list[list] = []
    atual: list | None = None
    for b in barras:
        balde = (int(b[T]) // minutos) * minutos
        if atual is None or atual[T] != balde:
            if atual is not None:
                fora.append(atual)
            atual = [balde, b[O], b[H], b[L], b[C], b[V]]
        else:
            atual[H] = max(atual[H], b[H])
            atual[L] = min(atual[L], b[L])
            atual[C] = b[C]
            atual[V] += b[V]
    if atual is not None:
        fora.append(atual)
    return [tuple(x) for x in fora]


def fechadas(velas, minutos: int, agora_min: int) -> list[tuple]:
    """So as velas que ja fecharam. A que se esta a formar sai fora.

    Uma vela de H4 aberta ha dez minutos tem um maximo provisorio. Pos-la na
    tabela e dizer ao modelo "o maximo das ultimas 4 horas e este" quando na
    verdade e "e este, por enquanto" — e essa e a diferenca entre ler um nivel
    e ler um acidente.

    Um balde esta fechado quando toda a sua janela ja passou: `balde + minutos
    <= agora_min`. O balde que contem `agora_min` esta a formar-se, mesmo que
    ja tenha 239 dos seus 240 minutos.
    """
    return [v for v in velas if int(v[T]) + minutos <= agora_min]


def atr(velas, periodos: int = ATR_PERIODOS) -> float | None:
    """ATR de Wilder sobre as velas dadas. None se nao houver historia."""
    if len(velas) < periodos + 1:
        return None
    trs = []
    for i in range(1, len(velas)):
        anterior = velas[i - 1][C]
        trs.append(max(velas[i][H] - velas[i][L],
                       abs(velas[i][H] - anterior),
                       abs(velas[i][L] - anterior)))
    if len(trs) < periodos:
        return None
    valor = sum(trs[:periodos]) / periodos
    for tr in trs[periodos:]:
        valor = (valor * (periodos - 1) + tr) / periodos
    return valor


def serie_atr(velas, periodos: int = ATR_PERIODOS) -> list[float]:
    """O ATR de Wilder em cada ponto, e nao so no fim.

    E o que permite dizer "o ATR de agora contra o ATR do costume". Sem a
    serie, "compressao" seria uma palavra sem denominador.
    """
    if len(velas) < periodos + 1:
        return []
    trs = []
    for i in range(1, len(velas)):
        anterior = velas[i - 1][C]
        trs.append(max(velas[i][H] - velas[i][L],
                       abs(velas[i][H] - anterior),
                       abs(velas[i][L] - anterior)))
    valor = sum(trs[:periodos]) / periodos
    fora = [valor]
    for tr in trs[periodos:]:
        valor = (valor * (periodos - 1) + tr) / periodos
        fora.append(valor)
    return fora


def percentil(valor: float, amostra, minimo: int = MINIMO_AMOSTRA):
    """Que percentagem da amostra fica abaixo deste valor, e o n.

    Devolve o n SEMPRE, mesmo quando se recusa a dar a percentagem — porque a
    unica coisa pior do que nao ter amostra e ter e nao saber que e pouca.
    """
    dados = [float(x) for x in amostra if x is not None and math.isfinite(float(x))]
    if len(dados) < max(1, int(minimo)):
        return None, len(dados)
    return 100.0 * sum(1 for x in dados if x < valor) / len(dados), len(dados)


def extremos(velas):
    """(maxima, minima) das velas dadas, ou (None, None) se nao houver."""
    if not velas:
        return None, None
    return max(v[H] for v in velas), min(v[L] for v in velas)


def lambda_kyle(velas, janela: int = KYLE_JANELA) -> float | None:
    """Impacto de preco por unidade de volume assinado, sobre `janela` velas.

    Kyle: a variacao de preco e proporcional ao fluxo de ordens assinado, e a
    constante de proporcionalidade e a iliquidez. Estimador canonico, regressao
    pela origem:

        lambda = soma(dP * Vs) / soma(Vs^2),  com Vs = volume * sinal(fecho-abertura)

    Lambda alto = o preco mexe muito por unidade de volume = pouca liquidez.

    ATENCAO, e isto vai escrito na saida e nao so aqui: em CFD e em FX o volume
    do cTrader e VOLUME DE TICKS, nao volume transacionado. Isto e um proxy de
    liquidez. Chamar-lhe outra coisa seria dar-lhe uma precisao que nao tem.
    """
    if len(velas) < janela + 1:
        return None
    recentes = velas[-janela:]
    anterior = velas[-janela - 1][C]
    num = den = 0.0
    for v in recentes:
        dp = v[C] - anterior
        anterior = v[C]
        sinal = 1.0 if v[C] > v[O] else (-1.0 if v[C] < v[O] else 0.0)
        vs = float(v[V]) * sinal
        num += dp * vs
        den += vs * vs
    if den <= 0:
        return None
    return num / den


def serie_lambda(velas, janela: int = KYLE_JANELA) -> list[float]:
    """O lambda em cada ponto, para o percentil ter contra o que se comparar."""
    fora = []
    for fim in range(janela + 1, len(velas) + 1):
        valor = lambda_kyle(velas[:fim], janela)
        if valor is not None:
            fora.append(valor)
    return fora


def percurso(velas, barras: int = PERCURSO_BARRAS, atr_ref=None) -> dict:
    """De onde o preco veio, nesta escala: compressao, pos% e sentido.

    - compressao: ATR de agora a dividir pela mediana do ATR das ultimas
      `barras`. Abaixo de 1 e compressao. E um racio, por isso compara-se
      entre escalas sem mais nada.
    - pos%: onde o preco esta dentro da faixa das ultimas `barras`. Pode passar
      dos 100 ou ficar abaixo de 0 — isso quer dizer que saiu da faixa, o que e
      informacao e nao um erro.
    - sentido: o percurso do fecho contra o fecho de `barras` atras, medido em
      ATR. Em ATR e nao em pontos, senao as escalas nao sao comparaveis.
    """
    vazio = {"compressao": None, "compressao_n": 0, "pos_pct": None,
             "sentido_atr": None, "maxima": None, "minima": None, "barras": len(velas)}
    if len(velas) < 2:
        return vazio

    recentes = velas[-barras:]
    alta, baixa = extremos(recentes)
    preco = velas[-1][C]
    amplitude = (alta - baixa) if (alta is not None and baixa is not None) else 0.0

    serie = serie_atr(velas)
    med = mediana(serie[-barras:]) if serie else None
    compressao = (serie[-1] / med) if (serie and med) else None

    # A referencia e o fecho de ANTES da janela, nao o da primeira barra dela:
    # senao a faixa mede `barras` velas e o percurso mede `barras - 1`, e as
    # duas colunas da mesma linha deixam de falar do mesmo pedaco de tempo.
    antes = velas[-(barras + 1)][C] if len(velas) > barras else velas[0][C]
    sentido = ((preco - antes) / atr_ref) if atr_ref else None

    return {
        "compressao": compressao,
        "compressao_n": len(serie[-barras:]) if serie else 0,
        "pos_pct": (100.0 * (preco - baixa) / amplitude) if amplitude > 0 else None,
        "sentido_atr": sentido,
        "maxima": alta,
        "minima": baixa,
        "barras": len(recentes),
    }


# ---------------------------------------------------------------------------
#  Estrutura: FVG, BOS, CHoCH
#
#  Isto e geometria, nao estrategia. Um FVG e um buraco entre duas velas; um
#  BOS e um fecho para la de um extremo anterior. Medir onde eles estao nao e
#  decidir nada — o que eles significam e o que se faz com eles fica todo do
#  outro lado, no modelo. A definicao de cada um vai escrita ao lado do numero,
#  para ninguem comparar o meu BOS com o BOS de outra ferramenta sem dar por isso.
# ---------------------------------------------------------------------------
def pivos(velas, k: int = PIVO_BARRAS) -> list[dict]:
    """Extremos com `k` velas mais baixas (ou mais altas) de cada lado.

    Um pivo so existe `k` velas DEPOIS de acontecer — e por isso que ele nunca
    e um sinal em tempo real, e e por isso que o BOS o usa como referencia
    passada e nao como gatilho.
    """
    fora = []
    for i in range(k, len(velas) - k):
        janela = velas[i - k:i + k + 1]
        if velas[i][H] == max(v[H] for v in janela) and \
                sum(1 for v in janela if v[H] == velas[i][H]) == 1:
            fora.append({"i": i, "tipo": "alta", "preco": velas[i][H], "ts": int(velas[i][T])})
        if velas[i][L] == min(v[L] for v in janela) and \
                sum(1 for v in janela if v[L] == velas[i][L]) == 1:
            fora.append({"i": i, "tipo": "baixa", "preco": velas[i][L], "ts": int(velas[i][T])})
    return sorted(fora, key=lambda p: (p["i"], p["tipo"]))


def estrutura(velas, k: int = PIVO_BARRAS) -> dict:
    """Percorre as velas e marca cada rompimento como BOS ou CHoCH.

    BOS   — fecho para la do ultimo pivo, NA direcao que a estrutura ja tinha.
    CHoCH — o primeiro fecho para la de um pivo CONTRA essa direcao. E o mesmo
            evento geometrico; o que muda e o que estava antes dele.

    Depois de romper, a referencia desse lado fica a None ate um pivo novo se
    confirmar. Sem isso, a mesma quebra dispararia em todas as velas seguintes
    e a lista de eventos passava a ser uma lista do mesmo evento.
    """
    todos = pivos(velas, k)
    por_indice: dict[int, list[dict]] = {}
    for p in todos:
        # Um pivo em `i` so se sabe em `i + k`: e ai que entra em jogo.
        por_indice.setdefault(p["i"] + k, []).append(p)

    alto = baixo = None
    direcao = 0
    eventos = []
    for i, v in enumerate(velas):
        for p in por_indice.get(i, []):
            if p["tipo"] == "alta":
                alto = p
            else:
                baixo = p

        if alto is not None and v[C] > alto["preco"]:
            tipo = "CHoCH" if direcao < 0 else "BOS"
            eventos.append({"tipo": tipo, "lado": "alta", "preco": alto["preco"],
                            "ts": int(v[T]), "i": i})
            direcao, alto = 1, None
        elif baixo is not None and v[C] < baixo["preco"]:
            tipo = "CHoCH" if direcao > 0 else "BOS"
            eventos.append({"tipo": tipo, "lado": "baixa", "preco": baixo["preco"],
                            "ts": int(v[T]), "i": i})
            direcao, baixo = -1, None

    return {
        "direcao": direcao,
        "swing_alto": alto["preco"] if alto else None,
        "swing_baixo": baixo["preco"] if baixo else None,
        "eventos": eventos[-MAX_EVENTOS:],
        "definicao": f"pivo de {k} velas de cada lado; BOS/CHoCH por fecho",
    }


def fvgs(velas) -> list[dict]:
    """Buracos de tres velas: a vela do meio andou sem deixar negocio atras.

    Alta:  minima[i+1] > maxima[i-1]   -> o buraco e (maxima[i-1], minima[i+1])
    Baixa: maxima[i+1] < minima[i-1]   -> o buraco e (maxima[i+1], minima[i-1])

    `mitigado` e `preenchido` nao sao a mesma coisa, e a diferenca importa: um
    buraco que o preco so aflorou nao e o mesmo objeto que um que ele fechou.
    Devolvo os dois para o modelo poder distingui-los, em vez de lhe dar uma
    bandeira so e obriga-lo a adivinhar qual delas e.
    """
    fora = []
    for i in range(1, len(velas) - 1):
        antes, depois = velas[i - 1], velas[i + 1]
        if depois[L] > antes[H]:
            baixo, alto, lado = antes[H], depois[L], "alta"
        elif depois[H] < antes[L]:
            baixo, alto, lado = depois[H], antes[L], "baixa"
        else:
            continue
        seguintes = velas[i + 2:]
        fora.append({
            "lado": lado, "de": baixo, "ate": alto, "ts": int(velas[i][T]),
            "idade_barras": len(velas) - 1 - i,
            "mitigado": any(v[L] <= alto and v[H] >= baixo for v in seguintes),
            "preenchido": any(v[L] <= baixo for v in seguintes) if lado == "alta"
                          else any(v[H] >= alto for v in seguintes),
        })
    return fora[-MAX_FVG:]


# ---------------------------------------------------------------------------
#  Sessoes
# ---------------------------------------------------------------------------
def janela_sessao(agora_min: int, ini_h: int, fim_h: int) -> tuple[int, int]:
    """A faixa mais recente desta sessao que ja comecou, em minutos da epoca.

    Tudo UTC por construcao: a epoca comeca a meia-noite UTC, por isso
    `(t // 1440) * 1440` cai exatamente na meia-noite e nao ha fuso nenhum a
    tratar. Uma sessao que atravessa a meia-noite comeca no dia anterior.
    """
    dia = (agora_min // 1440) * 1440
    if ini_h < fim_h:
        a, b = dia + ini_h * 60, dia + fim_h * 60
    else:
        a, b = dia - 1440 + ini_h * 60, dia + fim_h * 60
    if agora_min < a:
        a, b = a - 1440, b - 1440
    return a, b


def entre(velas, t0: int, t1: int) -> list[tuple]:
    """As velas cujo inicio cai em [t0, t1)."""
    return [v for v in velas if t0 <= v[T] < t1]


def rompeu(depois, alta, baixa) -> tuple[str, bool]:
    """Saiu da faixa, e voltou para dentro? Varrida ou rompimento.

    O segundo campo e o que separa uma coisa da outra: sair e ficar la fora e
    um rompimento; sair e voltar e uma varrida, e nao querem dizer o mesmo.
    """
    if not depois or alta is None or baixa is None:
        return "nao", False
    acima = any(v[H] > alta for v in depois)
    abaixo = any(v[L] < baixa for v in depois)
    onde = ("ambos" if acima and abaixo else
            "acima" if acima else "abaixo" if abaixo else "nao")
    fecho = depois[-1][C]
    return onde, (onde != "nao" and baixa <= fecho <= alta)


def sessoes(m1, agora_min: int, preco: float) -> list[dict]:
    """Cada sessao com a sua faixa, e se o preco ja a varreu."""
    fora = []
    for nome, ini_h, fim_h in SESSOES:
        t0, t1 = janela_sessao(agora_min, ini_h, fim_h)
        velas = entre(m1, t0, t1)
        alta, baixa = extremos(velas)
        onde, voltou = rompeu(entre(m1, t1, agora_min + 1), alta, baixa)
        amplitude = (alta - baixa) if (alta is not None and baixa is not None) else None
        fora.append({
            "nome": nome, "inicio_h": ini_h, "fim_h": fim_h,
            "aberta": t0 <= agora_min < t1,
            "maxima": alta, "minima": baixa, "velas": len(velas),
            "posicao_pct": (100.0 * (preco - baixa) / amplitude)
                           if (amplitude and amplitude > 0) else None,
            "rompeu": onde, "voltou_para_dentro": voltou,
        })
    return fora


# ---------------------------------------------------------------------------
#  A regua: os niveis todos na mesma escada
# ---------------------------------------------------------------------------
def regua(niveis, preco: float, atr_ref) -> dict:
    """Quantos pontos ha daqui ate cada sitio que interessa, e quantos ATR.

    E daqui que o agente escolhe: ele diz um NOME de nivel e um afastamento em
    ATR, e o preco sai desta escada. Por isso tudo o que ele possa querer
    escolher tem de estar aqui — um nivel que nao esteja na regua nao existe
    para ele, e e assim que se evita que invente um.
    """
    degraus = []
    for nome, p in niveis:
        if p is None:
            continue
        pontos = float(p) - preco
        degraus.append({
            "etiqueta": nome,
            "preco": float(p),
            "pontos": pontos,
            "atr": (abs(pontos) / atr_ref) if atr_ref else None,
        })
    return {
        "preco": preco,
        "atr_ref": atr_ref,
        "degraus": sorted(degraus, key=lambda d: -d["preco"]),
    }


# ===========================================================================
#  O BROKER: cTrader Open API em JSON, sem uma unica dependencia
#
#  A Open API serve protobuf na 5035 e JSON na 5036. Escolhi JSON: cada
#  mensagem e `{"clientMsgId", "payloadType", "payload"}` com um prefixo de
#  comprimento de 4 bytes big-endian a frente. Sao trinta linhas de socket, e
#  poupa o protobuf, o Twisted e o SDK — que e o que mantem este ficheiro
#  unico e sem `pip install` nenhum para o que interessa.
# ===========================================================================

class PT:
    """Os payloadType do enum oficial. Nomes, para nao haver 2137 solto."""
    HEARTBEAT = 51                    # de ProtoPayloadType, nao do ProtoOA*
    APP_AUTH_REQ = 2100
    APP_AUTH_RES = 2101
    ACCOUNT_AUTH_REQ = 2102
    ACCOUNT_AUTH_RES = 2103
    NEW_ORDER_REQ = 2106
    AMEND_POSITION_SLTP_REQ = 2110
    CLOSE_POSITION_REQ = 2111
    SYMBOLS_LIST_REQ = 2114
    SYMBOLS_LIST_RES = 2115
    SYMBOL_BY_ID_REQ = 2116
    SYMBOL_BY_ID_RES = 2117
    TRADER_REQ = 2121
    TRADER_RES = 2122
    RECONCILE_REQ = 2124
    RECONCILE_RES = 2125
    EXECUTION_EVENT = 2126
    ORDER_ERROR_EVENT = 2132
    GET_TRENDBARS_REQ = 2137
    GET_TRENDBARS_RES = 2138
    ERROR_RES = 2142
    ACCOUNTS_BY_TOKEN_REQ = 2149
    ACCOUNTS_BY_TOKEN_RES = 2150
    REFRESH_TOKEN_REQ = 2173
    REFRESH_TOKEN_RES = 2174


# Os periodos de trendbar, do enum ProtoOATrendbarPeriod.
PERIODO = {1: "M1", 5: "M5", 7: "M15", 8: "M30", 9: "H1", 10: "H4", 12: "D1"}
PERIODO_M1 = 1

SEGUNDOS_HEARTBEAT = 10       # a ligacao cai sozinha sem isto
TIMEOUT_PEDIDO = 30


def _ler_exato(sock, n: int) -> bytes:
    """Le exatamente n bytes, ou levanta. `recv` devolve o que quiser."""
    partes, faltam = [], n
    while faltam > 0:
        pedaco = sock.recv(faltam)
        if not pedaco:
            raise ErroBroker("o broker fechou a ligacao")
        partes.append(pedaco)
        faltam -= len(pedaco)
    return b"".join(partes)


def escala_ctrader(low) -> float:
    """Os precos vem em pontos (x100000). Um `low` pequeno ja veio convertido.

    Decide pela ordem de grandeza em vez de assumir, porque um candle dividido
    duas vezes produz numeros que ainda parecem uma leitura.
    """
    return 1e-5 if abs(float(low or 0)) > 1e5 else 1.0


def trendbar_para_vela(tb: dict, escala: float) -> tuple:
    """Um ProtoOATrendbar para o tuplo (ts, o, h, l, c, v).

    O `low` e o proprio low; as outras tres sao o low mais o respetivo delta.
    Nao ha deltaLow — se andares a procura dele, e por isso que nao o achas.
    """
    baixo = float(tb.get("low") or 0)
    return (
        int(tb.get("utcTimestampInMinutes") or 0),
        (baixo + float(tb.get("deltaOpen") or 0)) * escala,
        (baixo + float(tb.get("deltaHigh") or 0)) * escala,
        baixo * escala,
        (baixo + float(tb.get("deltaClose") or 0)) * escala,
        float(tb.get("volume") or 0),
    )


class Ligacao:
    """O socket, o enquadramento, o heartbeat e a correlacao das respostas.

    A correlacao por clientMsgId nao e um extra: as respostas chegam fora de
    ordem e misturadas com eventos que ninguem pediu (execucoes, spots). Sem um
    dicionario de pedidos a espera, mais cedo ou mais tarde le-se a resposta de
    outra pergunta e acredita-se nela.
    """

    def __init__(self, host: str, porta: int = PORTA_JSON, *, tls: bool = True,
                 timeout: int = TIMEOUT_PEDIDO):
        self.host, self.porta, self.tls, self.timeout = host, porta, tls, timeout
        self.sock = None
        self._envio = threading.Lock()
        self._pendentes: dict[str, dict] = {}
        self._pendentes_lock = threading.Lock()
        self._parar = threading.Event()
        self._leitor = self._pulso = None
        self.eventos: Callable[[int, dict], None] | None = None
        self.morreu: str = ""

    # -- ligar e desligar ---------------------------------------------------
    def abrir(self) -> None:
        cru = socket.create_connection((self.host, self.porta), timeout=self.timeout)
        if self.tls:
            ctx = ssl.create_default_context()
            cru = ctx.wrap_socket(cru, server_hostname=self.host)
        cru.settimeout(None)          # o leitor bloqueia; o timeout e por pedido
        self.sock, self.morreu = cru, ""
        self._parar.clear()
        self._leitor = threading.Thread(target=self._ler_sempre, name="ct-leitor", daemon=True)
        self._leitor.start()
        self._pulso = threading.Thread(target=self._bater_sempre, name="ct-pulso", daemon=True)
        self._pulso.start()

    def fechar(self) -> None:
        self._parar.set()
        try:
            if self.sock:
                self.sock.close()
        except OSError:
            pass
        self.sock = None
        with self._pendentes_lock:
            for espera in self._pendentes.values():
                espera["erro"] = ErroBroker("a ligacao foi fechada")
                espera["evento"].set()
            self._pendentes.clear()

    @property
    def viva(self) -> bool:
        return self.sock is not None and not self._parar.is_set()

    # -- escrever e ler -----------------------------------------------------
    def _escrever(self, mensagem: dict) -> None:
        carga = json.dumps(mensagem).encode("utf-8")
        with self._envio:
            sock = self.sock
            if sock is None:
                raise ErroBroker("nao estou ligado ao broker")
            sock.sendall(struct.pack(">I", len(carga)) + carga)

    def _ler_sempre(self) -> None:
        while not self._parar.is_set():
            try:
                (tamanho,) = struct.unpack(">I", _ler_exato(self.sock, 4))
                bruto = _ler_exato(self.sock, tamanho)
                mensagem = json.loads(bruto.decode("utf-8", "replace"))
            except Exception as e:                    # noqa: BLE001 — ver abaixo
                # Qualquer coisa que rebente aqui mata a ligacao, e a ligacao
                # morta tem de acordar quem esta a espera de uma resposta.
                # Deixar a excecao subir matava a thread em silencio e deixava
                # o resto do programa pendurado num Event que nunca vem.
                if not self._parar.is_set():
                    self.morreu = f"{type(e).__name__}: {e}"
                    log.warning("ligacao ao broker caiu: %s", self.morreu)
                self._acordar_todos(ErroBroker(f"a ligacao caiu: {self.morreu}"))
                self._parar.set()
                return
            self._despachar(mensagem)

    def _acordar_todos(self, erro: Exception) -> None:
        with self._pendentes_lock:
            for espera in self._pendentes.values():
                espera["erro"] = erro
                espera["evento"].set()
            self._pendentes.clear()

    def _despachar(self, mensagem: dict) -> None:
        tipo = int(mensagem.get("payloadType") or 0)
        carga = mensagem.get("payload") or {}
        cid = mensagem.get("clientMsgId") or ""

        if tipo == PT.HEARTBEAT:
            return
        with self._pendentes_lock:
            espera = self._pendentes.pop(cid, None)
        if espera is not None:
            if tipo == PT.ERROR_RES:
                espera["erro"] = ErroBroker(
                    f"{carga.get('errorCode', 'erro')}: {carga.get('description', '')}".strip(": "),
                    codigo=str(carga.get("errorCode") or ""))
            else:
                espera["tipo"], espera["carga"] = tipo, carga
            espera["evento"].set()
            return
        if self.eventos:
            try:
                self.eventos(tipo, carga)
            except Exception:
                log.exception("falhei a tratar o evento %s", tipo)

    def _bater_sempre(self) -> None:
        while not self._parar.wait(SEGUNDOS_HEARTBEAT):
            try:
                self._escrever({"payloadType": PT.HEARTBEAT, "payload": {}})
            except Exception:
                return

    # -- pedir e esperar ----------------------------------------------------
    def pedir(self, tipo: int, carga: dict, *, timeout: float | None = None) -> dict:
        """Manda um pedido e espera PELA RESPOSTA DELE, nao pela proxima."""
        cid = uuid.uuid4().hex[:16]
        espera = {"evento": threading.Event(), "erro": None, "tipo": 0, "carga": {}}
        with self._pendentes_lock:
            self._pendentes[cid] = espera
        try:
            self._escrever({"clientMsgId": cid, "payloadType": tipo, "payload": carga})
        except Exception:
            with self._pendentes_lock:
                self._pendentes.pop(cid, None)
            raise
        if not espera["evento"].wait(timeout or self.timeout):
            with self._pendentes_lock:
                self._pendentes.pop(cid, None)
            raise ErroBroker(f"o broker nao respondeu ao pedido {tipo} em "
                             f"{timeout or self.timeout}s")
        if espera["erro"]:
            raise espera["erro"]
        return espera["carga"]

    def enviar_sem_esperar(self, tipo: int, carga: dict) -> None:
        """Para o que nao tem resposta propria (as ordens vem por evento)."""
        self._escrever({"clientMsgId": uuid.uuid4().hex[:16],
                        "payloadType": tipo, "payload": carga})


def credenciais() -> dict:
    """As credenciais, do ambiente. Nunca do ficheiro, nunca do git."""
    faltam, fora = [], {}
    for chave, nome in (("CTRADER_CLIENT_ID", "cliente"),
                        ("CTRADER_CLIENT_SECRET", "segredo"),
                        ("CTRADER_ACCESS_TOKEN", "token"),
                        ("CTRADER_ACCOUNT_ID", "conta")):
        valor = (os.environ.get(chave) or "").strip()
        if not valor:
            faltam.append(chave)
        fora[nome] = valor
    if faltam:
        raise ErroBroker(
            "faltam credenciais no ambiente: " + ", ".join(faltam) +
            "\nApanha-as em https://connect.spotware.com (aplicacao + OAuth) e faz:\n" +
            "\n".join(f"    export {c}=..." for c in faltam))
    try:
        fora["conta"] = int(fora["conta"])
    except ValueError:
        raise ErroBroker(f"CTRADER_ACCOUNT_ID tem de ser um numero, e e {fora['conta']!r}") from None
    return fora


def host_da_conta() -> str:
    """O host sai de CONTA, e de mais lado nenhum. Fechadura numero um."""
    if CONTA not in HOSTS:
        raise ErroBroker(f"CONTA e {CONTA!r}; so conheco {sorted(HOSTS)}")
    return HOSTS[CONTA]


def exigir_conta_permitida() -> None:
    """Fechadura numero dois: live sozinho nao arranca.

    Um ficheiro copiado de outra maquina, ou uma edicao distraida da constante,
    param aqui com a razao escrita — em vez de comecarem a mandar ordens a
    serio e so darmos por isso depois.
    """
    if CONTA != "live":
        return
    if (os.environ.get("AGENTE_PERMITIR_LIVE") or "").strip() != "1":
        raise ErroBroker(
            "CONTA = \"live\" mas AGENTE_PERMITIR_LIVE nao esta a 1.\n"
            "Isto sao ordens com dinheiro a serio. Se e mesmo o que queres:\n"
            "    export AGENTE_PERMITIR_LIVE=1\n"
            "Se nao e, poe CONTA = \"demo\" de volta.")


class CTrader:
    """A conversa com o broker, ja em portugues: barras, ordens, posicoes."""

    def __init__(self, simbolo: str = SIMBOLO, *, ligacao: Ligacao | None = None,
                 creds: dict | None = None):
        exigir_conta_permitida()
        self.creds = creds or credenciais()
        self.simbolo_nome = simbolo
        self.lig = ligacao or Ligacao(host_da_conta())
        self.lig.eventos = self._evento
        self.simbolo_id = None
        self.detalhes: dict = {}
        self.escala = 1.0
        self.execucoes: list[dict] = []
        self._execucoes_lock = threading.Lock()

    # -- ligar --------------------------------------------------------------
    def ligar(self) -> None:
        self.lig.abrir()
        self.lig.pedir(PT.APP_AUTH_REQ, {"clientId": self.creds["cliente"],
                                         "clientSecret": self.creds["segredo"]})
        self._exigir_conta_do_token()
        self.lig.pedir(PT.ACCOUNT_AUTH_REQ, {"ctidTraderAccountId": self.creds["conta"],
                                             "accessToken": self.creds["token"]})

    def _exigir_conta_do_token(self) -> None:
        """Fechadura numero tres: a conta e confirmada, nao assumida.

        Um token de demo com um id de live (ou o contrario) e um erro de
        arranque com os dois valores a frente — e nao uma ordem enviada para o
        sitio errado, descoberta pelo extrato.
        """
        carga = self.lig.pedir(PT.ACCOUNTS_BY_TOKEN_REQ,
                               {"accessToken": self.creds["token"]})
        contas = [int(c.get("ctidTraderAccountId") or 0)
                  for c in (carga.get("ctidTraderAccount") or [])]
        if contas and self.creds["conta"] not in contas:
            raise ErroBroker(
                f"a conta {self.creds['conta']} nao esta entre as que este token "
                f"autoriza ({', '.join(str(c) for c in contas)}).\n"
                f"Ou o CTRADER_ACCOUNT_ID esta errado, ou o token e de outra conta.")

    def garantir_ligado(self) -> None:
        """Religa e volta a autenticar. Uma ligacao morta nao se remenda."""
        if self.lig.viva:
            return
        log.warning("a religar ao broker (%s)", self.lig.morreu or "sem razao")
        self.lig.fechar()
        espera = 2.0
        for tentativa in range(5):
            try:
                self.ligar()
                self.resolver_simbolo()
                log.info("ligado outra vez ao broker")
                return
            except Exception as e:                    # noqa: BLE001
                log.warning("tentativa %d de religar falhou: %s", tentativa + 1, e)
                time.sleep(espera)
                espera = min(espera * 2, 60.0)
        raise ErroBroker("nao consegui religar ao broker ao fim de 5 tentativas")

    def fechar(self) -> None:
        self.lig.fechar()

    # -- eventos que ninguem pediu -----------------------------------------
    def _evento(self, tipo: int, carga: dict) -> None:
        if tipo in (PT.EXECUTION_EVENT, PT.ORDER_ERROR_EVENT):
            with self._execucoes_lock:
                self.execucoes.append({"tipo": tipo, "carga": carga, "quando": time.time()})
                del self.execucoes[:-50]

    def execucoes_recentes(self, desde: float) -> list[dict]:
        with self._execucoes_lock:
            return [e for e in self.execucoes if e["quando"] >= desde]

    # -- o mercado ----------------------------------------------------------
    def resolver_simbolo(self) -> dict:
        """Nome -> symbolId, mais o lote, o passo e as casas decimais.

        Isto nao e burocracia: e onde se apanha o stepVolume errado e o simbolo
        que nao e o que se julgava, ANTES de existir uma ordem — e nao pela
        rejeicao dela.
        """
        carga = self.lig.pedir(PT.SYMBOLS_LIST_REQ,
                               {"ctidTraderAccountId": self.creds["conta"],
                                "includeArchivedSymbols": False})
        todos = carga.get("symbol") or []
        alvo = self.simbolo_nome.strip().upper()
        achado = next((s for s in todos
                       if str(s.get("symbolName") or "").strip().upper() == alvo), None)
        if achado is None:
            parecidos = sorted(str(s.get("symbolName") or "") for s in todos
                               if alvo[:3] in str(s.get("symbolName") or "").upper())
            raise ErroBroker(
                f"nao encontrei o simbolo {self.simbolo_nome!r} nesta conta.\n"
                + (f"Parecidos: {', '.join(parecidos[:15])}" if parecidos
                   else f"A conta tem {len(todos)} simbolos."))
        self.simbolo_id = int(achado["symbolId"])

        detalhe = self.lig.pedir(PT.SYMBOL_BY_ID_REQ,
                                 {"ctidTraderAccountId": self.creds["conta"],
                                  "symbolId": [self.simbolo_id]})
        cheio = (detalhe.get("symbol") or [{}])[0]
        self.detalhes = {
            "symbolId": self.simbolo_id,
            "nome": achado.get("symbolName"),
            "digits": int(cheio.get("digits") or achado.get("digits") or 2),
            "pipPosition": int(cheio.get("pipPosition") or 0),
            "lotSize": int(cheio.get("lotSize") or 0),
            "minVolume": int(cheio.get("minVolume") or 0),
            "maxVolume": int(cheio.get("maxVolume") or 0),
            "stepVolume": int(cheio.get("stepVolume") or 0),
        }
        return self.detalhes

    def conta(self) -> dict:
        """Saldo e moeda. Serve para dimensionar, e para veres onde estas."""
        carga = self.lig.pedir(PT.TRADER_REQ, {"ctidTraderAccountId": self.creds["conta"]})
        t = carga.get("trader") or {}
        casas = int(t.get("moneyDigits") or 2)
        return {
            "id": int(t.get("ctidTraderAccountId") or self.creds["conta"]),
            "saldo": float(t.get("balance") or 0) / (10 ** casas),
            "moeda_id": t.get("depositAssetId"),
            "alavancagem": t.get("leverageInCents"),
        }

    def _pedir_m1(self, inicio_ms: int, fim_ms: int) -> list[tuple]:
        carga = self.lig.pedir(PT.GET_TRENDBARS_REQ, {
            "ctidTraderAccountId": self.creds["conta"],
            "symbolId": self.simbolo_id,
            "period": PERIODO_M1,
            "fromTimestamp": int(inicio_ms),
            "toTimestamp": int(fim_ms),
            "count": int((fim_ms - inicio_ms) // 60000) + 1,
        }, timeout=60)
        cruas = carga.get("trendbar") or []
        if cruas:
            self.escala = escala_ctrader(cruas[0].get("low"))
        return [trendbar_para_vela(tb, self.escala) for tb in cruas]

    def m1(self, dias: int = DIAS_DE_M1) -> list[tuple]:
        """`dias` de velas M1, em pedacos, ja em tuplos e ja sem repetidas.

        Tudo o resto do ficheiro sai daqui: as quatro escalas sao reagrupadas
        destas mesmas velas. Uma so fonte, para nao haver duas verdades sobre o
        mesmo minuto — e para o M15 e o D1 nao poderem discordar por terem sido
        descarregados de sitios diferentes.
        """
        fim = int(time.time() * 1000)
        passo = M1_POR_PEDIDO_DIAS * 86_400_000
        inicio = fim - max(1, int(dias)) * 86_400_000
        por_ts: dict[int, tuple] = {}
        janela = inicio
        while janela < fim:
            for v in self._pedir_m1(janela, min(janela + passo, fim)):
                por_ts[v[T]] = v
            janela += passo
        if not por_ts:
            raise ErroBroker("o broker nao devolveu candle nenhum")
        return [por_ts[k] for k in sorted(por_ts)]

    def m1_recentes(self, minutos: int = 30) -> list[tuple]:
        """So os ultimos minutos. E o que a vigia pede, de 20 em 20 segundos.

        Pedir 30 dias de M1 a cada volta da vigia seria pagar a historia toda
        para responder a uma pergunta sobre o ultimo minuto.
        """
        fim = int(time.time() * 1000)
        velas = self._pedir_m1(fim - max(2, int(minutos)) * 60_000, fim)
        return sorted(velas, key=lambda v: v[T])

    # -- as ordens ----------------------------------------------------------
    def posicoes(self) -> list[dict]:
        """O que existe MESMO, perguntado ao broker.

        Nunca a memoria deste processo: um restart, uma ordem manual tua, ou um
        stop que bateu enquanto isto estava a dormir sao todos maneiras de a
        memoria ficar a mentir. Quem sabe e quem as tem.
        """
        carga = self.lig.pedir(PT.RECONCILE_REQ,
                               {"ctidTraderAccountId": self.creds["conta"],
                                "returnProtectionOrders": True})
        fora = []
        for p in (carga.get("position") or []):
            dados = p.get("tradeData") or {}
            if int(dados.get("symbolId") or 0) != int(self.simbolo_id or 0):
                continue
            fora.append({
                "positionId": int(p.get("positionId") or 0),
                "lado": "compra" if str(dados.get("tradeSide")) in ("1", "BUY") else "venda",
                "volume": int(dados.get("volume") or 0),
                "preco": float(p.get("price") or 0),
                "stop": p.get("stopLoss"),
                "take": p.get("takeProfit"),
                "aberta_em": dados.get("openTimestamp"),
            })
        return fora

    def nova_ordem(self, *, lado: str, volume: int, stop: float, take: float,
                   etiqueta: str = "") -> None:
        """Ordem a mercado, com stop e take ja postos.

        Vai sem esperar por resposta porque a confirmacao nao vem como resposta
        — vem como ProtoOAExecutionEvent, que pode chegar antes ou depois. Quem
        confirma e o reconcile a seguir, que pergunta em vez de acreditar.
        """
        self.lig.enviar_sem_esperar(PT.NEW_ORDER_REQ, {
            "ctidTraderAccountId": self.creds["conta"],
            "symbolId": self.simbolo_id,
            "orderType": 1,                       # MARKET
            "tradeSide": 1 if lado == "compra" else 2,
            "volume": int(volume),
            "stopLoss": round(float(stop), self.detalhes.get("digits", 2)),
            "takeProfit": round(float(take), self.detalhes.get("digits", 2)),
            "label": etiqueta[:100] or "agente",
        })

    def mexer_sltp(self, position_id: int, stop: float, take: float) -> None:
        casas = self.detalhes.get("digits", 2)
        self.lig.enviar_sem_esperar(PT.AMEND_POSITION_SLTP_REQ, {
            "ctidTraderAccountId": self.creds["conta"],
            "positionId": int(position_id),
            "stopLoss": round(float(stop), casas),
            "takeProfit": round(float(take), casas),
        })

    def fechar_posicao(self, position_id: int, volume: int) -> None:
        self.lig.enviar_sem_esperar(PT.CLOSE_POSITION_REQ, {
            "ctidTraderAccountId": self.creds["conta"],
            "positionId": int(position_id),
            "volume": int(volume),
        })


# ===========================================================================
#  A FOTOGRAFIA
#
#  O que sai daqui e o universo de numeros que o modelo pode usar do outro
#  lado. Se ele escrever um que nao esteja aqui, e rejeitado — por isso tudo o
#  que ele possa precisar tem de nascer nesta funcao.
# ===========================================================================

def fotografia(m1, agora_min: int, *, simbolo: str = SIMBOLO,
               detalhes: dict | None = None) -> dict:
    """Todos os numeros que a decisao precisa, e nem um julgamento."""
    if not m1:
        raise ErroBroker("nao consegui montar uma unica vela a partir dos candles")

    preco = m1[-1][C]
    ts_agora = int(m1[-1][T])
    escalas, niveis = [], []

    for nome, minutos in ESCALAS:
        velas = fechadas(agrupar(m1, minutos), minutos, agora_min)
        velas = velas[-HISTORIA_BARRAS:]
        a = atr(velas)
        lam = lambda_kyle(velas)
        historia = serie_lambda(velas)
        pct_lam, n_lam = ((None, len(historia)) if lam is None
                          else percentil(lam, historia, MINIMO_AMOSTRA))
        p = percurso(velas, PERCURSO_BARRAS, a)
        est = estrutura(velas)
        buracos = fvgs(velas)

        escalas.append({
            "nome": nome, "minutos": minutos, "velas": len(velas),
            "atr": a,
            "lambda_kyle": lam,
            "lambda_percentil": pct_lam,
            "lambda_n": n_lam,
            "compressao": p["compressao"],
            "compressao_n": p["compressao_n"],
            "pos_pct": p["pos_pct"],
            "sentido_atr": p["sentido_atr"],
            "maxima": p["maxima"], "minima": p["minima"],
            "estrutura": est,
            "fvg": buracos,
        })

        if p["maxima"] is not None:
            niveis.append((f"max {nome}", p["maxima"]))
        if p["minima"] is not None:
            niveis.append((f"min {nome}", p["minima"]))
        if est["swing_alto"] is not None:
            niveis.append((f"swing alto {nome}", est["swing_alto"]))
        if est["swing_baixo"] is not None:
            niveis.append((f"swing baixo {nome}", est["swing_baixo"]))
        aberto = next((b for b in reversed(buracos) if not b["preenchido"]), None)
        if aberto:
            niveis.append((f"fvg {aberto['lado']} {nome} topo", aberto["ate"]))
            niveis.append((f"fvg {aberto['lado']} {nome} fundo", aberto["de"]))

    sess = sessoes(m1, agora_min, preco)
    for s in sess:
        if s["maxima"] is not None:
            niveis.append((f"max {s['nome']}", s["maxima"]))
        if s["minima"] is not None:
            niveis.append((f"min {s['nome']}", s["minima"]))

    atr_ref = next((e["atr"] for e in escalas if e["nome"] == "H1" and e["atr"]), None)
    if atr_ref is None:
        atr_ref = next((e["atr"] for e in escalas if e["atr"]), None)

    dados = {
        "simbolo": simbolo,
        "conta": CONTA,
        "preco": preco,
        "ts_min": ts_agora,
        "agora_utc": datetime.fromtimestamp(ts_agora * 60, timezone.utc).strftime("%Y-%m-%d %H:%M"),
        "idade_min": max(0, agora_min - ts_agora),
        "candles": len(m1),
        "atr_referencia": atr_ref,
        "atr_definicao": f"Wilder, {ATR_PERIODOS} periodos, sobre velas FECHADAS",
        "lambda_definicao": (
            f"Kyle: soma(dP*Vs)/soma(Vs^2) em {KYLE_JANELA} barras, "
            "Vs = volume * sinal(fecho-abertura). O volume do cTrader em CFD/FX e "
            "VOLUME DE TICKS, nao volume transacionado: isto e um proxy de liquidez."),
        "compressao_definicao": f"ATR de agora / mediana do ATR das ultimas {PERCURSO_BARRAS} barras",
        "escalas": escalas,
        "sessoes": sess,
        "digits": (detalhes or {}).get("digits"),
    }
    dados["regua"] = regua(niveis, preco, atr_ref)
    dados["notavel"] = notavel(dados)
    return dados


def notavel(d: dict) -> list[str]:
    """O que salta a vista — por percentil, nao por opiniao.

    Cada frase traz o n de onde saiu. Uma taxa sem amostra e a maneira mais
    simples de dar a uma leitura uma confianca que ela nao ganhou.
    """
    fora = []
    for e in d.get("escalas", []):
        nome = e["nome"]
        comp = e.get("compressao")
        if comp is not None and e.get("compressao_n", 0) >= 10:
            if comp <= 0.7:
                fora.append(f"{nome} comprimido: ATR a {comp:.2f}x do costume "
                            f"(n={e['compressao_n']})")
            elif comp >= 1.5:
                fora.append(f"{nome} esticado: ATR a {comp:.2f}x do costume "
                            f"(n={e['compressao_n']})")
        pct = e.get("lambda_percentil")
        if pct is not None:
            if pct >= 90:
                fora.append(f"{nome} com pouca liquidez: lambda no decil superior "
                            f"de {e['lambda_n']} leituras")
            elif pct <= 10:
                fora.append(f"{nome} com muita liquidez: lambda no decil inferior "
                            f"de {e['lambda_n']} leituras")
        pos = e.get("pos_pct")
        if pos is not None:
            if pos >= 95:
                fora.append(f"preco no topo da faixa de {nome}")
            elif pos <= 5:
                fora.append(f"preco no fundo da faixa de {nome}")
        for ev in e.get("estrutura", {}).get("eventos", [])[-1:]:
            fora.append(f"{nome}: {ev['tipo']} de {ev['lado']} em {ev['preco']:.5g}")
    for s in d.get("sessoes", []):
        if s.get("rompeu") not in (None, "nao"):
            fora.append(f"faixa de {s['nome']} rompida {s['rompeu']} e o preco " +
                        ("voltou para dentro — varrida" if s["voltou_para_dentro"]
                         else "ficou la fora"))
    return fora


# ---------------------------------------------------------------------------
#  Escrever a fotografia para alguem ler
#
#  Nenhum numero e calculado aqui. Se fossem, haveria duas fontes de verdade e
#  mais cedo ou mais tarde a tabela e o guarda discordavam.
# ---------------------------------------------------------------------------
def _n(x, casas: int = 2, vazio: str = "-") -> str:
    if x is None:
        return vazio
    try:
        return f"{float(x):.{casas}f}"
    except (TypeError, ValueError):
        return str(x)


def formatar(d: dict) -> str:
    """A fotografia em texto. E isto, literalmente, que o modelo recebe."""
    linhas = [
        f"{carimbo()} {d['simbolo']} a {_n(d['preco'], 5)}",
        f"{d['agora_utc']} UTC · candle de ha {d['idade_min']} min · {d['candles']} M1",
        "",
        "```",
        f"{'escala':<7}{'ATR':>10}{'compr':>8}{'pos%':>7}{'sentido':>9}{'lambda%':>9}{'n':>7}",
    ]
    for e in d["escalas"]:
        linhas.append(
            f"{e['nome']:<7}{_n(e['atr'], 4):>10}{_n(e['compressao']):>8}"
            f"{_n(e['pos_pct'], 0):>7}{_n(e['sentido_atr']):>9}"
            f"{_n(e['lambda_percentil'], 0):>9}{e['lambda_n']:>7}")
    linhas.append("```")
    linhas.append(f"ATR: {d['atr_definicao']}")
    linhas.append(f"compressao: {d['compressao_definicao']}")
    linhas.append(f"lambda: {d['lambda_definicao']}")

    linhas.append("\n*Estrutura*")
    for e in d["escalas"]:
        est = e["estrutura"]
        sentido = {1: "alta", -1: "baixa", 0: "por definir"}[est["direcao"]]
        evs = ", ".join(f"{x['tipo']} {x['lado']} @{_n(x['preco'], 5)}"
                        for x in est["eventos"]) or "nenhum"
        linhas.append(f"{e['nome']}: estrutura de {sentido} · {evs}")
        for b in e["fvg"]:
            estado = ("preenchido" if b["preenchido"]
                      else "mitigado" if b["mitigado"] else "por tocar")
            linhas.append(f"   fvg {b['lado']} {_n(b['de'], 5)}-{_n(b['ate'], 5)} "
                          f"· {estado} · ha {b['idade_barras']} barras")

    linhas.append("\n*Sessoes (UTC)*")
    for s in d["sessoes"]:
        estado = "aberta" if s["aberta"] else "fechada"
        linhas.append(
            f"{s['nome']:<12} {s['inicio_h']:02d}-{s['fim_h']:02d}h {estado:<8} "
            f"max {_n(s['maxima'], 5)} min {_n(s['minima'], 5)} "
            f"pos {_n(s['posicao_pct'], 0)}% · rompeu {s['rompeu']}"
            + (" e voltou" if s["voltou_para_dentro"] else ""))

    if d.get("notavel"):
        linhas.append("\n*Notavel*")
        linhas += [f"· {x}" for x in d["notavel"]]

    linhas.append("\n*Regua* (nome do nivel · preco · pontos daqui · ATR)")
    linhas.append("```")
    for deg in d["regua"]["degraus"]:
        linhas.append(f"{deg['etiqueta']:<24}{_n(deg['preco'], 5):>12}"
                      f"{_n(deg['pontos'], 2):>10}{_n(deg['atr']):>8}")
    linhas.append("```")
    return "\n".join(linhas)


# ---------------------------------------------------------------------------
#  O guarda: o modelo nao inventa numeros
# ---------------------------------------------------------------------------
_RE_NUMERO = re.compile(r"-?\d+(?:[. ]\d{3})*(?:[.,]\d+)?")
LIVRES_ATE = 24            # inteiros pequenos: contagens, horas, barras
TOLERANCIA_REL = 0.005
TOLERANCIA_ABS = 0.005


def numeros_dos_dados(obj, saida: set | None = None) -> set:
    """Todos os numeros que o codigo produziu. E o universo permitido."""
    saida = set() if saida is None else saida
    if isinstance(obj, bool):
        return saida
    if isinstance(obj, (int, float)):
        if math.isfinite(float(obj)):
            saida.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            numeros_dos_dados(v, saida)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            numeros_dos_dados(v, saida)
    return saida


def _leituras_do_numero(bruto: str) -> list[float]:
    """Como se le "3.159": pode ser tres mil, pode ser tres virgula um.

    Devolvo as duas leituras possiveis e o guarda aceita se QUALQUER uma bater.
    Ser rigoroso nesta ambiguidade nao apanharia mais invencoes — apanharia
    numeros certos escritos a portuguesa.
    """
    t = bruto.replace(" ", "").replace(" ", "").replace(" ", "")
    candidatos = []
    if "," in t:
        candidatos.append(t.replace(".", "").replace(",", "."))
    elif t.count(".") > 1:
        candidatos.append(t.replace(".", ""))
    elif "." in t:
        candidatos.append(t)
        candidatos.append(t.replace(".", ""))
    else:
        candidatos.append(t)
    valores = []
    for s in candidatos:
        try:
            valores.append(float(s))
        except ValueError:
            pass
    return valores


def numero_inventado(texto: str, permitidos: set) -> str | None:
    """O primeiro numero do texto que nao veio dos dados, tal como escrito.

    Devolver o numero COMO ELE O ESCREVEU e o que faz o ciclo de correcao
    funcionar: "o numero 3.500 nao esta nos dados" e uma instrucao; "numero
    invalido" nao e nada.
    """
    inteiro = str(texto or "")
    for achado in _RE_NUMERO.finditer(inteiro):
        bruto = achado.group(0)
        valores = _leituras_do_numero(bruto)
        if not valores:
            continue
        seguinte = inteiro[achado.end():achado.end() + 2].lstrip()
        e_taxa = seguinte.startswith("%")
        # Passe livre a inteiros pequenos: contagens, horas, numeros de barras.
        # Menos as percentagens — uma taxa e sempre uma afirmacao sobre os
        # dados, por pequena que seja.
        if not e_taxa and all(v == int(v) and abs(v) <= LIVRES_ATE for v in valores):
            continue
        if any(any(abs(v - p) <= max(TOLERANCIA_ABS, TOLERANCIA_REL * abs(p))
                   for p in permitidos) for v in valores):
            continue
        return bruto
    return None


# ===========================================================================
#  O MODELO
# ===========================================================================

_CERCA = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _candidatos(texto: str):
    """O JSON pode vir cru, cercado, ou no meio de conversa. Tenta os tres."""
    yield texto
    for bloco in _CERCA.findall(texto):
        yield bloco
    for abre, fecha in (("{", "}"), ("[", "]")):
        inicio = texto.find(abre)
        if inicio < 0:
            continue
        nivel, dentro, escapado = 0, False, False
        for i in range(inicio, len(texto)):
            ch = texto[i]
            if escapado:
                escapado = False
                continue
            if ch == "\\":
                escapado = True
            elif ch == '"':
                dentro = not dentro
            elif not dentro and ch == abre:
                nivel += 1
            elif not dentro and ch == fecha:
                nivel -= 1
                if nivel == 0:
                    yield texto[inicio:i + 1]
                    break


def extrair_json(texto: str):
    if not (texto or "").strip():
        raise ErroModelo("resposta vazia do modelo")
    for bruto in _candidatos(texto):
        try:
            return json.loads(bruto)
        except (json.JSONDecodeError, TypeError):
            continue
    raise ErroModelo(f"nao encontrei JSON valido na resposta: {texto[:300]}")


def erro_de_estado(r, modelo: str) -> ErroModelo:
    """Traduz o HTTP do Ollama numa coisa que se possa fazer.

    Adivinhar aqui e o que faz um 402 chegar ao utilizador vestido de erro de
    formato, e manda-lo procurar o problema no sitio errado.
    """
    corpo = (r.text or "")[:300]
    if r.status_code == 402:
        return ErroModelo(f"o Ollama recusou {modelo}: {corpo}", permanente=True,
                          pista="Subscricao ou creditos, ou um modelo local pedido com :cloud.")
    if r.status_code in (401, 403):
        return ErroModelo(f"o Ollama recusou a autenticacao: {corpo}", permanente=True,
                          pista="Corre `ollama signin`.")
    if r.status_code == 404:
        return ErroModelo(f"o Ollama nao conhece {modelo}: {corpo}", permanente=True,
                          pista=f"Corre `ollama pull {modelo}`.")
    if r.status_code == 429:
        return ErroModelo(f"o Ollama pediu para esperar: {corpo}",
                          pista="Limite de ritmo. Espera e tenta outra vez.")
    return ErroModelo(f"o Ollama devolveu {r.status_code}: {corpo}")


class Ollama:
    def __init__(self, url: str = OLLAMA_URL, timeout: int = TIMEOUT_MODELO):
        self.url, self.timeout = url.rstrip("/"), timeout

    def conversar(self, sistema: str, utilizador: str, *, modelo: str = MODELO,
                  json_mode: bool = True) -> str:
        carga = {
            "model": modelo,
            "messages": [{"role": "system", "content": sistema},
                         {"role": "user", "content": utilizador}],
            "stream": False,
            "options": {"temperature": 0.2},
        }
        # num_ctx SO para modelos locais: um modelo de nuvem decide o contexto
        # do lado dele, e impor-lhe um valor pode so atrasa-lo.
        if modelo in JANELA_MODELO:
            carga["options"]["num_ctx"] = JANELA_MODELO[modelo]
        if json_mode:
            carga["format"] = "json"
        try:
            r = requests.post(f"{self.url}/api/chat", json=carga, timeout=self.timeout)
        except requests.exceptions.Timeout:
            raise ErroModelo(f"o modelo {modelo} nao respondeu em {self.timeout}s.") from None
        except requests.exceptions.ConnectionError:
            raise ErroModelo(f"nao consegui falar com o Ollama em {self.url}",
                             pista="O Ollama esta a correr?") from None
        if not r.ok:
            raise erro_de_estado(r, modelo)
        try:
            return r.json()["message"]["content"]
        except (ValueError, KeyError) as e:
            raise ErroModelo(f"resposta do Ollama inesperada: {(r.text or '')[:200]}") from e


class ModeloFalso:
    """Respostas guionadas. E com isto que o autoteste corre sem Ollama."""

    def __init__(self, respostas: list[str]):
        self.respostas = list(respostas)
        self.chamadas: list[dict] = []

    def conversar(self, sistema: str, utilizador: str, *, modelo: str = MODELO,
                  json_mode: bool = True) -> str:
        self.chamadas.append({"sistema": sistema, "utilizador": utilizador,
                              "modelo": modelo})
        if not self.respostas:
            raise ErroModelo("ModeloFalso ficou sem respostas guionadas")
        return self.respostas.pop(0)


def correr_agente(llm, *, papel: str, sistema: str, prompt: str, validar: Callable,
                  modelo: str = MODELO, tentativas: int = TENTATIVAS_JSON):
    """Pergunta, valida, e devolve o motivo da rejeicao para ele se corrigir.

    O motivo volta escrito PARA ELE. "o numero 3.500 nao esta nos dados" e uma
    instrucao; "resposta invalida" nao e nada, e a segunda tentativa sai igual
    a primeira.
    """
    ultimo, gastas, houve_resposta = "", 0, False
    for _ in range(max(1, tentativas)):
        gastas += 1
        pedido = prompt
        if ultimo:
            pedido = (f"{prompt}\n\n--- A TUA RESPOSTA ANTERIOR FOI REJEITADA ---\n"
                      f"Motivo: {ultimo}\n"
                      f"Corrige e devolve APENAS o JSON no formato pedido, "
                      f"sem texto a volta.")
        try:
            bruto = llm.conversar(sistema, pedido, modelo=modelo, json_mode=True)
            houve_resposta = True
            return validar(extrair_json(bruto))
        except ErroModelo as e:
            ultimo = str(e)
            if e.permanente:
                break
        except ValueError as e:
            ultimo = str(e)
    conta = ("parou a primeira tentativa — repetir nao ia mudar nada"
             if gastas == 1 and not houve_resposta else f"falhou {gastas} tentativas")
    raise ErroAgente(f"[{papel}] o modelo {modelo} {conta}.\nUltimo erro: {ultimo}")


# ---------------------------------------------------------------------------
#  As ferramentas: noticias, e o que esta a acontecer
# ---------------------------------------------------------------------------
CABECALHOS_WEB = {
    "User-Agent": "Mozilla/5.0 (compatible; agente-trading/1.0)",
    "Accept-Language": "pt-PT,pt;q=0.9,en;q=0.8",
}


def _texto_de_html(bruto: str) -> str:
    """Tirar as tags. Nao e um parser a serio, e de proposito que nao e."""
    t = re.sub(r"(?is)<(script|style|nav|footer|header|form|svg|noscript)[^>]*>.*?</\1>", " ", bruto)
    t = re.sub(r"(?s)<!--.*?-->", " ", t)
    t = re.sub(r"(?i)<(br|/p|/div|/li|/h[1-6]|/tr)[^>]*>", "\n", t)
    t = re.sub(r"(?s)<[^>]+>", " ", t)
    t = desescapar_html(t)
    t = "\n".join(re.sub(r"[ \t]+", " ", linha).strip() for linha in t.splitlines())
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def ler_pagina(url: str, limite: int = WEB_MAX_CARACTERES) -> tuple[str, str]:
    if not str(url).startswith(("http://", "https://")):
        raise ErroWeb(f"isso nao e um endereco: {url!r}")
    try:
        r = requests.get(url, headers=CABECALHOS_WEB, timeout=WEB_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise ErroWeb(f"nao consegui abrir {url}: {e}") from e
    if not r.ok:
        raise ErroWeb(f"{url} devolveu {r.status_code}")
    tipo = (r.headers.get("Content-Type") or "").lower()
    if "html" not in tipo and "text" not in tipo:
        raise ErroWeb(f"{url} nao e texto ({tipo or 'sem tipo'})")
    titulo = ""
    achado = re.search(r"(?is)<title[^>]*>(.*?)</title>", r.text)
    if achado:
        titulo = desescapar_html(achado.group(1)).strip()[:200]
    texto = _texto_de_html(r.text)
    if not texto:
        raise ErroWeb(f"{url} nao tinha texto nenhum")
    if len(texto) > limite:
        texto = texto[:limite] + "\n\n[... pagina cortada aqui ...]"
    return (titulo or url), texto


def procurar_web(pergunta: str, n: int = 5) -> list[dict]:
    """DuckDuckGo pelo endpoint HTML: sem chave de API e sem dependencia."""
    try:
        r = requests.post("https://html.duckduckgo.com/html/", data={"q": pergunta},
                          headers=CABECALHOS_WEB, timeout=WEB_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise ErroWeb(f"a pesquisa falhou: {e}") from e
    if not r.ok:
        raise ErroWeb(f"a pesquisa devolveu {r.status_code}")
    fora, vistos = [], set()
    for achado in re.finditer(r'(?is)<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                              r.text):
        url, titulo = achado.group(1), _texto_de_html(achado.group(2))[:200]
        redirect = re.search(r"[?&]uddg=([^&]+)", url)
        if redirect:
            url = urllib.parse.unquote(redirect.group(1))
        if url in vistos or not url.startswith("http"):
            continue
        vistos.add(url)
        fora.append({"url": url, "titulo": titulo})
        if len(fora) >= n:
            break
    return fora


class Ferramentas:
    """O que o agente pode pedir, e o orcamento com que o pode pedir.

    Devolve texto, sempre — mesmo quando falha. Uma ferramenta que levanta no
    meio do turno do modelo mata a decisao inteira por causa de uma pagina que
    nao abriu, e isso e trocar uma decisao por um erro de rede.
    """

    def __init__(self, m1=None):
        self.m1 = m1 or []
        self.consultadas: list[dict] = []

    def descrever(self) -> str:
        return (
            "FERRAMENTAS — pede uma de cada vez, e so se precisares:\n"
            '  {"ferramenta": "procurar", "pergunta": "..."}   pesquisa na web\n'
            '  {"ferramenta": "ler", "url": "https://..."}     le uma pagina\n'
            '  {"ferramenta": "velas", "escala": "H1", "n": 20}  as ultimas velas em cru\n'
            "\nDepois de teres o que precisas, responde com o JSON pedido.\n"
            f"Tens no maximo {MAX_FERRAMENTAS} consultas.")

    def executar(self, pedido: dict) -> str:
        nome = str(pedido.get("ferramenta") or "").strip().lower()
        try:
            if nome == "procurar":
                pergunta = str(pedido.get("pergunta") or "").strip()
                if not pergunta:
                    return "procurar precisa de uma pergunta."
                achados = procurar_web(pergunta)
                self.consultadas.append({"tipo": "procurar", "o_que": pergunta,
                                         "achados": achados})
                if not achados:
                    return f"sem resultados para {pergunta!r}."
                return "\n".join(f"- {a['titulo']}\n  {a['url']}" for a in achados)
            if nome == "ler":
                url = str(pedido.get("url") or "").strip()
                titulo, texto = ler_pagina(url)
                self.consultadas.append({"tipo": "ler", "o_que": url, "titulo": titulo})
                return f"{titulo}\n{url}\n\n{texto}"
            if nome == "velas":
                return self._velas(pedido)
            return (f"nao conheco a ferramenta {nome!r}. "
                    "Ha procurar, ler e velas.")
        except (ErroWeb, ValueError) as e:
            return f"a ferramenta {nome} falhou: {e}"

    def _velas(self, pedido: dict) -> str:
        escala = str(pedido.get("escala") or "H1").strip().upper()
        minutos = dict(ESCALAS).get(escala)
        if minutos is None:
            return f"nao conheco a escala {escala!r}. Ha {', '.join(n for n, _ in ESCALAS)}."
        try:
            quantas = max(1, min(60, int(pedido.get("n") or 20)))
        except (TypeError, ValueError):
            quantas = 20
        if not self.m1:
            return "nao tenho velas para te dar."
        agora = int(self.m1[-1][T])
        velas = fechadas(agrupar(self.m1, minutos), minutos, agora)[-quantas:]
        self.consultadas.append({"tipo": "velas", "o_que": f"{escala} x{len(velas)}"})
        linhas = [f"{escala}, {len(velas)} velas fechadas (utc, abertura, maxima, minima, fecho, volume)"]
        for v in velas:
            quando = datetime.fromtimestamp(v[T] * 60, timezone.utc).strftime("%m-%d %H:%M")
            linhas.append(f"{quando} {v[O]:.5g} {v[H]:.5g} {v[L]:.5g} {v[C]:.5g} {v[V]:.0f}")
        return "\n".join(linhas)


def correr_agente_com_ferramentas(llm, *, papel: str, sistema: str, prompt: str,
                                  validar: Callable, ferramentas: Ferramentas,
                                  formato: str = "", modelo: str = MODELO,
                                  tentativas: int = TENTATIVAS_JSON):
    """Como `correr_agente`, mas ele pode pedir coisas antes de responder.

    O `llm.conversar` e de um turno so, por isso a conversa e remontada a cada
    volta. O lembrete do formato vai SEMPRE no fim: e a ultima coisa que ele le
    antes de escrever, e e a que ele mais esquece.
    """
    historico: list[tuple[str, str]] = []
    ultimo, consultas, falhadas, descartados = "", 0, 0, 0

    while consultas < MAX_FERRAMENTAS + 1 and falhadas < max(1, tentativas):
        partes = [prompt, "", ferramentas.descrever()]
        if historico:
            partes.append("\n--- O QUE JA CONSULTASTE ---")
            for pedido, resultado in historico:
                partes.append(f"> {pedido}\n{resultado}")
        if descartados:
            partes.append(f"\n[{descartados} consultas anteriores descartadas: "
                          f"contexto cheio. Nao contes com elas.]")
        if consultas >= MAX_FERRAMENTAS:
            partes.append("\nAcabaram-se as consultas. Responde com o que tens.")
        if ultimo:
            partes.append(f"\n--- A TUA RESPOSTA ANTERIOR FOI REJEITADA ---\nMotivo: {ultimo}")
        if formato:
            partes.append(f"\n--- O QUE TENS DE DEVOLVER, SE JA SOUBERES ---\n{formato}")

        try:
            bruto = llm.conversar(sistema, "\n".join(partes), modelo=modelo, json_mode=True)
            dados = extrair_json(bruto)
        except ErroModelo as e:
            ultimo, falhadas = str(e), falhadas + 1
            if e.permanente:
                break
            continue

        if isinstance(dados, dict) and dados.get("ferramenta"):
            if consultas >= MAX_FERRAMENTAS:
                ultimo = "acabaram-se as consultas: responde com o que ja tens."
                falhadas += 1
                continue
            consultas += 1
            resultado = ferramentas.executar(dados)
            historico.append((json.dumps(dados, ensure_ascii=False)[:200], resultado))
            gasto = sum(len(r) for _, r in historico)
            while gasto > ORCAMENTO_FERRAMENTAS and len(historico) > 1:
                historico.pop(0)
                descartados += 1
                gasto = sum(len(r) for _, r in historico)
            ultimo = ""
            continue

        try:
            return validar(dados)
        except ValueError as e:
            ultimo, falhadas = str(e), falhadas + 1

    raise ErroAgente(f"[{papel}] o modelo {modelo} nao chegou a uma resposta valida "
                     f"({consultas} consultas, {falhadas} rejeicoes).\nUltimo erro: {ultimo}")


# ===========================================================================
#  OS PROMPTS
#
#  E AQUI que mexes. Nao ha um `if` de estrategia neste ficheiro: se o queres
#  mais agressivo, mais seletivo, ou a operar outra coisa, muda-se este texto e
#  mais nada. O codigo continua a medir os mesmos numeros e a verificar as
#  mesmas contas — o que muda e quem le.
# ===========================================================================

COMO_SE_LE = """\
COMO SE LE A FOTOGRAFIA

A tabela tem uma linha por escala (M15, H1, H4, D1). Todas sao agregadas dos
mesmos M1, alinhadas ao relogio UTC, e a barra que ainda se esta a formar esta
DESCARTADA — o que la esta ja fechou e nao muda mais.

  ATR      amplitude tipica dessa escala, Wilder. E a tua unidade de distancia.
           "longe" e "perto" nao querem dizer nada; 1.5 ATR quer.
  compr    ATR de agora a dividir pelo ATR do costume. Abaixo de 1 esta
           comprimido, acima esta esticado. Vem com o n: sem n nao decidas.
  pos%     onde o preco esta dentro da faixa recente dessa escala. Pode passar
           de 100 ou ficar abaixo de 0 — isso quer dizer que saiu da faixa, e e
           informacao, nao um erro.
  sentido  percurso do preco contra o de N barras atras, EM ATR. Em ATR para
           poderes comparar escalas: +2 no M15 e +2 no D1 nao sao a mesma
           viagem, mas sao a mesma medida.
  lambda%  percentil da iliquidez (Kyle). Alto = o preco mexe muito por unidade
           de volume = pouca liquidez = os stops respiram menos e as varridas
           sao maiores. Vem com o n. E volume de TICKS, nao volume negociado:
           e um proxy, trata-o como tal.

ESCALAS QUE DISCORDAM SAO INFORMACAO, NAO UM PROBLEMA A RESOLVER
O D1 comprimido com o M15 esticado nao e uma contradicao: e um movimento curto
dentro de um intervalo longo. O H4 a subir com o M15 a descer e uma correcao,
ou o principio de uma inversao — e sao coisas diferentes, e a diferenca esta na
estrutura, nao na tabela. Nunca escolhas a escala que te da a resposta que
querias: diz o que cada uma diz, e depois decide com todas.

ESTRUTURA
  BOS    fecho para la do ultimo pivo, NA direcao que a estrutura ja tinha.
         Continuacao.
  CHoCH  o mesmo fecho, mas CONTRA a direcao que estava. E o primeiro sinal de
         que a estrutura mudou de dono. Um CHoCH no H4 vale mais do que dez no
         M15.
  FVG    buraco de tres velas: o preco andou sem deixar negocio atras. "por
         tocar" e diferente de "mitigado" e diferente de "preenchido" — um
         buraco ja preenchido nao e um nivel, e historia.

SESSOES, EM UTC
A hora e sempre UTC, nunca a tua. A faixa da Asia e estreita e serve de isco: a
abertura de Londres varre-a muitas vezes antes de ir para o outro lado — por
isso "rompeu e voltou para dentro" (varrida) e "rompeu e ficou la fora"
(rompimento) sao acontecimentos opostos com a mesma aparencia. A sobreposicao
Londres/Nova Iorque e onde ha mais volume e onde os movimentos vao mais longe.
Uma sessao FECHADA continua a ter niveis validos: a faixa fica la.

COMO SE ESCOLHE UM NIVEL
Tu NAO escreves precos. Escolhes um nivel da regua PELO NOME, e dizes quantos
ATR para la (ou para ca) dele. O preco e calculado deste lado.
  - um nivel bom e um sitio onde ja houve negocio: um extremo, um pivo, uma
    borda de FVG, uma ponta de sessao;
  - se o teu take fica para la de outro nivel, o preco tem de romper esse nivel
    antes de te pagar — conta com isso ou escolhe outro;
  - um stop dentro de uma faixa de sessao esta na zona que a varrida apanha.

FICAR DE FORA E UMA DECISAO COMPLETA
Nao e a ausencia de uma decisao, nem um empate, nem falta de coragem. A maior
parte das horas nao tem nada. Se a tabela nao mostra um padrao, diz que nao ha
— inventar uma historia para justificar uma entrada e exatamente como se perde
dinheiro devagar.

REGRAS DOS NUMEROS, E SAO MESMO REGRAS
1. NAO INVENTES NUMEROS. Todos os numeros que escreveres tem de aparecer nos
   dados que te dou. A conta ja foi feita: nao ha nada para arredondar de
   cabeca.
2. Diz sempre o que te faria mudar de ideias.
3. Portugues, direto, sem entusiasmo e sem avisos legais."""


SISTEMA_OBSERVA = COMO_SE_LE + """

O TEU TRABALHO AGORA
Olhas para o mercado e decides uma de duas coisas: armar um nivel, ou esperar.

Armar nao e entrar. E dizer "se o preco chegar aqui, quero voltar a olhar" — e
quando ele chegar eu acordo-te outra vez, com o mercado desse instante, e so
ai e que decides entrar. Por isso podes armar um sitio onde ainda nao querias
entrar hoje: o que estas a escolher e onde vale a pena olhar melhor.

Responde SO com JSON:
{"acao": "armar" | "esperar",
 "nivel": "<o nome exato de um nivel da regua>",
 "afastamento_atr": <numero, pode ser negativo para o lado de baixo>,
 "lado": "compra" | "venda",
 "porque": "o que te faz olhar para ai, 1 a 3 frases",
 "invalidacao": "o que mata esta ideia antes de o preco la chegar",
 "validade_min": <minutos que isto vale>}

Com "esperar", `nivel`, `afastamento_atr` e `lado` sao ignorados — mas `porque`
nao: diz o que estas a ver que te faz ficar quieto, porque isso tambem e
leitura."""


SISTEMA_TOCOU = COMO_SE_LE + """

O TEU TRABALHO AGORA
O preco tocou o nivel que armaste. Esta e a fotografia DESSE instante, nao a de
quando armaste — o mercado mudou entretanto, e e por isso que te estou a
perguntar outra vez em vez de entrar sozinho.

Decides: entrar, ou desistir. Desistir aqui e barato e e comum — chegar ao
nivel nao obriga a nada. Se o que la esta agora nao e o que esperavas, diz.

Se entrares, escolhes o stop e o take PELO NOME de um nivel mais um
afastamento em ATR. O stop tem de ficar do lado errado do preco para a tua
posicao (abaixo numa compra, acima numa venda) e o take do lado certo — eu
verifico isso e recuso o que nao bater.

Responde SO com JSON:
{"acao": "entrar" | "desistir",
 "stop_nivel": "<nome exato de um nivel da regua>",
 "stop_afastamento_atr": <numero>,
 "take_nivel": "<nome exato de um nivel da regua>",
 "take_afastamento_atr": <numero>,
 "porque": "porque e que entras ou desistes, 1 a 3 frases"}

Com "desistir", os campos do stop e do take sao ignorados."""


SISTEMA_DENTRO = COMO_SE_LE + """

O TEU TRABALHO AGORA
Tens uma posicao aberta. Decides: aguentar, mexer no stop/take, ou sair.

  aguentar  a tese continua de pe. E a resposta certa na maior parte das vezes,
            e mexer por mexer so estreita o espaco que a posicao precisa.
  mexer     a tese continua mas os niveis mudaram. Diz os dois, stop e take,
            mesmo que so queiras mudar um.
  sair      a tese morreu, ou ja se cumpriu. Sair antes do stop e uma decisao
            legitima; esperar pelo stop "para ver" nao e.

Nota: o stop so se move a favor da posicao. Nao e uma opiniao sobre mercado — a
posicao foi dimensionada para o stop original, e afasta-lo agora e aumentar um
risco que ja foi assumido. Se pedires um stop que afasta, eu recuso e mantenho
o que la esta.

Responde SO com JSON:
{"acao": "aguentar" | "mexer" | "sair",
 "stop_nivel": "<nome exato de um nivel da regua>",
 "stop_afastamento_atr": <numero>,
 "take_nivel": "<nome exato de um nivel da regua>",
 "take_afastamento_atr": <numero>,
 "porque": "1 a 3 frases"}

Com "aguentar" e com "sair", os campos do stop e do take sao ignorados."""


FORMATO_OBSERVA = ('{"acao": "armar"|"esperar", "nivel": "...", '
                   '"afastamento_atr": 0.0, "lado": "compra"|"venda", '
                   '"porque": "...", "invalidacao": "...", "validade_min": 240}')
FORMATO_TOCOU = ('{"acao": "entrar"|"desistir", "stop_nivel": "...", '
                 '"stop_afastamento_atr": 0.0, "take_nivel": "...", '
                 '"take_afastamento_atr": 0.0, "porque": "..."}')
FORMATO_DENTRO = ('{"acao": "aguentar"|"mexer"|"sair", "stop_nivel": "...", '
                  '"stop_afastamento_atr": 0.0, "take_nivel": "...", '
                  '"take_afastamento_atr": 0.0, "porque": "..."}')


# ===========================================================================
#  DO QUE ELE DIZ PARA UM PRECO
#
#  O agente escolhe um NOME e um afastamento em ATR. O preco nasce aqui. E o
#  que mantem o guarda dos numeros a funcionar, o que impede um stop a 30 ATR
#  por um zero a mais, e o que o obriga a ancorar a decisao num nivel que
#  existe em vez de num numero que lhe soa bem.
# ===========================================================================

def nivel_para_preco(regua_: dict, nome: str, afastamento_atr) -> tuple[float, dict]:
    """`nome` + `afastamento_atr` -> preco. Levanta ValueError PARA O MODELO."""
    alvo = str(nome or "").strip().lower()
    if not alvo:
        raise ValueError("faltou o nome do nivel. Escolhe um da regua, tal como esta escrito.")
    degrau = next((d for d in regua_["degraus"] if d["etiqueta"].lower() == alvo), None)
    if degrau is None:
        nomes = ", ".join(d["etiqueta"] for d in regua_["degraus"])
        raise ValueError(f"nao ha nenhum nivel chamado {nome!r} na regua. "
                         f"Os que existem sao: {nomes}. Usa um deles, tal como esta escrito.")
    try:
        afastamento = float(afastamento_atr or 0)
    except (TypeError, ValueError):
        raise ValueError(f"afastamento_atr tem de ser um numero, e mandaste "
                         f"{afastamento_atr!r}.") from None
    if not math.isfinite(afastamento) or abs(afastamento) > MAX_AFASTAMENTO_ATR:
        raise ValueError(f"afastamento_atr de {afastamento} esta fora do alcance "
                         f"(-{MAX_AFASTAMENTO_ATR} a {MAX_AFASTAMENTO_ATR}). "
                         f"Um afastamento maior do que isso ja nao e um nivel.")
    atr_ref = regua_.get("atr_ref")
    if not atr_ref:
        raise ValueError("nao tenho ATR de referencia, por isso nao consigo "
                         "converter ATR em preco. Diz afastamento_atr igual a 0.")
    return degrau["preco"] + afastamento * atr_ref, degrau


def _texto_da_resposta(d: dict, chaves: Sequence[str]) -> str:
    return " ".join(str(d.get(k) or "") for k in chaves)


def validar_observa(d, regua_: dict, permitidos: set) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"esperava um objeto JSON e recebi {type(d).__name__}.")
    acao = str(d.get("acao") or "").strip().lower()
    if acao not in ("armar", "esperar"):
        raise ValueError(f'acao tem de ser "armar" ou "esperar", e mandaste {d.get("acao")!r}.')
    if not str(d.get("porque") or "").strip():
        raise ValueError("falta o `porque`. Mesmo para esperar: dizer o que ves "
                         "que te faz ficar quieto tambem e leitura.")
    mau = numero_inventado(_texto_da_resposta(d, ("porque", "invalidacao")), permitidos)
    if mau:
        raise ValueError(f"o numero {mau} nao esta nos dados que te dei. So podes usar "
                         f"numeros que aparecam na tabela ou na regua. Reescreve sem ele.")

    fora = {"acao": acao, "porque": str(d.get("porque")).strip(),
            "invalidacao": str(d.get("invalidacao") or "").strip()}
    if acao == "esperar":
        return fora

    lado = str(d.get("lado") or "").strip().lower()
    if lado not in ("compra", "venda"):
        raise ValueError(f'para armar, lado tem de ser "compra" ou "venda", '
                         f'e mandaste {d.get("lado")!r}.')
    preco, degrau = nivel_para_preco(regua_, d.get("nivel"), d.get("afastamento_atr"))
    try:
        validade = int(d.get("validade_min") or VALIDADE_OMISSAO_MIN)
    except (TypeError, ValueError):
        validade = VALIDADE_OMISSAO_MIN
    fora.update({
        "lado": lado,
        "nivel": degrau["etiqueta"],
        "afastamento_atr": float(d.get("afastamento_atr") or 0),
        "gatilho": preco,
        # De que lado e que o preco tem de vir para "tocar". Fixa-se agora, com
        # o preco de agora, e nao no momento do toque: decidir isto depois seria
        # escolher a regra ja a saber o resultado.
        "toque": "abaixo" if preco < regua_["preco"] else "acima",
        "validade_min": max(1, min(validade, VALIDADE_MAX_MIN)),
    })
    return fora


def _validar_stop_take(d: dict, regua_: dict, lado: str) -> dict:
    """Os precos do stop e do take, e a verificacao de que estao dos lados certos.

    Isto e aritmetica, nao estrategia: um stop acima do preco numa compra nao e
    uma escolha arrojada, e uma ordem que o broker recusa.
    """
    stop, _ = nivel_para_preco(regua_, d.get("stop_nivel"), d.get("stop_afastamento_atr"))
    take, _ = nivel_para_preco(regua_, d.get("take_nivel"), d.get("take_afastamento_atr"))
    preco = regua_["preco"]
    sinal = 1 if lado == "compra" else -1
    if sinal * (preco - stop) <= 0:
        raise ValueError(
            f"numa {lado}, o stop tem de ficar "
            f"{'abaixo' if lado == 'compra' else 'acima'} do preco ({preco:.5g}), "
            f"e o que escolheste da {stop:.5g}. Escolhe outro nivel ou outro afastamento.")
    if sinal * (take - preco) <= 0:
        raise ValueError(
            f"numa {lado}, o take tem de ficar "
            f"{'acima' if lado == 'compra' else 'abaixo'} do preco ({preco:.5g}), "
            f"e o que escolheste da {take:.5g}. Escolhe outro nivel ou outro afastamento.")
    risco = abs(preco - stop)
    return {"stop": stop, "take": take, "risco_pts": risco,
            "R": (abs(take - preco) / risco) if risco > 0 else None}


def validar_tocou(d, regua_: dict, permitidos: set, lado: str) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"esperava um objeto JSON e recebi {type(d).__name__}.")
    acao = str(d.get("acao") or "").strip().lower()
    if acao not in ("entrar", "desistir"):
        raise ValueError(f'acao tem de ser "entrar" ou "desistir", e mandaste {d.get("acao")!r}.')
    if not str(d.get("porque") or "").strip():
        raise ValueError("falta o `porque`.")
    mau = numero_inventado(str(d.get("porque")), permitidos)
    if mau:
        raise ValueError(f"o numero {mau} nao esta nos dados que te dei. Reescreve sem ele.")
    fora = {"acao": acao, "porque": str(d.get("porque")).strip()}
    if acao == "desistir":
        return fora
    fora.update(_validar_stop_take(d, regua_, lado))
    fora["stop_nivel"] = str(d.get("stop_nivel"))
    fora["take_nivel"] = str(d.get("take_nivel"))
    return fora


def validar_dentro(d, regua_: dict, permitidos: set, lado: str) -> dict:
    if not isinstance(d, dict):
        raise ValueError(f"esperava um objeto JSON e recebi {type(d).__name__}.")
    acao = str(d.get("acao") or "").strip().lower()
    if acao not in ("aguentar", "mexer", "sair"):
        raise ValueError(f'acao tem de ser "aguentar", "mexer" ou "sair", '
                         f'e mandaste {d.get("acao")!r}.')
    if not str(d.get("porque") or "").strip():
        raise ValueError("falta o `porque`.")
    mau = numero_inventado(str(d.get("porque")), permitidos)
    if mau:
        raise ValueError(f"o numero {mau} nao esta nos dados que te dei. Reescreve sem ele.")
    fora = {"acao": acao, "porque": str(d.get("porque")).strip()}
    if acao != "mexer":
        return fora
    fora.update(_validar_stop_take(d, regua_, lado))
    fora["stop_nivel"] = str(d.get("stop_nivel"))
    fora["take_nivel"] = str(d.get("take_nivel"))
    return fora


def stop_a_favor(lado: str, stop_atual, stop_novo: float) -> bool:
    """O stop novo aperta o risco, ou alarga-o?

    Nao e uma regra de mercado: a posicao foi dimensionada para o stop original,
    e alargar depois e aumentar retroactivamente um risco ja assumido. Fica com
    interruptor (STOP_SO_A_FAVOR) porque quem discorda tem o direito de o dizer.
    """
    if stop_atual is None:
        return True
    return (stop_novo >= float(stop_atual)) if lado == "compra" else (stop_novo <= float(stop_atual))


def calcular_volume(saldo: float, risco_pts: float, detalhes: dict) -> tuple[int, str]:
    """Quantas unidades, a partir do risco e da distancia ao stop.

    ISTO PRESSUPOE QUE A MOEDA DE COTACAO DO SIMBOLO E A MOEDA DA CONTA. Num
    ETHUSD com conta em USD isso e verdade. Nao e verdade num EURJPY com conta
    em USD, e eu nao tenho aqui a taxa de conversao — por isso a suposicao vai
    escrita na mensagem em vez de ficar escondida a produzir um volume que
    parece certo.

    O volume do cTrader vem em centesimos de unidade: por isso o x100, e por
    isso o minVolume e o stepVolume ja vem nessa mesma escala.
    """
    if risco_pts <= 0:
        raise ValueError("a distancia ao stop e zero: nao da para dimensionar nada.")
    passo = int(detalhes.get("stepVolume") or 0) or 1
    minimo = int(detalhes.get("minVolume") or 0) or passo
    maximo = int(detalhes.get("maxVolume") or 0)

    dinheiro = saldo * (RISCO_POR_TRADE_PCT / 100.0)
    unidades = dinheiro / risco_pts
    bruto = int(unidades * 100)
    volume = (bruto // passo) * passo

    if volume < minimo:
        raise ValueError(
            f"o volume que sai de {RISCO_POR_TRADE_PCT}% de {saldo:.2f} com um stop "
            f"de {risco_pts:.5g} pontos e {bruto} (arredondado a {volume}), e o minimo "
            f"deste simbolo e {minimo}. Ou o stop e demasiado largo para esta conta, "
            f"ou o RISCO_POR_TRADE_PCT e demasiado pequeno.")
    if maximo and volume > maximo:
        volume = (maximo // passo) * passo
    nota = (f"{volume} (risco {RISCO_POR_TRADE_PCT}% de {saldo:.2f}, stop "
            f"{risco_pts:.5g} pts, passo {passo}) — assume moeda de cotacao = moeda da conta")
    return volume, nota


# ===========================================================================
#  O ESTADO
#
#  Persistido porque um restart no meio de um nivel armado — ou pior, no meio
#  de uma posicao aberta — nao pode fazer o agente esquecer-se do que estava a
#  fazer. A posicao continua la no broker de qualquer maneira.
# ===========================================================================

ESQUEMA = """
CREATE TABLE IF NOT EXISTS maquina (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    momento     TEXT NOT NULL DEFAULT 'observa',
    armado      TEXT,
    posicao     TEXT,
    mudou       REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS decisoes (
    id          TEXT PRIMARY KEY,
    momento     TEXT NOT NULL,
    quando      REAL NOT NULL,
    simbolo     TEXT,
    preco       REAL,
    ts_min      INTEGER,
    entrada     TEXT,      -- a fotografia que ele viu, tal e qual
    resposta    TEXT,      -- o JSON que ele devolveu
    consultas   TEXT,      -- o que ele foi ler
    feito       TEXT,      -- o que o codigo fez a seguir
    erro        TEXT
);
CREATE TABLE IF NOT EXISTS posicoes (
    id            TEXT PRIMARY KEY,
    position_id   INTEGER,
    simbolo       TEXT,
    lado          TEXT,
    volume        INTEGER,
    preco_entrada REAL,
    stop          REAL,
    take          REAL,
    risco_pts     REAL,
    armado_em     REAL,
    aberta_em     REAL,
    fechada_em    REAL,
    preco_saida   REAL,
    r_realizado   REAL,
    motivo        TEXT
);
CREATE INDEX IF NOT EXISTS i_decisoes ON decisoes(quando);
CREATE INDEX IF NOT EXISTS i_posicoes ON posicoes(aberta_em);
"""


class Estado:
    def __init__(self, caminho: Path | str | None = None):
        self.caminho = Path(caminho or BD)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.bd = sqlite3.connect(str(self.caminho), timeout=30, isolation_level=None)
        self.bd.row_factory = sqlite3.Row
        self.bd.execute("PRAGMA journal_mode=WAL")
        self.bd.execute("PRAGMA synchronous=NORMAL")
        self.bd.executescript(ESQUEMA)
        # O sqlite3 recusa uma ligacao usada noutra thread, e a mensagem que
        # ele da nao diz o que fazer. Guardo a thread para poder dizer eu.
        self._thread = threading.get_ident()
        self.bd.execute(
            "INSERT OR IGNORE INTO maquina (id, momento, mudou) VALUES (1, 'observa', ?)",
            (time.time(),))

    def exigir_mesma_thread(self, quem: str = "este componente") -> None:
        if threading.get_ident() != self._thread:
            raise RuntimeError(
                f"{quem} esta a usar um Estado aberto noutra thread. Abre o teu "
                f"proprio Estado(BD) dentro da thread — o WAL trata da "
                f"concorrencia entre duas ligacoes.")

    def fechar(self) -> None:
        try:
            self.bd.close()
        except sqlite3.Error:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.fechar()

    # -- a maquina ----------------------------------------------------------
    def maquina(self) -> dict:
        r = self.bd.execute("SELECT * FROM maquina WHERE id = 1").fetchone()
        return {
            "momento": r["momento"],
            "armado": json.loads(r["armado"]) if r["armado"] else None,
            "posicao": json.loads(r["posicao"]) if r["posicao"] else None,
            "mudou": r["mudou"],
        }

    def gravar_maquina(self, momento: str, *, armado=None, posicao=None) -> None:
        self.bd.execute(
            "UPDATE maquina SET momento = ?, armado = ?, posicao = ?, mudou = ? WHERE id = 1",
            (momento,
             json.dumps(armado, ensure_ascii=False) if armado else None,
             json.dumps(posicao, ensure_ascii=False) if posicao else None,
             time.time()))

    # -- o registo ----------------------------------------------------------
    def gravar_decisao(self, momento: str, dados: dict | None, resposta,
                       consultas=None, feito: str = "", erro: str = "") -> str:
        """Tudo o que o modelo viu, e nao so o que ele respondeu.

        Sem a entrada guardada nao ha maneira de perceber uma decisao ma depois:
        ficava-se a olhar para a conclusao sem as premissas.
        """
        did = novo_id("dec")
        self.bd.execute(
            "INSERT INTO decisoes (id, momento, quando, simbolo, preco, ts_min, "
            "entrada, resposta, consultas, feito, erro) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (did, momento, time.time(),
             (dados or {}).get("simbolo"), (dados or {}).get("preco"),
             (dados or {}).get("ts_min"),
             formatar(dados) if dados else None,
             json.dumps(resposta, ensure_ascii=False, default=str) if resposta is not None else None,
             json.dumps(consultas or [], ensure_ascii=False, default=str),
             feito, erro))
        return did

    def decisoes(self, n: int = 20) -> list[dict]:
        return [dict(r) for r in self.bd.execute(
            "SELECT * FROM decisoes ORDER BY quando DESC LIMIT ?", (n,))]

    def ultima_decisao(self) -> dict | None:
        r = self.bd.execute("SELECT * FROM decisoes ORDER BY quando DESC LIMIT 1").fetchone()
        return dict(r) if r else None

    # -- as posicoes --------------------------------------------------------
    def abrir_posicao(self, dados: dict) -> str:
        pid = novo_id("pos")
        self.bd.execute(
            "INSERT INTO posicoes (id, position_id, simbolo, lado, volume, "
            "preco_entrada, stop, take, risco_pts, armado_em, aberta_em) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (pid, dados.get("position_id"), dados.get("simbolo"), dados.get("lado"),
             dados.get("volume"), dados.get("preco_entrada"), dados.get("stop"),
             dados.get("take"), dados.get("risco_pts"), dados.get("armado_em"),
             time.time()))
        return pid

    def mexer_posicao(self, pid: str, stop: float, take: float) -> None:
        self.bd.execute("UPDATE posicoes SET stop = ?, take = ? WHERE id = ?",
                        (stop, take, pid))

    def fechar_posicao(self, pid: str, preco_saida, motivo: str) -> None:
        r = self.bd.execute("SELECT * FROM posicoes WHERE id = ?", (pid,)).fetchone()
        r_realizado = None
        if r and preco_saida is not None and r["risco_pts"]:
            sinal = 1 if r["lado"] == "compra" else -1
            r_realizado = sinal * (float(preco_saida) - float(r["preco_entrada"])) / float(r["risco_pts"])
        self.bd.execute(
            "UPDATE posicoes SET fechada_em = ?, preco_saida = ?, r_realizado = ?, "
            "motivo = ? WHERE id = ?",
            (time.time(), preco_saida, r_realizado, motivo, pid))

    def posicoes(self, n: int = 20) -> list[dict]:
        return [dict(r) for r in self.bd.execute(
            "SELECT * FROM posicoes ORDER BY aberta_em DESC LIMIT ?", (n,))]

    def placar(self) -> dict:
        """Contagens brutas. Quem se recusa a dar percentagem e quem formata."""
        linhas = [dict(r) for r in self.bd.execute(
            "SELECT r_realizado FROM posicoes WHERE fechada_em IS NOT NULL "
            "AND r_realizado IS NOT NULL")]
        erres = [float(x["r_realizado"]) for x in linhas]
        return {
            "fechadas": len(erres),
            "ganhas": sum(1 for r in erres if r > 0),
            "perdidas": sum(1 for r in erres if r < 0),
            "r_total": sum(erres),
            "r_medio": (sum(erres) / len(erres)) if erres else None,
        }


# ===========================================================================
#  OS QUATRO MOMENTOS
#
#      OBSERVA  -> arma um nivel, ou espera
#      VIGIA    -> so codigo: o preco tocou? pela maxima/minima de M1
#      TOCOU    -> acorda o modelo com o mercado DESSE instante
#      DENTRO   -> aguenta, mexe, ou sai
#
#  Nenhuma destas funcoes decide nada sobre mercado. Elas montam a pergunta,
#  verificam a resposta, e executam-na.
# ===========================================================================

class AvisoConsola:
    """O mensageiro de omissao. Sem Telegram, o agente nao para: escreve aqui."""

    def enviar(self, texto: str) -> None:
        print(texto, flush=True)


class Agente:
    def __init__(self, estado: Estado, broker: CTrader, llm, aviso=None,
                 modelo: str = MODELO):
        self.estado, self.broker, self.llm = estado, broker, llm
        self.aviso = aviso or AvisoConsola()
        self.modelo = modelo
        self.ultimo_observa = 0.0
        self.ultimo_dentro = 0.0
        self.ultima_sincronia = 0.0
        self.saldo = 0.0

    # -- montar a pergunta --------------------------------------------------
    def olhar(self) -> tuple[dict, list]:
        """A fotografia completa, e as M1 de onde ela saiu."""
        self.broker.garantir_ligado()
        m1 = self.broker.m1()
        dados = fotografia(m1, agora_utc_min(), simbolo=self.broker.simbolo_nome,
                           detalhes=self.broker.detalhes)
        return dados, m1

    def _perguntar(self, *, papel: str, sistema: str, formato: str,
                   dados: dict, m1: list, validar: Callable, extra: str = ""):
        ferramentas = Ferramentas(m1)
        prompt = formatar(dados) + (f"\n\n{extra}" if extra else "")
        resposta = correr_agente_com_ferramentas(
            self.llm, papel=papel, sistema=sistema, prompt=prompt,
            validar=validar, ferramentas=ferramentas, formato=formato,
            modelo=self.modelo)
        return resposta, ferramentas.consultadas

    # -- sincronizar com quem sabe -----------------------------------------
    def sincronizar(self) -> None:
        """O broker manda sobre a memoria deste processo. Sempre.

        Um restart, uma ordem tua a mao, um stop que bateu enquanto isto estava
        a dormir: sao todos maneiras de a memoria ficar a mentir. Perguntar
        custa um pedido; acreditar custa uma posicao duplicada.
        """
        self.broker.garantir_ligado()
        abertas = self.broker.posicoes()
        m = self.estado.maquina()

        if m["momento"] == "dentro" and not abertas:
            posicao = m["posicao"] or {}
            self.estado.fechar_posicao(posicao.get("id", ""), None,
                                       "fechada fora do agente (stop, take, ou a mao)")
            self.estado.gravar_maquina("observa")
            self.aviso.enviar(f"{carimbo()} 📕 A posicao fechou-se sem ser por mim "
                              f"(stop, take, ou a mao). Volto a observar.")
            return

        if m["momento"] != "dentro" and abertas:
            p = abertas[0]
            pid = self.estado.abrir_posicao({
                "position_id": p["positionId"], "simbolo": self.broker.simbolo_nome,
                "lado": p["lado"], "volume": p["volume"], "preco_entrada": p["preco"],
                "stop": p["stop"], "take": p["take"],
                "risco_pts": abs(p["preco"] - float(p["stop"])) if p["stop"] else None,
            })
            self.estado.gravar_maquina("dentro", posicao={
                "id": pid, "position_id": p["positionId"], "lado": p["lado"],
                "volume": p["volume"], "preco_entrada": p["preco"],
                "stop": p["stop"], "take": p["take"],
                "risco_pts": abs(p["preco"] - float(p["stop"])) if p["stop"] else None,
            })
            self.aviso.enviar(f"{carimbo()} 📗 Encontrei uma posicao aberta que nao era minha "
                              f"({p['lado']}, {p['volume']}). Passo a geri-la.")

    # -- 1. OBSERVA ---------------------------------------------------------
    def momento_observa(self) -> None:
        dados, m1 = self.olhar()
        permitidos = numeros_dos_dados(dados)
        try:
            resposta, consultas = self._perguntar(
                papel="observa", sistema=SISTEMA_OBSERVA, formato=FORMATO_OBSERVA,
                dados=dados, m1=m1,
                validar=lambda d: validar_observa(d, dados["regua"], permitidos))
        except ErroAgente as e:
            self.estado.gravar_decisao("observa", dados, None, erro=str(e))
            self.aviso.enviar(f"{carimbo()} ⚠️ Nao consegui uma leitura: {e}\n"
                              f"Fico de fora — nao decidir e ficar de fora, nunca entrar.")
            return

        if resposta["acao"] == "esperar":
            self.estado.gravar_decisao("observa", dados, resposta, consultas,
                                       feito="fiquei de fora")
            self.aviso.enviar(f"{carimbo()} 👁 *Espero* — {resposta['porque']}")
            return

        armado = dict(resposta)
        armado["armado_em"] = time.time()
        armado["armado_ts"] = dados["ts_min"]
        armado["expira_ts"] = dados["ts_min"] + resposta["validade_min"]
        armado["preco_ao_armar"] = dados["preco"]
        self.estado.gravar_maquina("armado", armado=armado)
        self.estado.gravar_decisao("observa", dados, resposta, consultas,
                                   feito=f"armei {armado['nivel']} em {armado['gatilho']:.5g}")
        self.aviso.enviar(
            f"{carimbo()} 🎯 *Armado* {armado['lado']} em {armado['gatilho']:.5g}\n"
            f"nivel: {armado['nivel']} {armado['afastamento_atr']:+.2f} ATR · "
            f"toque por {armado['toque']} · vale {armado['validade_min']} min\n"
            f"{armado['porque']}\n"
            f"_Invalida-se se_ {armado['invalidacao']}")

    # -- 2. VIGIA (so codigo) ----------------------------------------------
    def vigiar(self) -> bool:
        """O preco tocou? Pela MAXIMA e pela MINIMA, nunca pelo fecho.

        Um toque e um toque no instante em que acontece. Esperar pelo fecho da
        vela e chegar tarde a metade deles — e as que mais interessam sao
        precisamente as que tocam e voltam dentro do mesmo minuto.
        """
        m = self.estado.maquina()
        armado = m.get("armado")
        if m["momento"] != "armado" or not armado:
            return False

        self.broker.garantir_ligado()
        velas = self.broker.m1_recentes(30)
        # ESTRITAMENTE depois: a vela que ja estava a formar-se quando ele armou
        # nao serve de prova. Se essa vela ja tinha tocado o gatilho antes da
        # decisao, contar com ela era fazer passar um toque do passado por um
        # toque futuro — e entrar por causa de uma coisa que ja tinha acontecido.
        novas = [v for v in velas if int(v[T]) > int(armado["armado_ts"])]
        if not novas:
            return False

        agora_ts = int(velas[-1][T])
        if agora_ts >= int(armado["expira_ts"]):
            self.estado.gravar_maquina("observa")
            self.aviso.enviar(f"{carimbo()} ⌛ O nivel armado expirou sem ser tocado. "
                              f"Volto a observar.")
            return False

        gatilho = float(armado["gatilho"])
        if armado["toque"] == "abaixo":
            tocou = any(v[L] <= gatilho for v in novas)
        else:
            tocou = any(v[H] >= gatilho for v in novas)
        return bool(tocou)

    # -- 3. TOCOU -----------------------------------------------------------
    def momento_tocou(self) -> None:
        m = self.estado.maquina()
        armado = m.get("armado") or {}
        lado = armado.get("lado", "compra")

        dados, m1 = self.olhar()
        permitidos = numeros_dos_dados(dados)
        extra = (f"Tinhas armado {armado.get('nivel')} em "
                 f"{float(armado.get('gatilho', 0)):.5g} para uma {lado}, porque: "
                 f"{armado.get('porque', '')}\n"
                 f"O preco tocou la. Isto e o mercado agora.")
        try:
            resposta, consultas = self._perguntar(
                papel="tocou", sistema=SISTEMA_TOCOU, formato=FORMATO_TOCOU,
                dados=dados, m1=m1, extra=extra,
                validar=lambda d: validar_tocou(d, dados["regua"], permitidos, lado))
        except ErroAgente as e:
            self.estado.gravar_decisao("tocou", dados, None, erro=str(e))
            self.estado.gravar_maquina("observa")
            self.aviso.enviar(f"{carimbo()} ⚠️ Tocou, mas nao consegui uma decisao: {e}\n"
                              f"Nao entro. Volto a observar.")
            return

        if resposta["acao"] == "desistir":
            self.estado.gravar_maquina("observa")
            self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                       feito="desisti")
            self.aviso.enviar(f"{carimbo()} 🚫 *Tocou e desisti* — {resposta['porque']}")
            return

        self._entrar(dados, armado, resposta, consultas, lado)

    def _entrar(self, dados: dict, armado: dict, resposta: dict,
                consultas: list, lado: str) -> None:
        """A cerca, o dimensionamento, e a ordem. Por esta ordem."""
        abertas = self.broker.posicoes()
        if len(abertas) >= MAX_POSICOES_ABERTAS:
            self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                       feito="recusei: ja havia posicao aberta")
            self.aviso.enviar(f"{carimbo()} 🚧 Ele quis entrar, mas ja ha "
                              f"{len(abertas)} posicao aberta e o limite e "
                              f"{MAX_POSICOES_ABERTAS}. Nao entro.")
            self.sincronizar()
            return

        try:
            conta = self.broker.conta()
            self.saldo = conta["saldo"]
            volume, nota = calcular_volume(self.saldo, resposta["risco_pts"],
                                           self.broker.detalhes)
        except (ValueError, ErroBroker) as e:
            self.estado.gravar_maquina("observa")
            self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                       erro=f"nao consegui dimensionar: {e}")
            self.aviso.enviar(f"{carimbo()} ⚠️ Nao consegui dimensionar a ordem: {e}")
            return

        marco = time.time()
        try:
            self.broker.nova_ordem(lado=lado, volume=volume, stop=resposta["stop"],
                                   take=resposta["take"], etiqueta="agente")
        except ErroBroker as e:
            self.estado.gravar_maquina("observa")
            self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                       erro=f"a ordem falhou: {e}")
            self.aviso.enviar(f"{carimbo()} ⚠️ A ordem falhou: {e}")
            return

        aberta = self._confirmar_abertura(marco)
        if aberta is None:
            self.estado.gravar_maquina("observa")
            self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                       erro="mandei a ordem e nao apareceu posicao nenhuma")
            self.aviso.enviar(
                f"{carimbo()} ⚠️ Mandei a ordem e ao fim de alguns segundos nao havia "
                f"posicao nenhuma. Pode ter sido recusada. Vai ver a conta — eu volto "
                f"a observar, e nao mando outra.")
            return

        pid = self.estado.abrir_posicao({
            "position_id": aberta["positionId"], "simbolo": self.broker.simbolo_nome,
            "lado": lado, "volume": aberta["volume"],
            "preco_entrada": aberta["preco"], "stop": resposta["stop"],
            "take": resposta["take"], "risco_pts": resposta["risco_pts"],
            "armado_em": armado.get("armado_em"),
        })
        posicao = {
            "id": pid, "position_id": aberta["positionId"], "lado": lado,
            "volume": aberta["volume"], "preco_entrada": aberta["preco"],
            "stop": resposta["stop"], "take": resposta["take"],
            "risco_pts": resposta["risco_pts"],
        }
        self.estado.gravar_maquina("dentro", posicao=posicao)
        self.estado.gravar_decisao("tocou", dados, resposta, consultas,
                                   feito=f"entrei {lado} volume {aberta['volume']}")
        self.ultimo_dentro = time.time()
        self.aviso.enviar(
            f"{carimbo()} ✅ *Entrei* {lado} a {aberta['preco']:.5g}\n"
            f"stop {resposta['stop']:.5g} ({resposta['stop_nivel']}) · "
            f"take {resposta['take']:.5g} ({resposta['take_nivel']}) · "
            f"R {resposta['R']:.2f}\n"
            f"volume {nota}\n"
            f"{resposta['porque']}")

    def _confirmar_abertura(self, desde: float, tentativas: int = 6) -> dict | None:
        """Pergunta ao broker se a posicao existe, em vez de assumir que sim.

        A confirmacao de uma ordem nao vem como resposta ao pedido — vem como
        evento, que pode chegar antes ou depois. O reconcile e a unica coisa
        que responde a "existe?" sem depender do timing.
        """
        for _ in range(tentativas):
            time.sleep(1.0)
            try:
                abertas = self.broker.posicoes()
            except ErroBroker:
                continue
            if abertas:
                return abertas[0]
        return None

    # -- 4. DENTRO ----------------------------------------------------------
    def momento_dentro(self) -> None:
        m = self.estado.maquina()
        posicao = m.get("posicao") or {}
        lado = posicao.get("lado", "compra")

        dados, m1 = self.olhar()
        permitidos = numeros_dos_dados(dados)
        risco = posicao.get("risco_pts") or 0
        sinal = 1 if lado == "compra" else -1
        em_r = ((sinal * (dados["preco"] - float(posicao.get("preco_entrada", 0))) / risco)
                if risco else None)
        extra = (f"POSICAO ABERTA: {lado}, volume {posicao.get('volume')}, "
                 f"entrada {float(posicao.get('preco_entrada', 0)):.5g}, "
                 f"stop {posicao.get('stop')}, take {posicao.get('take')}.")
        if em_r is not None:
            extra += f"\nEsta a {em_r:+.2f} R."

        try:
            resposta, consultas = self._perguntar(
                papel="dentro", sistema=SISTEMA_DENTRO, formato=FORMATO_DENTRO,
                dados=dados, m1=m1, extra=extra,
                validar=lambda d: validar_dentro(d, dados["regua"], permitidos, lado))
        except ErroAgente as e:
            self.estado.gravar_decisao("dentro", dados, None, erro=str(e))
            self.aviso.enviar(f"{carimbo()} ⚠️ Nao consegui uma decisao sobre a posicao: "
                              f"{e}\nDeixo-a como esta — o stop e o take ja estao no broker.")
            return

        if resposta["acao"] == "aguentar":
            self.estado.gravar_decisao("dentro", dados, resposta, consultas,
                                       feito="aguentei")
            self.aviso.enviar(f"{carimbo()} ⏳ *Aguento* — {resposta['porque']}")
            return

        if resposta["acao"] == "sair":
            self._sair(dados, posicao, resposta, consultas)
            return

        self._mexer(dados, posicao, resposta, consultas, lado)

    def _mexer(self, dados: dict, posicao: dict, resposta: dict,
               consultas: list, lado: str) -> None:
        if STOP_SO_A_FAVOR and not stop_a_favor(lado, posicao.get("stop"), resposta["stop"]):
            self.estado.gravar_decisao(
                "dentro", dados, resposta, consultas,
                feito="recusei: o stop novo afastava-se")
            self.aviso.enviar(
                f"{carimbo()} 🚧 Ele quis por o stop em {resposta['stop']:.5g}, que fica "
                f"mais longe do que o que ja la esta ({posicao.get('stop')}). Recusei: "
                f"a posicao foi dimensionada para o stop original.")
            return
        try:
            self.broker.mexer_sltp(posicao["position_id"], resposta["stop"], resposta["take"])
        except ErroBroker as e:
            self.estado.gravar_decisao("dentro", dados, resposta, consultas,
                                       erro=f"nao consegui mexer: {e}")
            self.aviso.enviar(f"{carimbo()} ⚠️ Nao consegui mexer no stop/take: {e}")
            return
        posicao["stop"], posicao["take"] = resposta["stop"], resposta["take"]
        self.estado.mexer_posicao(posicao["id"], resposta["stop"], resposta["take"])
        self.estado.gravar_maquina("dentro", posicao=posicao)
        self.estado.gravar_decisao("dentro", dados, resposta, consultas, feito="mexi")
        self.aviso.enviar(
            f"{carimbo()} 🔧 *Mexi* — stop {resposta['stop']:.5g} "
            f"({resposta['stop_nivel']}), take {resposta['take']:.5g} "
            f"({resposta['take_nivel']})\n{resposta['porque']}")

    def _sair(self, dados: dict, posicao: dict, resposta: dict, consultas: list) -> None:
        try:
            self.broker.fechar_posicao(posicao["position_id"], posicao["volume"])
        except ErroBroker as e:
            self.estado.gravar_decisao("dentro", dados, resposta, consultas,
                                       erro=f"nao consegui fechar: {e}")
            self.aviso.enviar(f"{carimbo()} ⚠️ Nao consegui fechar a posicao: {e}")
            return
        time.sleep(2.0)
        self.estado.fechar_posicao(posicao["id"], dados["preco"], resposta["porque"])
        self.estado.gravar_maquina("observa")
        self.estado.gravar_decisao("dentro", dados, resposta, consultas, feito="sai")
        self.aviso.enviar(f"{carimbo()} 🏁 *Sai* a {dados['preco']:.5g} — {resposta['porque']}")

    # -- o passo e o ciclo --------------------------------------------------
    def passo(self) -> None:
        self.estado.exigir_mesma_thread("o agente")
        agora = time.time()
        # Perguntar ao broker o que existe e certo; perguntar-lhe tres vezes por
        # minuto para sempre nao e. Um stop que bateu ha 40 segundos e novidade
        # suficientemente fresca, e quem vai mandar uma ordem volta a perguntar
        # antes de a mandar.
        if agora - self.ultima_sincronia >= SEGUNDOS_SINCRONIA:
            self.ultima_sincronia = agora
            self.sincronizar()
        m = self.estado.maquina()

        if m["momento"] == "armado":
            if self.vigiar():
                self.momento_tocou()
            return
        if m["momento"] == "dentro":
            if agora - self.ultimo_dentro >= MINUTOS_DENTRO * 60:
                self.ultimo_dentro = agora
                self.momento_dentro()
            return
        if agora - self.ultimo_observa >= MINUTOS_OBSERVA * 60:
            self.ultimo_observa = agora
            self.momento_observa()

    def correr(self, parar: threading.Event) -> None:
        while not parar.is_set():
            try:
                self.passo()
            except ErroBroker as e:
                log.warning("o broker falhou: %s", e)
            except Exception:
                # O ciclo nunca morre por causa de uma volta ma. Um agente que
                # para em silencio com uma posicao aberta e pior do que um que
                # se engana e diz.
                log.exception("falhei uma volta")
            parar.wait(SEGUNDOS_VIGIA)


# ===========================================================================
#  TELEGRAM
#
#  So leitura. O agente decide sozinho; isto e onde ele te conta o que fez e
#  onde tu vais ver porque. Se o token nao existir, ele corre na mesma e
#  escreve no terminal — nao para por falta de mensageiro.
# ===========================================================================

AJUDA = """*Agente* — o modelo decide, o codigo mede e executa.

/estado     em que momento esta, e com o que
/porque     a ultima decisao por extenso, com o que ele foi ler
/foto       a fotografia do mercado agora
/historico  as ultimas decisoes
/placar     as posicoes fechadas, em R
/ajuda      isto"""


def token_telegram() -> str:
    return (TELEGRAM_TOKEN or os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


class Telegram:
    def __init__(self, tok: str, timeout: int = 40):
        if ":" not in tok:
            raise ValueError("o TELEGRAM_BOT_TOKEN nao parece um token "
                             "(falta o ':'). Pede um novo ao @BotFather.")
        self.base, self.timeout = f"https://api.telegram.org/bot{tok}", timeout

    def _chamar(self, metodo: str, **carga):
        try:
            r = requests.post(f"{self.base}/{metodo}", json=carga,
                              timeout=carga.get("timeout", self.timeout) + 15)
        except requests.exceptions.RequestException as e:
            raise RuntimeError(f"nao consegui falar com o Telegram: {e}") from e
        try:
            corpo = r.json()
        except ValueError:
            raise RuntimeError(f"o Telegram devolveu {r.status_code}") from None
        if not corpo.get("ok"):
            raise RuntimeError(f"o Telegram recusou {metodo}: {corpo.get('description')}")
        return corpo.get("result")

    def enviar(self, chat, texto: str, markdown: bool = True):
        carga = {"chat_id": chat, "text": texto[:4096],
                 "disable_web_page_preview": True}
        if markdown:
            carga["parse_mode"] = "Markdown"
        try:
            return self._chamar("sendMessage", **carga)
        except RuntimeError:
            # Perde-se o negrito; nao se perde a mensagem. Um asterisco a mais
            # numa razao do modelo nao pode calar um aviso de posicao aberta.
            carga.pop("parse_mode", None)
            return self._chamar("sendMessage", **carga)

    def atualizacoes(self, offset: int, timeout: int):
        return self._chamar("getUpdates", offset=offset, timeout=timeout,
                            allowed_updates=["message"])


class AvisoTelegram:
    """O mensageiro do agente. Engole as falhas: o agente nao para por elas."""

    def __init__(self, tg: Telegram, chat: int):
        self.tg, self.chat, self._lock = tg, chat, threading.Lock()

    def enviar(self, texto: str) -> None:
        print(texto, flush=True)
        with self._lock:
            try:
                self.tg.enviar(self.chat, texto)
            except RuntimeError as e:
                log.error("nao consegui avisar pelo Telegram: %s", e)


def formatar_placar(p: dict) -> str:
    """O n antes da taxa. Sempre."""
    if not p["fechadas"]:
        return ("Ainda nao fechei nenhuma posicao. Quando fechar, cada uma fica "
                "aqui com o R que deu.")
    linhas = [f"*{p['fechadas']}* posicoes fechadas · "
              f"{p['ganhas']} ganhas, {p['perdidas']} perdidas",
              f"R total {p['r_total']:+.2f} · R medio {p['r_medio']:+.2f}"]
    if p["fechadas"] < 20:
        linhas.append(f"\nNao te dou percentagem com {p['fechadas']}: faltam "
                      f"{20 - p['fechadas']}. Ate la e ruido com casas decimais.")
    return "\n".join(linhas)


def formatar_estado(m: dict) -> str:
    momento = m["momento"]
    desde = time.time() - (m["mudou"] or time.time())
    linhas = [f"{carimbo()} momento: *{momento}* (ha {desde / 60:.0f} min)"]
    if momento == "armado" and m["armado"]:
        a = m["armado"]
        linhas += [
            f"armado: {a['lado']} em {a['gatilho']:.5g}",
            f"nivel {a['nivel']} {a['afastamento_atr']:+.2f} ATR · toque por {a['toque']}",
            f"expira as {datetime.fromtimestamp(a['expira_ts'] * 60, timezone.utc).strftime('%H:%M')} UTC",
            f"_{a['porque']}_",
        ]
    elif momento == "dentro" and m["posicao"]:
        p = m["posicao"]
        linhas += [
            f"posicao: {p['lado']} volume {p['volume']} a {float(p['preco_entrada']):.5g}",
            f"stop {p.get('stop')} · take {p.get('take')}",
        ]
    else:
        linhas.append("nada armado, nada aberto — a olhar.")
    return "\n".join(linhas)


def formatar_porque(d: dict | None) -> str:
    if not d:
        return "Ainda nao decidi nada."
    quando = datetime.fromtimestamp(d["quando"], timezone.utc).strftime("%Y-%m-%d %H:%M")
    linhas = [f"{carimbo()} *{d['momento']}* · {quando} UTC · preco {d['preco']}"]
    if d.get("erro"):
        linhas.append(f"⚠️ {d['erro']}")
    if d.get("resposta"):
        try:
            r = json.loads(d["resposta"])
            linhas.append(f"acao: *{r.get('acao')}*")
            if r.get("porque"):
                linhas.append(r["porque"])
            if r.get("invalidacao"):
                linhas.append(f"_Invalida-se se_ {r['invalidacao']}")
        except (ValueError, TypeError):
            linhas.append(str(d["resposta"])[:1000])
    if d.get("feito"):
        linhas.append(f"fiz: {d['feito']}")
    try:
        consultas = json.loads(d.get("consultas") or "[]")
    except (ValueError, TypeError):
        consultas = []
    if consultas:
        linhas.append("\n*Foi ler*")
        for c in consultas:
            linhas.append(f"· {c.get('tipo')}: {c.get('o_que')}")
            for a in (c.get("achados") or [])[:3]:
                linhas.append(f"    {a.get('url')}")
    return "\n".join(linhas)


class Bot:
    """So le. Nenhum comando aqui manda o agente fazer nada."""

    def __init__(self, estado: Estado, agente: Agente, tg: Telegram,
                 parar: threading.Event | None = None):
        self.estado, self.agente, self.tg = estado, agente, tg
        self.parar = parar or threading.Event()

    def _resp(self, chat, texto: str) -> None:
        try:
            self.tg.enviar(chat, texto)
        except RuntimeError as e:
            log.error("nao consegui responder: %s", e)

    def _autorizado(self, chat) -> bool:
        if int(chat) == int(CHAT_ID):
            return True
        log.warning("chat nao autorizado: %s", chat)
        return False

    def _texto(self, chat, texto: str) -> None:
        if not texto.startswith("/"):
            return self._resp(chat, "So percebo comandos. Manda /ajuda.")
        cmd = texto.partition(" ")[0].lstrip("/").split("@")[0].lower()

        if cmd in ("start", "ajuda", "help"):
            self._resp(chat, AJUDA)
        elif cmd == "estado":
            self._resp(chat, formatar_estado(self.estado.maquina()))
        elif cmd == "porque":
            self._resp(chat, formatar_porque(self.estado.ultima_decisao()))
        elif cmd == "foto":
            if self.agente is None:
                return self._resp(chat, "Ainda estou a arrancar. Tenta daqui a pouco.")
            self._resp(chat, "⏳ A ler o mercado...")
            try:
                dados, _ = self.agente.olhar()
            except (ErroBroker, ValueError) as e:
                return self._resp(chat, f"⚠️ {e}")
            self._resp(chat, formatar(dados))
        elif cmd == "historico":
            linhas = []
            for d in self.estado.decisoes(12):
                quando = datetime.fromtimestamp(d["quando"], timezone.utc).strftime("%m-%d %H:%M")
                linhas.append(f"{quando} · {d['momento']} · {d.get('feito') or d.get('erro') or '-'}")
            self._resp(chat, "\n".join(linhas) or "Ainda nao ha historico.")
        elif cmd == "placar":
            self._resp(chat, formatar_placar(self.estado.placar()))
        else:
            self._resp(chat, f"Nao conheco /{cmd}. Manda /ajuda.")

    def correr(self) -> None:
        offset = 0
        while not self.parar.is_set():
            try:
                updates = self.tg.atualizacoes(offset, 30)
            except RuntimeError as e:
                log.warning("o Telegram falhou: %s", e)
                self.parar.wait(5)
                continue
            for u in updates or []:
                offset = u["update_id"] + 1
                mensagem = u.get("message") or {}
                chat = (mensagem.get("chat") or {}).get("id")
                if chat is None or not self._autorizado(chat):
                    continue
                try:
                    self._texto(chat, str(mensagem.get("text") or "").strip())
                except Exception:
                    log.exception("falhei a tratar um comando")


# ===========================================================================
#  O AUTOTESTE
#
#  Nada disto precisa da conta ligada, e e de proposito: um teste que so corre
#  contra o broker a serio nao se corre, e um teste que nao se corre nao e um
#  teste.
#
#  A peca que faz o resto ser testavel e o BrokerFalso: fala o mesmo protocolo
#  num socket local (prefixo de 4 bytes + JSON), por isso o cliente que corre
#  aqui e literalmente o mesmo que corre contra o cTrader.
# ===========================================================================

def velas_falsas(n: int, *, base: float = 3400.0, semente: int = 7,
                 fim_min: int | None = None) -> list[tuple]:
    """Um passeio deterministico de M1. Nao e mercado: e aritmetica com forma."""
    fim = fim_min if fim_min is not None else agora_utc_min()
    inicio = fim - n
    x, fora = base, []
    for i in range(n):
        # Deterministico e sem `random`: o mesmo teste tem de dar o mesmo
        # numero daqui a um ano, senao nao serve para apanhar regressoes.
        semente = (semente * 1103515245 + 12345) % (2 ** 31)
        passo = ((semente % 2000) - 1000) / 1000.0
        onda = math.sin(i / 180.0) * 3.0
        x = max(1.0, x + passo * 0.6 + onda * 0.02)
        alto = x + abs(passo) * 0.8 + 0.2
        baixo = x - abs(passo) * 0.8 - 0.2
        abertura = fora[-1][C] if fora else x
        volume = 50 + (semente % 200)
        fora.append((inicio + i, abertura, max(alto, abertura, x),
                     min(baixo, abertura, x), x, float(volume)))
    return fora


def _para_trendbar(v: tuple) -> dict:
    """O caminho inverso do `trendbar_para_vela`, para o broker falso."""
    baixo = int(round(v[L] * 1e5))
    return {
        "utcTimestampInMinutes": int(v[T]),
        "low": baixo,
        "deltaOpen": int(round(v[O] * 1e5)) - baixo,
        "deltaHigh": int(round(v[H] * 1e5)) - baixo,
        "deltaClose": int(round(v[C] * 1e5)) - baixo,
        "volume": int(v[V]),
    }


class BrokerFalso(threading.Thread):
    """Um cTrader de mentira. Mesmo enquadramento, mesmos payloadType."""

    def __init__(self, velas=None, *, conta: int = 111, saldo: float = 10_000.0):
        super().__init__(name="broker-falso", daemon=True)
        self.velas = velas if velas is not None else velas_falsas(45 * 1440)
        self.conta, self.saldo = conta, saldo
        self.posicoes: list[dict] = []
        self.ordens: list[dict] = []
        self.amendas: list[dict] = []
        self.fechos: list[dict] = []
        self.autenticado = {"app": False, "conta": False}
        self.proximo_id = 5001
        self.servidor = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.servidor.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.servidor.bind(("127.0.0.1", 0))
        self.servidor.listen(1)
        self.porta = self.servidor.getsockname()[1]
        self._parar = threading.Event()
        self._cliente = None

    def parar(self) -> None:
        self._parar.set()
        for s in (self._cliente, self.servidor):
            try:
                if s:
                    s.close()
            except OSError:
                pass

    def run(self) -> None:
        try:
            self._cliente, _ = self.servidor.accept()
        except OSError:
            return
        while not self._parar.is_set():
            try:
                (tamanho,) = struct.unpack(">I", _ler_exato(self._cliente, 4))
                pedido = json.loads(_ler_exato(self._cliente, tamanho).decode("utf-8"))
            except Exception:
                return
            try:
                self._tratar(pedido)
            except Exception:
                log.exception("o broker falso rebentou")

    def _enviar(self, tipo: int, carga: dict, cid: str = "") -> None:
        mensagem = {"payloadType": tipo, "payload": carga}
        if cid:
            mensagem["clientMsgId"] = cid
        bruto = json.dumps(mensagem).encode("utf-8")
        self._cliente.sendall(struct.pack(">I", len(bruto)) + bruto)

    def _tratar(self, pedido: dict) -> None:
        tipo = int(pedido.get("payloadType") or 0)
        carga = pedido.get("payload") or {}
        cid = pedido.get("clientMsgId") or ""

        if tipo == PT.HEARTBEAT:
            return
        if tipo == PT.APP_AUTH_REQ:
            self.autenticado["app"] = True
            return self._enviar(PT.APP_AUTH_RES, {}, cid)
        if tipo == PT.ACCOUNTS_BY_TOKEN_REQ:
            return self._enviar(PT.ACCOUNTS_BY_TOKEN_RES,
                                {"ctidTraderAccount": [{"ctidTraderAccountId": self.conta}]}, cid)
        if tipo == PT.ACCOUNT_AUTH_REQ:
            if not self.autenticado["app"]:
                return self._enviar(PT.ERROR_RES,
                                    {"errorCode": "NOT_AUTHENTICATED",
                                     "description": "autentica a aplicacao primeiro"}, cid)
            self.autenticado["conta"] = True
            return self._enviar(PT.ACCOUNT_AUTH_RES, {"ctidTraderAccountId": self.conta}, cid)
        if tipo == PT.SYMBOLS_LIST_REQ:
            return self._enviar(PT.SYMBOLS_LIST_RES, {"symbol": [
                {"symbolId": 42, "symbolName": "ETHUSD", "digits": 2},
                {"symbolId": 43, "symbolName": "BTCUSD", "digits": 2}]}, cid)
        if tipo == PT.SYMBOL_BY_ID_REQ:
            return self._enviar(PT.SYMBOL_BY_ID_RES, {"symbol": [
                {"symbolId": 42, "digits": 2, "pipPosition": 2, "lotSize": 10_000_000,
                 "minVolume": 1_000, "maxVolume": 100_000_000, "stepVolume": 1_000}]}, cid)
        if tipo == PT.TRADER_REQ:
            return self._enviar(PT.TRADER_RES, {"trader": {
                "ctidTraderAccountId": self.conta, "balance": int(self.saldo * 100),
                "moneyDigits": 2}}, cid)
        if tipo == PT.GET_TRENDBARS_REQ:
            de = int(carga.get("fromTimestamp") or 0) // 60000
            ate = int(carga.get("toTimestamp") or 0) // 60000
            dentro = [v for v in self.velas if de <= v[T] <= ate]
            return self._enviar(PT.GET_TRENDBARS_RES,
                                {"period": PERIODO_M1, "symbolId": 42,
                                 "trendbar": [_para_trendbar(v) for v in dentro]}, cid)
        if tipo == PT.RECONCILE_REQ:
            return self._enviar(PT.RECONCILE_RES, {"position": [
                {"positionId": p["positionId"], "price": p["preco"],
                 "stopLoss": p.get("stop"), "takeProfit": p.get("take"),
                 "tradeData": {"symbolId": 42, "volume": p["volume"],
                               "tradeSide": 1 if p["lado"] == "compra" else 2}}
                for p in self.posicoes]}, cid)
        if tipo == PT.NEW_ORDER_REQ:
            self.ordens.append(carga)
            self.posicoes.append({
                "positionId": self.proximo_id,
                "lado": "compra" if int(carga.get("tradeSide") or 1) == 1 else "venda",
                "volume": int(carga.get("volume") or 0),
                "preco": self.velas[-1][C],
                "stop": carga.get("stopLoss"), "take": carga.get("takeProfit")})
            self.proximo_id += 1
            # A confirmacao vem como evento, sem clientMsgId — como no real.
            return self._enviar(PT.EXECUTION_EVENT, {"executionType": 2}, "")
        if tipo == PT.AMEND_POSITION_SLTP_REQ:
            self.amendas.append(carga)
            for p in self.posicoes:
                if p["positionId"] == int(carga.get("positionId") or 0):
                    p["stop"], p["take"] = carga.get("stopLoss"), carga.get("takeProfit")
            return
        if tipo == PT.CLOSE_POSITION_REQ:
            self.fechos.append(carga)
            alvo = int(carga.get("positionId") or 0)
            self.posicoes = [p for p in self.posicoes if p["positionId"] != alvo]
            return self._enviar(PT.EXECUTION_EVENT, {"executionType": 4}, "")
        self._enviar(PT.ERROR_RES, {"errorCode": "UNKNOWN",
                                    "description": f"nao conheco {tipo}"}, cid)


def broker_de_teste(falso: BrokerFalso) -> CTrader:
    """Um CTrader a falar com o BrokerFalso: sem TLS, sem rede, sem conta."""
    lig = Ligacao("127.0.0.1", falso.porta, tls=False, timeout=10)
    creds = {"cliente": "id", "segredo": "segredo", "token": "tok", "conta": falso.conta}
    broker = CTrader("ETHUSD", ligacao=lig, creds=creds)
    broker.ligar()
    broker.resolver_simbolo()
    return broker


def autoteste() -> int:  # noqa: C901 — e uma lista de casos, nao um algoritmo
    """Corre tudo o que da para correr sem broker, sem Ollama e sem Telegram."""
    falhas: list[str] = []

    def verificar(condicao, descricao: str) -> None:
        print(f"  {'✅' if condicao else '❌'} {descricao}")
        if not condicao:
            falhas.append(descricao)

    tmp = Path(os.environ.get("TMPDIR") or "/tmp") / f"agente_teste_{os.getpid()}"
    tmp.mkdir(parents=True, exist_ok=True)

    print("\n=== 1. A barra em formacao e descartada ===")
    # Baldes de 15m: 0-14, 15-29, 30-44. Com agora_min = 38, o balde dos 30
    # ainda esta a meio e o seu maximo e provisorio.
    cruas = [(t, 10.0, 10.0 + (1.0 if t == 31 else 0.0), 9.0, 10.0, 1.0) for t in range(0, 39)]
    quinze = agrupar(cruas, 15)
    so_fechadas = fechadas(quinze, 15, 38)
    verificar(len(quinze) == 3, "agrupar devolve os tres baldes, o incompleto incluido")
    verificar(len(so_fechadas) == 2, "fechadas deixa cair o balde que contem o agora")
    verificar(max(v[H] for v in so_fechadas) == 10.0,
              "o maximo provisorio de 11.0 nao aparece nas velas fechadas")
    verificar(fechadas(quinze, 15, 45) == quinze,
              "passado o balde inteiro, ele ja conta")

    print("\n=== 2. Aritmetica, contra contas a mao ===")
    # 14 TR de 10 e um de 24: (10*13 + 24)/14 = 11.0
    velas_atr = [(i, 100.0, 105.0, 95.0, 100.0, 1.0) for i in range(15)]
    velas_atr.append((15, 100.0, 124.0, 100.0, 100.0, 1.0))   # TR = 124 - 100 = 24
    verificar(abs(atr(velas_atr, 14) - 11.0) < 1e-9, "ATR de Wilder bate a conta a mao")
    verificar(atr(velas_atr[:5], 14) is None, "sem historia, o ATR e None e nao um palpite")

    # Kyle: dP constante = 2 e Vs constante = 10 -> lambda = 2/10 = 0.2
    kyle = [(i, 100.0 + 2 * i, 100.0 + 2 * i, 100.0 + 2 * i, 102.0 + 2 * i, 10.0)
            for i in range(30)]
    verificar(abs(lambda_kyle(kyle, 20) - 0.2) < 1e-9,
              "lambda de Kyle: dP=2 por Vs=10 da 0.2")
    plano = [(i, 100.0, 100.0, 100.0, 100.0, 10.0) for i in range(30)]
    verificar(lambda_kyle(plano, 20) is None,
              "sem volume assinado (todas as velas doji), lambda e None e nao zero")

    # pos%: preco no topo da faixa da vela mais recente
    faixa = [(i, 10.0, 20.0, 0.0, 20.0, 1.0) for i in range(25)]
    p = percurso(faixa, 20, 5.0)
    verificar(abs(p["pos_pct"] - 100.0) < 1e-9, "pos% de 100 quando o preco esta na maxima")
    verificar(abs(p["sentido_atr"]) < 1e-9, "sentido zero quando o fecho nao mudou")
    subida = [(i, 100.0, 100.0, 100.0, 100.0 + i, 1.0) for i in range(25)]
    verificar(abs(percurso(subida, 20, 10.0)["sentido_atr"] - 2.0) < 1e-9,
              "sentido: subir 20 com ATR 10 da +2.0 ATR")

    verificar(percentil(5.0, [1, 2, 3, 4], minimo=100) == (None, 4),
              "percentil recusa a percentagem com pouca amostra, mas diz o n")
    verificar(percentil(5.0, list(range(10)), minimo=5)[0] == 50.0,
              "percentil: 5 de 10 valores ficam abaixo de 5")

    print("\n=== 3. Estrutura: pivos, BOS, CHoCH, FVG ===")
    # Sobe ate 110, corrige, e volta a romper: BOS. Depois cai abaixo do pivo
    # de baixo: CHoCH.
    precos = [100, 101, 102, 105, 103, 102, 104, 108, 106, 105, 107, 112,
              110, 108, 104, 99, 95]
    estrut = []
    for i, c in enumerate(precos):
        estrut.append((i, float(c), float(c) + 0.5, float(c) - 0.5, float(c), 1.0))
    e = estrutura(estrut, k=2)
    tipos = [x["tipo"] for x in e["eventos"]]
    verificar(any(t == "BOS" for t in tipos) or any(t == "CHoCH" for t in tipos),
              "uma serie que rompe extremos produz eventos de estrutura")
    verificar(all(x["tipo"] in ("BOS", "CHoCH") for x in e["eventos"]),
              "todos os eventos sao BOS ou CHoCH, e nada mais")
    verificar("pivo de 2 velas" in e["definicao"],
              "a definicao do pivo vai escrita ao lado do resultado")

    subida_limpa = [(i, float(100 + i), float(100 + i) + 0.5, float(100 + i) - 0.5,
                     float(100 + i), 1.0) for i in range(20)]
    so_bos = estrutura(subida_limpa, k=2)["eventos"]
    verificar(all(x["tipo"] == "BOS" for x in so_bos),
              "numa subida sem correcao nenhuma nao ha CHoCH nenhum")

    # FVG de alta: a minima da terceira acima da maxima da primeira.
    buraco = [(0, 10.0, 11.0, 9.0, 10.0, 1.0),
              (1, 11.0, 15.0, 10.5, 14.0, 1.0),
              (2, 14.0, 16.0, 13.0, 15.0, 1.0),
              (3, 15.0, 16.0, 14.5, 15.5, 1.0)]
    achados = fvgs(buraco)
    verificar(len(achados) == 1 and achados[0]["lado"] == "alta",
              "tres velas com um buraco para cima dao um FVG de alta")
    verificar(abs(achados[0]["de"] - 11.0) < 1e-9 and abs(achados[0]["ate"] - 13.0) < 1e-9,
              "o buraco vai da maxima da primeira a minima da terceira")
    verificar(not achados[0]["preenchido"], "um buraco que ninguem voltou a tocar nao esta preenchido")
    tapado = buraco + [(4, 15.0, 15.0, 10.0, 11.0, 1.0)]
    verificar(fvgs(tapado)[0]["preenchido"],
              "uma vela que desce abaixo do buraco marca-o como preenchido")

    print("\n=== 4. Sessoes, em UTC ===")
    dia = 30 * 1440                       # um dia redondo desde a epoca
    verificar(janela_sessao(dia + 3 * 60, 0, 7) == (dia, dia + 7 * 60),
              "as 03:00, a sessao 0-7h e a de hoje")
    verificar(janela_sessao(dia + 3 * 60, 22, 7) == (dia - 1440 + 22 * 60, dia + 7 * 60),
              "uma sessao que atravessa a meia-noite comeca no dia anterior")
    verificar(janela_sessao(dia + 20 * 60, 0, 7) == (dia, dia + 7 * 60),
              "as 20:00, a sessao 0-7h ja fechou mas continua a ser a de hoje")
    fora_dentro = [(0, 10.0, 12.0, 9.5, 11.0, 1.0)]   # rompe so por cima
    verificar(rompeu(fora_dentro, 11.0, 9.0) == ("acima", True),
              "sair por cima e fechar dentro e uma varrida")
    verificar(rompeu([(0, 10.0, 12.0, 11.5, 12.0, 1.0)], 11.0, 9.0) == ("acima", False),
              "sair por cima e ficar la fora e um rompimento")

    print("\n=== 5. A regua, e o preco que nasce de um nome ===")
    r = regua([("max H1", 110.0), ("min H1", 90.0)], 100.0, 10.0)
    verificar([d["etiqueta"] for d in r["degraus"]] == ["max H1", "min H1"],
              "a regua sai ordenada do preco mais alto para o mais baixo")
    verificar(abs(r["degraus"][0]["atr"] - 1.0) < 1e-9, "10 pontos com ATR 10 e 1 ATR")
    preco, _ = nivel_para_preco(r, "min H1", -0.5)
    verificar(abs(preco - 85.0) < 1e-9, "min H1 a -0.5 ATR da 85")
    verificar(abs(nivel_para_preco(r, "MAX h1", 0)[0] - 110.0) < 1e-9,
              "o nome do nivel nao repara em maiusculas")
    try:
        nivel_para_preco(r, "max D1", 0)
        verificar(False, "um nivel que nao existe tem de ser recusado")
    except ValueError as e:
        verificar("max H1" in str(e) and "min H1" in str(e),
                  "o erro do nivel inexistente lista os que existem")
    try:
        nivel_para_preco(r, "max H1", 99)
        verificar(False, "um afastamento absurdo tem de ser recusado")
    except ValueError as e:
        verificar("fora do alcance" in str(e), "o afastamento absurdo diz que esta fora do alcance")

    print("\n=== 6. O guarda: ele nao inventa numeros ===")
    permitidos = numeros_dos_dados({"a": 3412.5, "b": [1.916279], "c": {"d": 34.3}})
    verificar(numero_inventado("o preco 3.412,50 e o racio 1,92", permitidos) is None,
              "numeros certos escritos a portuguesa passam")
    verificar(numero_inventado("resistencia em 3.500", permitidos) == "3.500",
              "um numero inventado e apanhado, e devolvido como ele o escreveu")
    verificar(numero_inventado("3 velas seguidas", permitidos) is None,
              "inteiros pequenos passam: sao contagens")
    verificar(numero_inventado("subiu 12%", permitidos) == "12",
              "uma percentagem pequena NAO passa: e uma afirmacao sobre os dados")

    print("\n=== 7. Dimensionamento ===")
    detalhes = {"stepVolume": 1_000, "minVolume": 1_000, "maxVolume": 100_000_000}
    # 0.5% de 10000 = 50 de risco; stop de 10 pontos -> 5 unidades -> 500 centesimos,
    # que arredondado ao passo de 1000 da 0 e fica abaixo do minimo.
    try:
        calcular_volume(10_000.0, 10.0, detalhes)
        verificar(False, "um volume abaixo do minimo tem de ser recusado")
    except ValueError as e:
        verificar("minimo" in str(e), "o erro do volume minimo explica-se em vez de arredondar para cima")
    volume, nota = calcular_volume(1_000_000.0, 10.0, detalhes)
    verificar(volume == 50_000,
              "0.5% de 1M = 5000 de risco; a 10 pts sao 500 unidades = 50000 centesimos")
    verificar("moeda de cotacao" in nota,
              "a suposicao da moeda vai escrita, em vez de ficar escondida")
    verificar(volume % 1_000 == 0, "o volume respeita o passo do simbolo")

    print("\n=== 8. O protocolo: enquadramento, autenticacao, ordens ===")
    falso = BrokerFalso()
    falso.start()
    broker = None
    try:
        broker = broker_de_teste(falso)
        verificar(falso.autenticado["app"] and falso.autenticado["conta"],
                  "a autenticacao faz os dois passos: aplicacao e depois conta")
        verificar(broker.simbolo_id == 42, "o nome do simbolo resolveu-se para um symbolId")
        verificar(broker.detalhes["stepVolume"] == 1_000,
                  "o stepVolume vem do broker, e nao de um palpite meu")
        verificar(abs(broker.conta()["saldo"] - 10_000.0) < 1e-6,
                  "o saldo vem em centesimos e sai em unidades")

        velas = broker.m1(2)
        verificar(len(velas) > 1000, "os M1 chegam pelo enquadramento de 4 bytes")
        verificar(all(len(v) == 6 for v in velas), "cada vela tem os seis campos, volume incluido")
        verificar(velas == sorted(velas, key=lambda v: v[T]),
                  "as velas saem ordenadas, mesmo vindas de varios pedidos")
        verificar(abs(velas[-1][C] - falso.velas[-1][C]) < 0.01,
                  "o preco sobrevive a ida e volta pelos deltas do cTrader")

        recentes = broker.m1_recentes(30)
        verificar(0 < len(recentes) <= 40, "a vigia pede so os ultimos minutos")

        broker.nova_ordem(lado="compra", volume=1_000, stop=1.0, take=2.0)
        time.sleep(0.4)
        verificar(len(falso.ordens) == 1, "a ordem chegou ao broker")
        verificar(int(falso.ordens[0]["tradeSide"]) == 1, "uma compra vai como tradeSide 1")
        verificar(len(broker.posicoes()) == 1, "o reconcile ve a posicao que a ordem abriu")

        pos = broker.posicoes()[0]
        broker.mexer_sltp(pos["positionId"], 1.5, 2.5)
        time.sleep(0.4)
        verificar(len(falso.amendas) == 1, "o mexer no stop/take chegou ao broker")
        broker.fechar_posicao(pos["positionId"], pos["volume"])
        time.sleep(0.4)
        verificar(not broker.posicoes(), "depois de fechar, o reconcile ja nao ve nada")
        verificar(broker.execucoes_recentes(0), "os eventos sem clientMsgId foram apanhados")
    finally:
        if broker:
            broker.fechar()
        falso.parar()

    print("\n=== 9. Os quatro momentos ===")
    falso2 = BrokerFalso()
    falso2.start()
    broker2 = None
    try:
        broker2 = broker_de_teste(falso2)
        dados, m1_teste = None, broker2.m1(2)
        dados = fotografia(m1_teste, agora_utc_min(), detalhes=broker2.detalhes)
        verificar([e["nome"] for e in dados["escalas"]] == ["M15", "H1", "H4", "D1"],
                  "a fotografia traz as quatro escalas")
        verificar(dados["escalas"][0]["atr"] is not None, "o M15 tem ATR")
        verificar("VOLUME DE TICKS" in dados["lambda_definicao"],
                  "a ressalva do volume de ticks vai na saida, nao num comentario")
        verificar(len(formatar(dados)) > 200, "a fotografia escreve-se em texto")

        acima = next(d for d in dados["regua"]["degraus"] if d["preco"] > dados["preco"])
        abaixo = next(d for d in dados["regua"]["degraus"] if d["preco"] < dados["preco"])

        estado = Estado(tmp / "moments.db")
        resposta_observa = json.dumps({
            "acao": "armar", "nivel": abaixo["etiqueta"], "afastamento_atr": 0,
            "lado": "compra", "porque": "teste", "invalidacao": "teste",
            "validade_min": 240})
        llm = ModeloFalso([resposta_observa])
        ag = Agente(estado, broker2, llm)
        ag.momento_observa()
        m = estado.maquina()
        verificar(m["momento"] == "armado", "o momento observa arma quando ele diz armar")
        verificar(abs(m["armado"]["gatilho"] - abaixo["preco"]) < 1e-6,
                  "o gatilho e o preco do nivel que ele escolheu PELO NOME")
        verificar(m["armado"]["toque"] == "abaixo",
                  "o sentido do toque fica fixado ao armar, e nao no momento do toque")
        verificar(COMO_SE_LE.split("\n")[0] in llm.chamadas[0]["sistema"],
                  "o SISTEMA que ele leu e o que ensina a ler a fotografia")

        # O toque: uma vela cujo FECHO nao passa o gatilho, mas cuja MINIMA passa.
        gatilho = m["armado"]["gatilho"]
        agora = agora_utc_min()
        falso2.velas = falso2.velas[:-3] + [
            (agora - 2, gatilho + 5, gatilho + 6, gatilho + 4, gatilho + 5, 10.0),
            (agora - 1, gatilho + 5, gatilho + 6, gatilho - 0.5, gatilho + 5, 10.0),
            (agora, gatilho + 5, gatilho + 6, gatilho + 4, gatilho + 5, 10.0)]
        armado = m["armado"]
        armado["armado_ts"] = agora - 3
        armado["expira_ts"] = agora + 500
        estado.gravar_maquina("armado", armado=armado)
        verificar(ag.vigiar(), "a vigia dispara com uma vela cuja MINIMA toca, e o fecho nao")

        armado["gatilho"] = gatilho - 100.0
        estado.gravar_maquina("armado", armado=armado)
        verificar(not ag.vigiar(), "a vigia nao dispara com um gatilho que ninguem tocou")

        # A vela que JA estava a formar-se quando ele armou nao serve de prova:
        # se contasse, um toque anterior a decisao passava por um toque futuro.
        armado["gatilho"] = gatilho
        armado["armado_ts"] = agora - 1          # a vela que tocou e esta mesma
        estado.gravar_maquina("armado", armado=armado)
        verificar(not ag.vigiar(),
                  "um toque na vela que ja estava aberta ao armar NAO conta")
        armado["armado_ts"] = agora - 2          # agora a vela que tocou e posterior
        estado.gravar_maquina("armado", armado=armado)
        verificar(ag.vigiar(), "um toque numa vela posterior ao armar conta")

        armado["gatilho"] = gatilho
        armado["expira_ts"] = agora - 1
        estado.gravar_maquina("armado", armado=armado)
        verificar(not ag.vigiar(), "um nivel expirado nao dispara")
        verificar(estado.maquina()["momento"] == "observa",
                  "um nivel expirado volta a observar sozinho")

        # TOCOU -> entrar
        armado["expira_ts"] = agora + 500
        estado.gravar_maquina("armado", armado=armado)
        ag.llm = ModeloFalso([json.dumps({
            "acao": "entrar", "stop_nivel": abaixo["etiqueta"], "stop_afastamento_atr": -0.5,
            "take_nivel": acima["etiqueta"], "take_afastamento_atr": 0.5,
            "porque": "teste"})])
        falso2.saldo = 5_000_000.0
        ag.momento_tocou()
        verificar(len(falso2.ordens) == 1, "o momento tocou manda uma ordem a serio")
        verificar(estado.maquina()["momento"] == "dentro", "e passa para dentro")
        pos_m = estado.maquina()["posicao"]
        verificar(pos_m and pos_m["position_id"] == 5001,
                  "a posicao guardada e a que o broker confirmou, nao a que eu supus")

        # A CERCA: com posicao aberta, nao ha caminho ate uma segunda ordem.
        estado.gravar_maquina("armado", armado=armado)
        ag.llm = ModeloFalso([json.dumps({
            "acao": "entrar", "stop_nivel": abaixo["etiqueta"], "stop_afastamento_atr": -0.5,
            "take_nivel": acima["etiqueta"], "take_afastamento_atr": 0.5,
            "porque": "outra vez"})])
        ag.momento_tocou()
        verificar(len(falso2.ordens) == 1,
                  "com uma posicao aberta, uma segunda ordem NAO sai")
        verificar(estado.maquina()["momento"] == "dentro",
                  "e a maquina volta a alinhar-se com o que o broker tem")

        # DENTRO: mexer, e a recusa do stop que se afasta.
        ag.ultimo_dentro = 0
        ag.llm = ModeloFalso([json.dumps({
            "acao": "mexer", "stop_nivel": abaixo["etiqueta"], "stop_afastamento_atr": -3.0,
            "take_nivel": acima["etiqueta"], "take_afastamento_atr": 0.5,
            "porque": "afastar"})])
        ag.momento_dentro()
        verificar(not falso2.amendas,
                  "um stop que se AFASTA e recusado: a posicao ja foi dimensionada")

        ag.llm = ModeloFalso([json.dumps({
            "acao": "mexer", "stop_nivel": abaixo["etiqueta"], "stop_afastamento_atr": 0.2,
            "take_nivel": acima["etiqueta"], "take_afastamento_atr": 0.5,
            "porque": "apertar"})])
        ag.momento_dentro()
        verificar(len(falso2.amendas) == 1, "um stop que APERTA passa")

        # DENTRO: sair.
        ag.llm = ModeloFalso([json.dumps({"acao": "sair", "porque": "acabou"})])
        ag.momento_dentro()
        verificar(len(falso2.fechos) == 1, "sair fecha a posicao no broker")
        verificar(estado.maquina()["momento"] == "observa", "e volta a observar")
        verificar(estado.posicoes()[0]["fechada_em"] is not None,
                  "a posicao fica registada como fechada, com o R que deu")

        # Um modelo que so devolve lixo NAO pode acabar numa entrada.
        estado.gravar_maquina("observa")
        antes = len(falso2.ordens)
        ag.llm = ModeloFalso(["nao e json", "tambem nao", "nem isto"])
        ag.momento_observa()
        verificar(len(falso2.ordens) == antes and estado.maquina()["momento"] == "observa",
                  "um modelo que devolve lixo deixa-o de fora — nao decidir e ficar de fora")
        verificar(estado.ultima_decisao()["erro"],
                  "e a falha fica registada, em vez de desaparecer")

        # O guarda, aplicado ao que ele responde de verdade.
        estado.gravar_maquina("observa")
        ag.llm = ModeloFalso([json.dumps({
            "acao": "esperar", "porque": "a resistencia esta em 99999.5", "invalidacao": ""})] * 3)
        ag.momento_observa()
        verificar(estado.ultima_decisao()["erro"],
                  "uma resposta com um preco inventado e rejeitada as tres vezes")

        # O `passo` e o que corre de verdade. Testar so os momentos deixava de
        # fora o despacho — que e onde uma cadencia mal posta faria o agente
        # nunca olhar para o mercado.
        estado.gravar_maquina("observa")
        ag.ultimo_observa = 0.0
        ag.ultima_sincronia = 0.0
        ag.llm = ModeloFalso([json.dumps({
            "acao": "esperar", "porque": "nada aqui", "invalidacao": ""})])
        ag.passo()
        verificar(estado.ultima_decisao()["momento"] == "observa",
                  "o passo despacha para observa quando nao ha nada armado")
        antes_chamadas = len(ag.llm.chamadas)
        ag.passo()
        verificar(len(ag.llm.chamadas) == antes_chamadas,
                  "o passo seguinte nao volta a olhar antes da cadencia")

        estado.fechar()
    finally:
        if broker2:
            broker2.fechar()
        falso2.parar()

    print("\n=== 10. As tres fechaduras do live ===")
    global CONTA
    original = CONTA
    try:
        CONTA = "demo"
        verificar(host_da_conta() == "demo.ctraderapi.com", "o host de demo sai de CONTA")
        exigir_conta_permitida()
        verificar(True, "demo arranca sem pedir licenca a ninguem")
        CONTA = "live"
        verificar(host_da_conta() == "live.ctraderapi.com", "o host de live sai da MESMA constante")
        os.environ.pop("AGENTE_PERMITIR_LIVE", None)
        try:
            exigir_conta_permitida()
            verificar(False, "live sem AGENTE_PERMITIR_LIVE tem de recusar arrancar")
        except ErroBroker as e:
            verificar("AGENTE_PERMITIR_LIVE" in str(e),
                      "live sem a variavel recusa, e diz qual e a variavel")
        os.environ["AGENTE_PERMITIR_LIVE"] = "1"
        exigir_conta_permitida()
        verificar(True, "live com a variavel a 1 arranca")
        os.environ.pop("AGENTE_PERMITIR_LIVE", None)
        CONTA = "coisa"
        try:
            host_da_conta()
            verificar(False, "uma CONTA que nao existe tem de rebentar")
        except ErroBroker:
            verificar(True, "uma CONTA desconhecida rebenta em vez de escolher um host")
    finally:
        CONTA = original

    print("\n=== 11. A conta e confirmada, nao assumida ===")
    falso3 = BrokerFalso(velas=velas_falsas(2000), conta=111)
    falso3.start()
    try:
        lig = Ligacao("127.0.0.1", falso3.porta, tls=False, timeout=10)
        errado = CTrader("ETHUSD", ligacao=lig,
                         creds={"cliente": "id", "segredo": "s", "token": "t", "conta": 999})
        try:
            errado.ligar()
            verificar(False, "uma conta que o token nao autoriza tem de parar o arranque")
        except ErroBroker as e:
            verificar("999" in str(e) and "111" in str(e),
                      "o erro da conta trocada mostra os DOIS valores")
        errado.fechar()
    finally:
        falso3.parar()

    print("\n=== 12. O Estado nao atravessa threads ===")
    with Estado(tmp / "threads.db") as e0:
        recado = {}

        def noutra_thread():
            try:
                e0.exigir_mesma_thread("o agente")
                recado["erro"] = None
            except RuntimeError as e:
                recado["erro"] = str(e)

        t = threading.Thread(target=noutra_thread)
        t.start()
        t.join(5)
        verificar(recado.get("erro") and "outra thread" in recado["erro"],
                  "usar um Estado noutra thread da um erro que diz o que fazer")
        e0.exigir_mesma_thread("o teste")
        verificar(True, "na propria thread nao se queixa de nada")

    print("\n=== 13. O estado sobrevive a um restart ===")
    caminho = tmp / "restart.db"
    with Estado(caminho) as e1:
        e1.gravar_maquina("armado", armado={"nivel": "max H1", "gatilho": 123.0,
                                            "lado": "compra", "toque": "acima",
                                            "afastamento_atr": 0.0, "porque": "x",
                                            "validade_min": 60, "expira_ts": 9, "armado_ts": 1})
    with Estado(caminho) as e2:
        m = e2.maquina()
        verificar(m["momento"] == "armado" and abs(m["armado"]["gatilho"] - 123.0) < 1e-9,
                  "o nivel armado sobrevive a fechar e reabrir a base")

    print()
    if falhas:
        print(f"❌ {len(falhas)} falhas:")
        for f in falhas:
            print(f"   - {f}")
        return 1
    print("✅ tudo verde.")
    return 0


# ===========================================================================
#  ARRANQUE
# ===========================================================================

def montar() -> tuple[CTrader, object]:
    """Liga, resolve o simbolo, e escolhe o mensageiro.

    NAO abre o Estado: quem o abre e a thread que o vai usar. O sqlite3 recusa
    uma ligacao que atravesse threads, e o WAL trata da concorrencia entre as
    duas ligacoes muito melhor do que eu trataria de a passar de uma para a
    outra.
    """
    broker = CTrader(SIMBOLO)
    broker.ligar()
    broker.resolver_simbolo()
    tok = token_telegram()
    aviso = AvisoConsola()
    if tok and CHAT_ID:
        try:
            aviso = AvisoTelegram(Telegram(tok), CHAT_ID)
        except ValueError as e:
            print(f"aviso: {e}\nSigo sem Telegram.", file=sys.stderr)
    return broker, aviso


def cmd_verificar() -> int:
    """O passo que nao se salta. E aqui que se apanha a conta trocada."""
    print(f"{carimbo()} host: {host_da_conta()}:{PORTA_JSON} (JSON)")
    broker = CTrader(SIMBOLO)
    try:
        broker.ligar()
        conta = broker.conta()
        print(f"conta    : {conta['id']}")
        print(f"saldo    : {conta['saldo']:.2f}")
        detalhes = broker.resolver_simbolo()
        print(f"simbolo  : {detalhes['nome']} (id {detalhes['symbolId']})")
        print(f"digits   : {detalhes['digits']}   pipPosition: {detalhes['pipPosition']}")
        print(f"lotSize  : {detalhes['lotSize']}")
        print(f"minVolume: {detalhes['minVolume']}   stepVolume: {detalhes['stepVolume']}")
        velas = broker.m1(2)
        idade = agora_utc_min() - int(velas[-1][T])
        print(f"candles  : {len(velas)} M1, o ultimo de ha {idade} min")
        abertas = broker.posicoes()
        print(f"posicoes : {len(abertas)} aberta(s) neste simbolo")
        for p in abertas:
            print(f"           {p['lado']} volume {p['volume']} a {p['preco']:.5g}")
    finally:
        broker.fechar()
    return 0


def cmd_contexto() -> int:
    broker = CTrader(SIMBOLO)
    try:
        broker.ligar()
        broker.resolver_simbolo()
        dados = fotografia(broker.m1(), agora_utc_min(), simbolo=SIMBOLO,
                           detalhes=broker.detalhes)
    finally:
        broker.fechar()
    print(formatar(dados))
    return 0


def correr() -> int:
    parar = threading.Event()
    signal.signal(signal.SIGINT, lambda *a: (print("\na parar..."), parar.set()))
    signal.signal(signal.SIGTERM, lambda *a: parar.set())

    broker, aviso = montar()
    aviso.enviar(
        f"{carimbo()} 🤖 A arrancar em {SIMBOLO}, {host_da_conta()}.\n"
        f"observa de {MINUTOS_OBSERVA} em {MINUTOS_OBSERVA} min · "
        f"vigia de {SEGUNDOS_VIGIA} em {SEGUNDOS_VIGIA} s · "
        f"gere de {MINUTOS_DENTRO} em {MINUTOS_DENTRO} min")

    # O agente vive aqui dentro para o Estado nascer na thread que o usa.
    caixa: dict[str, Agente] = {}

    def tarefa_agente():
        with Estado(BD) as meu:
            agente = Agente(meu, broker, Ollama(), aviso)
            caixa["agente"] = agente
            agente.correr(parar)

    threads = [threading.Thread(target=tarefa_agente, name="agente", daemon=True)]
    tok = token_telegram()
    if tok and CHAT_ID:
        def tarefa_bot():
            with Estado(BD) as meu:
                # O bot so precisa do agente para a fotografia do /foto, e essa
                # nao toca no Estado dele.
                for _ in range(50):
                    if "agente" in caixa:
                        break
                    parar.wait(0.2)
                Bot(meu, caixa.get("agente"), Telegram(tok), parar).correr()
        threads.append(threading.Thread(target=tarefa_bot, name="bot", daemon=True))

    for t in threads:
        t.start()
    try:
        while not parar.is_set() and any(t.is_alive() for t in threads):
            parar.wait(1)
    except KeyboardInterrupt:
        parar.set()
    for t in threads:
        t.join(timeout=10)
    broker.fechar()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Agente de trading ao vivo: o modelo decide, o codigo mede e executa.")
    ap.add_argument("comando", nargs="?", default="correr",
                    choices=["correr", "verificar", "contexto", "teste"])
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    if a.comando == "teste":
        return autoteste()
    try:
        if a.comando == "verificar":
            return cmd_verificar()
        if a.comando == "contexto":
            return cmd_contexto()
        return correr()
    except ErroBroker as e:
        print(f"\n{e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    _codigo = main()
    # Só sai com codigo se houver mesmo um erro: assim o depurador nao mostra
    # um SystemExit(0) como se fosse uma excecao.
    if _codigo:
        sys.exit(_codigo)
