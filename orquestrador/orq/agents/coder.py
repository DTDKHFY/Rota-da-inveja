"""Agente de Desenvolvimento: transforma uma hipotese em alteracoes de codigo.

Recebe a hipotese do Agente Pesquisa e os ficheiros de estrategia, e devolve
edicoes concretas. Nao ve — e nao pode alterar — o codigo que corre e mede o
backtest.

Trabalha por blocos procurar/substituir e nao por diff unificado, e as edicoes
sao experimentadas em memoria dentro do `parse`: se um bloco nao encaixa, o
modelo recebe de volta a razao exata e tenta outra vez, em vez de gastarmos um
worktree e quarenta minutos de backtest para descobrir que o patch nao aplica.
"""
from __future__ import annotations

from typing import Any

from ..patching import (
    PatchError, apply_edits, edit_size, ensure_path_allowed, parse_edits,
)
from .base import Agent

LIMITE_CONTEXTO = 60_000

SYSTEM = """Es um programador a trabalhar numa estrategia de trading.

Recebes uma hipotese e o conteudo de ficheiros. Devolves alteracoes concretas a
UM ficheiro, na forma de blocos procurar/substituir.

Regras absolutas:
- Responde SO com JSON. Sem texto antes ou depois.
- O bloco "procurar" tem de ser copiado EXATAMENTE do ficheiro, com a mesma
  indentacao. E procurado como texto literal.
- O bloco "procurar" tem de ser unico no ficheiro. Se o trecho se repetir,
  inclui linhas de contexto a volta ate ser unico.
- Altera o minimo necessario para testar a hipotese. Nao reformates, nao
  reorganizes, nao "melhores" codigo que nao faz parte da hipotese.
- So podes alterar ficheiros da lista de editaveis que te for dada.
- Nao alteres nada que calcule ou registe metricas. Se a hipotese so se
  consegue testar mexendo nisso, di-lo em "justificacao" e devolve uma lista
  de edicoes com uma unica edicao que nao muda nada.

Formato exato da resposta:
{"ficheiro": "caminho/relativo.py",
 "edicoes": [{"procurar": "texto exato", "substituir": "texto novo"}],
 "justificacao": "uma ou duas frases"}
"""


def render_files(ficheiros: dict[str, str], limite: int = LIMITE_CONTEXTO) -> str:
    """Junta os ficheiros num bloco legivel, com numeros de linha para orientacao."""
    partes: list[str] = []
    gasto = 0
    for caminho, conteudo in sorted(ficheiros.items()):
        corpo = "\n".join(
            f"{n:>4} | {linha}" for n, linha in enumerate(conteudo.splitlines(), 1)
        )
        bloco = f"===== {caminho} =====\n{corpo}\n"
        if gasto + len(bloco) > limite:
            partes.append(
                f"===== {caminho} =====\n[omitido: contexto esgotado. "
                f"Restringe target.editable_paths a menos ficheiros.]\n"
            )
            continue
        gasto += len(bloco)
        partes.append(bloco)
    return "\n".join(partes)


class CodeAgent(Agent):
    """O Agente de Desenvolvimento."""

    role = "coder"

    def __init__(self, provider, model, *, editable_paths, max_edit_lines=120, **kwargs):
        super().__init__(provider, model, **kwargs)
        self.editable_paths = tuple(editable_paths)
        self.max_edit_lines = max_edit_lines

    def system_prompt(self) -> str:
        return SYSTEM

    def build_prompt(self, *, hipotese: dict, ficheiros: dict[str, str], **_: Any) -> str:
        return (
            f"HIPOTESE A IMPLEMENTAR:\n"
            f"{hipotese['nome']} — {hipotese['raciocinio']}\n\n"
            f"FICHEIROS QUE PODES ALTERAR:\n{', '.join(self.editable_paths)}\n\n"
            f"CONTEUDO (os numeros de linha sao so para te orientares — "
            f"nao os incluas nos blocos):\n{render_files(ficheiros)}\n\n"
            f"Limite: no maximo {self.max_edit_lines} linhas tocadas no total.\n\n"
            f"Devolve as edicoes."
        )

    def parse(self, data: Any, *, ficheiros: dict[str, str], **_: Any) -> dict:
        if not isinstance(data, dict):
            raise ValueError("a resposta tem de ser um objeto JSON")
        for chave in ("ficheiro", "edicoes"):
            if chave not in data:
                raise ValueError(f"falta a chave `{chave}`")

        caminho = str(data["ficheiro"]).strip().lstrip("./")

        # A guarda que importa. Antes de tudo o resto.
        ensure_path_allowed(caminho, self.editable_paths)

        if caminho not in ficheiros:
            raise ValueError(
                f"`{caminho}` nao esta entre os ficheiros que te mostrei. "
                f"Escolhe um de: {', '.join(sorted(ficheiros))}"
            )

        edicoes = parse_edits(data["edicoes"])

        tamanho = edit_size(edicoes)
        if tamanho > self.max_edit_lines:
            raise ValueError(
                f"a proposta toca {tamanho} linhas, o maximo e {self.max_edit_lines}. "
                "Reduz a alteracao ao minimo que testa a hipotese."
            )

        # Experimenta em memoria: se nao aplica, o modelo fica a saber porque.
        try:
            novo = apply_edits(ficheiros[caminho], edicoes)
        except PatchError as exc:
            raise ValueError(str(exc)) from exc

        if novo == ficheiros[caminho]:
            raise ValueError(
                "as edicoes nao mudam nada no ficheiro. Se a hipotese nao se "
                "consegue implementar aqui, diz isso em `justificacao`."
            )

        return {
            "ficheiro": caminho,
            "edicoes": [{"procurar": e.procurar, "substituir": e.substituir} for e in edicoes],
            "conteudo_novo": novo,
            "linhas_tocadas": tamanho,
            "justificacao": str(data.get("justificacao", ""))[:400],
        }
