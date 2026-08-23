#!/usr/bin/env python3
"""O PROGRAMADOR — o agente que escreve codigo.

E um programa completo e independente. Nao sabe o que e Telegram, nao sabe o
que e um Sharpe, nao sabe o que e um backtest. Sabe uma coisa so:

    dada uma hipotese e um conjunto de ficheiros, propor uma alteracao valida

O `orquestrador.py` chama-o. Mas podes corre-lo sozinho, e vale a pena faze-lo
quando quiseres perceber porque e que o agente propos o que propos:

    python programador.py autoteste
    python programador.py ver      --projeto /caminho     # que ficheiros ve
    python programador.py propor   --projeto /caminho --hipotese "filtrar por volatilidade"
    python programador.py propor   --projeto /caminho --hipotese "..." --aplicar

A fronteira com o orquestrador e deliberada: aqui vive TUDO o que mexe em
codigo — lista branca, edicoes, validacao — e nada do que mede resultados. Quem
mede nao programa; quem programa nao mede. E por isso que o agente nao consegue
melhorar a sua propria nota.

Requisitos:  pip install requests
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Callable, Sequence

try:
    import requests
except ImportError:
    sys.exit("Falta a biblioteca requests.  Corre:  pip install requests")


# ===========================================================================
#  CONFIGURACAO (usada quando corres este ficheiro sozinho)
#
#  Quando e o orquestrador a chamar, e ele que manda os valores dele. Estas
#  constantes so valem para a linha de comandos.
# ===========================================================================

OLLAMA_URL = "http://localhost:11434"
MODELO = "dcxglm-5.2:cloud"
TIMEOUT_MODELO = 300
TENTATIVAS = 3

# Quantas linhas uma proposta pode tocar. Uma alteracao de 400 linhas nao e uma
# hipotese testavel — e uma reescrita, e ninguem a consegue rever pelo telemovel.
MAX_LINHAS_EDICAO = 120

# A lista branca. Os UNICOS ficheiros que este programa pode ler ou alterar.
FICHEIROS_EDITAVEIS = ["estrategia"]

# Teto do contexto enviado ao modelo, em caracteres.
LIMITE_CONTEXTO = 60_000


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
#  A guarda central deste programa. Um agente cuja tarefa e melhorar uma
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
                  validar: Callable, tentativas: int = TENTATIVAS,
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
                     editaveis: Sequence[str] = tuple(FICHEIROS_EDITAVEIS),
                     max_linhas: int = MAX_LINHAS_EDICAO,
                     modelo: str = MODELO, llm=None,
                     tentativas: int = TENTATIVAS) -> dict:
    """Transforma uma hipotese numa alteracao concreta e validada.

    A validacao corre em Python, nao no modelo, e a edicao e experimentada em
    memoria antes de sair daqui: se um bloco nao encaixa, o modelo recebe a
    razao exata e tenta outra vez. Assim nao gastamos um worktree e quarenta
    minutos de backtest para descobrir que o patch nao aplicava.

    Devolve: {ficheiro, edicoes, conteudo_novo, linhas, justificacao}
    """
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
#  LINHA DE COMANDOS
# ===========================================================================

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
        print(f"\n  ⚠️  Estes ficheiros editaveis tem nomes que sugerem que calculam")
        print(f"      resultados: {', '.join(suspeitos)}")
        print(f"      Se algum deles mede o desempenho, tira-o da lista branca. Um")
        print(f"      agente que pode reescrever a regua vai reescrever a regua.")
    return 0


def cmd_propor(projeto: Path, hipotese_txt: str, editaveis: Sequence[str],
               modelo: str, aplicar: bool, saida_json: bool) -> int:
    ficheiros = listar_editaveis(projeto, editaveis)
    if not ficheiros:
        print("Nenhum ficheiro corresponde a lista branca. Corre `ver` primeiro.",
              file=sys.stderr)
        return 1

    hipotese = {"nome": hipotese_txt, "raciocinio": ""}
    inicio = time.monotonic()
    try:
        proposta = propor_alteracao(ficheiros, hipotese, editaveis=editaveis,
                                    modelo=modelo)
    except (ErroAgente, ErroModelo) as e:
        print(f"\n❌ {e}\n", file=sys.stderr)
        return 1

    if saida_json:
        print(json.dumps(proposta, ensure_ascii=False, indent=2))
        return 0

    print(f"\nHipotese: {hipotese_txt}")
    print(f"Modelo:   {modelo}  ({time.monotonic() - inicio:.0f}s)")
    print(f"\n{pre_visualizar(proposta['ficheiro'], proposta['edicoes'])}")
    print(f"\nJustificacao: {proposta['justificacao'] or '(nenhuma)'}")
    print(f"Linhas tocadas: {proposta['linhas']}")

    if not aplicar:
        print("\nNao apliquei nada. Usa --aplicar se quiseres gravar.")
        print("Convem teres o projeto sob git antes de o fazeres.")
        return 0

    destino = escrever_alteracao(projeto, proposta, editaveis)
    print(f"\n✅ Gravado em {destino}")
    print("   Ve o diff com `git diff` antes de te comprometeres.")
    return 0


# ===========================================================================
#  AUTOTESTE — prova que este programa funciona, sem Ollama
# ===========================================================================

def autoteste() -> int:
    falhas = []

    def verificar(condicao, descricao):
        print(f"  {'✅' if condicao else '❌'} {descricao}")
        if not condicao:
            falhas.append(descricao)

    print("\n=== Lista branca ===")
    casos = [
        ("estrategia/sinal.py", ["estrategia"], True),
        ("estrategia/a/b/c.py", ["estrategia"], True),
        ("run_backtest.py", ["estrategia"], False),
        ("metricas.py", ["estrategia"], False),
        ("estrategia_falsa/x.py", ["estrategia"], False),
        ("estrategia/a/b.py", ["estrategia/**/*.py"], True),
        ("estrategia/a/b.txt", ["estrategia/**/*.py"], False),
        ("sinal.py", ["*.py"], True),
        ("a/metricas.py", ["*.py"], False),
        ("../../etc/passwd", ["estrategia"], False),
        ("/etc/passwd", ["estrategia"], False),
        ("qualquer.py", [], False),
    ]
    for caminho, padroes, esperado in casos:
        obtido = caminho_permitido(caminho, padroes)
        verificar(obtido is esperado,
                  f"{caminho!r} com {padroes} -> {'permitido' if esperado else 'proibido'}")

    print("\n=== Edicoes ===")
    verificar(aplicar_edicoes("a\nb\nc\n", [{"procurar": "b", "substituir": "B"}]) == "a\nB\nc\n",
              "uma edicao simples aplica")
    for descricao, edicoes, conteudo, esperado in [
        ("bloco inexistente da erro util", [{"procurar": "z", "substituir": "Z"}], "a\nb\n", "nao aparece"),
        ("bloco ambiguo pede contexto", [{"procurar": "x", "substituir": "y"}], "x\nx\nx\n", "aparece 3 vezes"),
        ("procurar vazio e recusado", [{"procurar": "  ", "substituir": "y"}], "a\n", "esta vazio"),
    ]:
        try:
            aplicar_edicoes(conteudo, edicoes)
            verificar(False, descricao)
        except ErroEdicao as e:
            verificar(esperado in str(e), f"{descricao}: {str(e)[:50]}...")

    print("\n=== Extracao de JSON de respostas sujas ===")
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

    print("\n=== O agente ===")
    ficheiros = {"estrategia/sinal.py": "def forca():\n    return 1.0\n"}
    hipotese = {"nome": "dobrar a forca", "raciocinio": "o filtro corta demais"}

    def proposta(ficheiro="estrategia/sinal.py", procurar="    return 1.0",
                 substituir="    return 2.0"):
        return json.dumps({"ficheiro": ficheiro,
                           "edicoes": [{"procurar": procurar, "substituir": substituir}],
                           "justificacao": "teste"})

    # O cenario que importa: o modelo tenta reescrever quem o avalia.
    llm = ModeloFalso([proposta(ficheiro="metricas.py", procurar="sharpe"),
                       proposta()])
    saida = propor_alteracao(ficheiros, hipotese, editaveis=["estrategia"], llm=llm,
                             modelo="falso", tentativas=3)
    verificar(saida["ficheiro"] == "estrategia/sinal.py",
              "tentativa de editar metricas.py foi recusada e corrigida")
    verificar("intocaveis" in llm.chamadas[1]["utilizador"],
              "o modelo recebeu o motivo concreto da recusa")
    verificar(saida["conteudo_novo"] == "def forca():\n    return 2.0\n",
              "a alteracao valida produziu o conteudo certo")
    verificar(saida["linhas"] == 2, "contagem de linhas tocadas")

    llm = ModeloFalso([proposta(procurar="return 1.0 # comentario inexistente"), proposta()])
    propor_alteracao(ficheiros, hipotese, editaveis=["estrategia"], llm=llm,
                     modelo="falso", tentativas=3)
    verificar("nao aparece" in llm.chamadas[1]["utilizador"],
              "bloco que nao encaixa e explicado ao modelo")

    grande = "\n".join(f"linha {i}" for i in range(60))
    llm = ModeloFalso([proposta(substituir=grande)] * 3)
    try:
        propor_alteracao(ficheiros, hipotese, editaveis=["estrategia"], llm=llm,
                         modelo="falso", max_linhas=20, tentativas=2)
        verificar(False, "alteracao desproporcionada devia ser recusada")
    except ErroAgente:
        verificar("o maximo e 20" in llm.chamadas[1]["utilizador"],
                  "alteracao desproporcionada recusada com o limite explicito")

    llm = ModeloFalso([proposta(substituir="    return 1.0")] * 3)
    try:
        propor_alteracao(ficheiros, hipotese, editaveis=["estrategia"], llm=llm,
                         modelo="falso", tentativas=2)
        verificar(False, "alteracao nula devia ser recusada")
    except ErroAgente:
        verificar(True, "alteracao que nao muda nada e recusada")

    llm = ModeloFalso(["isto nao e json"] * 5)
    try:
        propor_alteracao(ficheiros, hipotese, editaveis=["estrategia"], llm=llm,
                         modelo="falso", tentativas=2)
        verificar(False, "resposta sem JSON devia falhar")
    except ErroAgente as e:
        verificar("falhou 2 tentativas" in str(e), "desiste ao fim das tentativas")

    print("\n" + "=" * 60)
    if falhas:
        print(f"❌ {len(falhas)} verificacao(oes) falharam:")
        for f in falhas:
            print(f"   - {f}")
        return 1
    print("✅ O programador funciona.")
    return 0


def painel_inicial() -> int:
    """Sem argumentos: explicar, em vez de despejar um erro de argparse."""
    print(f"""
╭─────────────────────────────────────────────────────────────╮
│  PROGRAMADOR — o agente que escreve codigo                   │
╰─────────────────────────────────────────────────────────────╯

Correste sem argumentos (foi o botao Run do editor, provavelmente).

  python programador.py autoteste          verifica que funciona,
                                           sem Ollama nem GPU
                                           👉 COMECA POR AQUI

  python programador.py ver     --projeto C:\\caminho
                                           que ficheiros o agente
                                           alcanca e quais estao
                                           protegidos

  python programador.py propor  --projeto C:\\caminho --hipotese "..."
                                           pedir uma alteracao e ver
                                           o diff, sem gravar nada

Este programa escreve codigo e nao sabe o que e um Sharpe. Quem mede
e o orquestrador.py, ao lado.
""")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="programador", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="comando")

    sub.add_parser("autoteste", help="verifica que este programa funciona, sem Ollama")

    p_ver = sub.add_parser("ver", help="que ficheiros o agente pode e nao pode tocar")
    p_ver.add_argument("--projeto", required=True)
    p_ver.add_argument("--editaveis", nargs="+", default=FICHEIROS_EDITAVEIS)

    p_prop = sub.add_parser("propor", help="pedir uma alteracao ao modelo")
    p_prop.add_argument("--projeto", required=True)
    p_prop.add_argument("--hipotese", required=True)
    p_prop.add_argument("--editaveis", nargs="+", default=FICHEIROS_EDITAVEIS)
    p_prop.add_argument("--modelo", default=MODELO)
    p_prop.add_argument("--aplicar", action="store_true", help="grava no disco")
    p_prop.add_argument("--json", action="store_true", help="saida em JSON")

    a = ap.parse_args(argv)
    if a.comando is None:
        return painel_inicial()
    if a.comando == "autoteste":
        return autoteste()
    projeto = Path(a.projeto).expanduser().resolve()
    if not projeto.is_dir():
        print(f"nao encontrei o projeto em {projeto}", file=sys.stderr)
        return 2
    if a.comando == "ver":
        return cmd_ver(projeto, a.editaveis)
    if a.comando == "propor":
        return cmd_propor(projeto, a.hipotese, a.editaveis, a.modelo, a.aplicar, a.json)
    return 2


if __name__ == "__main__":
    # Ver a nota no orquestrador.py: sair com 0 faria o depurador do VS Code
    # anunciar uma excecao onde nao ha nenhuma.
    _codigo = main()
    if _codigo:
        sys.exit(_codigo)
