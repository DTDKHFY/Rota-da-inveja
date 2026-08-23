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

import re
from dataclasses import dataclass
from pathlib import PurePosixPath


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
        padrao = str(PurePosixPath(bruto.strip().rstrip("/")))
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
