"""Edicao de ficheiros por blocos procurar/substituir.

Porque nao diff unificado: para produzir um diff valido o modelo tem de acertar
em numeros de linha e contagens de contexto. Ate modelos grandes erram isso com
frequencia, e um diff que falha `git apply` gasta uma tentativa inteira sem
produzir nada.

Blocos procurar/substituir sao ancorados no conteudo, nao em coordenadas. O
modelo copia um pedaco do ficheiro e diz com o que o troca. Quando falha, falha
de forma diagnosticavel — "este bloco nao aparece" ou "aparece 3 vezes" — e
essas mensagens sao acionaveis, ao contrario de "patch does not apply".

Aqui tambem vive a lista branca de caminhos, e essa e a guarda mais importante
do modo `code`: um agente cuja tarefa e melhorar uma metrica tem um caminho
muito curto para a melhorar — editar o codigo que a calcula. A estrategia e
editavel; o arnes que a mede nunca e.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Sequence


class PatchError(ValueError):
    """Edicao invalida. A mensagem e escrita para o modelo se poder corrigir."""


class PathNotAllowed(PatchError):
    """Tentativa de editar um ficheiro fora da lista branca."""


@dataclass(frozen=True)
class Edit:
    procurar: str
    substituir: str


def path_allowed(rel_path: str, patterns: tuple[str, ...] | list[str]) -> bool:
    """O caminho relativo esta coberto por algum padrao da lista branca?

    Aceita tres formas: caminho exato (`estrategia/sinal.py`), prefixo de pasta
    (`estrategia` cobre tudo la dentro) e glob (`estrategia/**/*.py`).
    """
    if not patterns:
        return False
    alvo = str(PurePosixPath(rel_path))
    if alvo.startswith("/") or ".." in PurePosixPath(alvo).parts:
        return False  # nada de escapar da raiz do projeto

    for bruto in patterns:
        cru = str(bruto).strip()
        if cru in ("*", "**", "."):
            return True
        padrao = str(PurePosixPath(cru.rstrip("/")))
        if alvo == padrao or alvo.startswith(padrao + "/"):
            return True
        if re.fullmatch(_glob_para_regex(padrao), alvo):
            return True
    return False


def _glob_para_regex(padrao: str) -> str:
    """Traduz um glob para regex com semantica de caminhos.

    Nao uso `fnmatch`: la, `*` atravessa `/`, e portanto `*.py` casaria com
    `qualquer/pasta/run_backtest.py`. Numa lista branca de ficheiros editaveis
    isso e um buraco — o padrao que o utilizador escreveu a pensar na raiz do
    projeto passaria a cobrir o arnes de metricas dentro de qualquer subpasta.
    Aqui `*` para no separador e so `**` o atravessa.
    """
    return (
        re.escape(padrao)
        .replace(r"\*\*/", "(?:.*/)?")
        .replace(r"\*\*", ".*")
        .replace(r"\*", "[^/]*")
        .replace(r"\?", "[^/]")
    )


def ensure_path_allowed(rel_path: str, patterns: tuple[str, ...] | list[str]) -> None:
    if not path_allowed(rel_path, patterns):
        raise PathNotAllowed(
            f"`{rel_path}` nao esta na lista de ficheiros editaveis. "
            f"So podes alterar: {', '.join(patterns) or '(nada configurado)'}. "
            "Os ficheiros que correm e medem o backtest sao intocaveis."
        )


def parse_edits(bruto: object) -> list[Edit]:
    """Valida a lista de edicoes vinda do modelo."""
    if not isinstance(bruto, list) or not bruto:
        raise PatchError("`edicoes` tem de ser uma lista nao vazia")
    edicoes: list[Edit] = []
    for i, item in enumerate(bruto):
        if not isinstance(item, dict):
            raise PatchError(f"edicao {i} nao e um objeto")
        for chave in ("procurar", "substituir"):
            if chave not in item:
                raise PatchError(f"edicao {i} nao tem a chave `{chave}`")
            if not isinstance(item[chave], str):
                raise PatchError(f"edicao {i}: `{chave}` tem de ser texto")
        if not item["procurar"].strip():
            raise PatchError(
                f"edicao {i}: `procurar` esta vazio. Para acrescentar codigo, "
                "procura uma linha vizinha e devolve-a junto com o codigo novo."
            )
        edicoes.append(Edit(procurar=item["procurar"], substituir=item["substituir"]))
    return edicoes


def apply_edits(conteudo: str, edicoes: list[Edit]) -> str:
    """Aplica as edicoes por ordem. Cada bloco tem de aparecer exatamente uma vez.

    A exigencia de unicidade e deliberada: se um bloco aparece duas vezes, o
    modelo nao disse qual queria, e adivinhar seria alterar codigo ao acaso.
    """
    atual = conteudo
    for i, edicao in enumerate(edicoes):
        ocorrencias = atual.count(edicao.procurar)
        if ocorrencias == 0:
            aproximado = _pista_de_falha(atual, edicao.procurar)
            raise PatchError(
                f"edicao {i}: o bloco a procurar nao aparece no ficheiro.{aproximado} "
                "Copia o texto exatamente como esta, incluindo indentacao."
            )
        if ocorrencias > 1:
            raise PatchError(
                f"edicao {i}: o bloco aparece {ocorrencias} vezes e nao sei qual queres. "
                "Inclui mais linhas de contexto a volta para o tornar unico."
            )
        atual = atual.replace(edicao.procurar, edicao.substituir, 1)
    return atual


def _pista_de_falha(conteudo: str, procurado: str) -> str:
    """Ajuda o modelo a perceber porque falhou, quando da para perceber."""
    primeira = procurado.strip().splitlines()[0].strip() if procurado.strip() else ""
    if primeira and primeira in conteudo:
        return (
            f" A primeira linha (`{primeira[:60]}`) existe, portanto o que difere "
            "e a indentacao ou as linhas seguintes."
        )
    if primeira and primeira.replace(" ", "") in conteudo.replace(" ", ""):
        return " O texto existe mas com espacamento diferente."
    return ""


def edit_size(edicoes: list[Edit]) -> int:
    """Linhas tocadas, para travar propostas desproporcionadas."""
    return sum(
        len(e.procurar.splitlines()) + len(e.substituir.splitlines()) for e in edicoes
    )


# --------------------------------------------------------------------------
# Protecao ao nivel da funcao
#
# Para quando a estrategia e o calculo das metricas vivem no MESMO ficheiro — o
# caso comum em backtests que cresceram organicamente. Aqui a lista branca de
# ficheiros nao protege nada: para o agente poder mexer na estrategia, tem de
# poder mexer no ficheiro inteiro. A protecao tem de descer um nivel.
# --------------------------------------------------------------------------

def file_functions(source: str) -> dict[str, str]:
    """Nome -> codigo-fonte, para funcoes e classes a qualquer profundidade.

    Comparo o texto e nao a arvore: uma alteracao que so mude formatacao
    tambem e uma alteracao, e nao quero discutir com o modelo sobre o que conta
    como "igual".
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            try:
                out[node.name] = ast.get_source_segment(source, node) or ""
            except (ValueError, TypeError):
                continue
    return out


def check_frozen_functions(before: str, after: str,
                           frozen: Sequence[str]) -> str | None:
    """Alguma funcao congelada mudou? Devolve a queixa, ou None se esta tudo bem."""
    if not frozen:
        return None
    antes, depois = file_functions(before), file_functions(after)
    alteradas = [n for n in frozen if n in antes and n in depois and antes[n] != depois[n]]
    desaparecidas = [n for n in frozen if n in antes and n not in depois]
    if not alteradas and not desaparecidas:
        return None
    queixa = []
    if alteradas:
        queixa.append(f"alteraste {', '.join(alteradas)}")
    if desaparecidas:
        queixa.append(f"apagaste {', '.join(desaparecidas)}")
    return (f"{' e '.join(queixa)}. Essas funcoes calculam ou registam resultados e "
            "estao congeladas — se pudesses mexer nelas, podias melhorar a tua propria "
            "nota em vez de melhorar a estrategia. Faz a alteracao sem lhes tocar.")
