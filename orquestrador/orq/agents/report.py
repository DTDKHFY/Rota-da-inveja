"""Relatorio para o Telegram.

Decisao de desenho que importa mais do que parece: os NUMEROS na mensagem que
te pede aprovacao sao gerados por codigo, nunca pelo modelo. Um 7B a reescrever
"Sharpe 1.24" como "Sharpe 1.42" e um erro plausivel e silencioso, e serias tu
a carregar em Aprovar com base nele.

O modelo entra so no fim, para escrever uma frase de leitura do resultado. Se
essa frase falhar ou vier estranha, a mensagem sai na mesma — sem comentario.
"""
from __future__ import annotations

from typing import Any

from ..gate import Verdict
from ..metrics import WindowMetrics
from .base import Agent, AgentError

SYSTEM = """Es um analista quantitativo a comentar o resultado de um backtest
para o dono da estrategia, em portugues.

Escreves UMA frase, no maximo duas. Direto, sem entusiasmo, sem vendas.
Se o resultado for fraco ou suspeito, dizes isso claramente.

Regras absolutas:
- NUNCA repitas numeros. Eles ja aparecem na mensagem; se os repetires e te
  enganares, induzes uma decisao errada.
- Comenta o SIGNIFICADO: o resultado e solido, e frageil, cheira a overfit,
  precisa de mais ensaios.
- Responde so com a frase. Sem JSON, sem aspas, sem prefixos.
"""


def _fmt_janela(nome: str, m: WindowMetrics) -> str:
    linhas = [
        f"*{nome}*",
        f"  Sharpe: {m.sharpe_annualised:.2f}",
        f"  Drawdown: {m.max_drawdown * 100:.1f}%",
        f"  Trades: {m.trades}",
    ]
    if m.total_return is not None:
        linhas.insert(2, f"  Retorno: {m.total_return * 100:+.1f}%")
    return "\n".join(linhas)


def _fmt_alteracao(alteracao: dict | None, limite: int = 1200) -> str:
    """Mostra o codigo que muda.

    Aprovar uma alteracao de codigo sem a ver e pior do que aprovar parametros
    sem os ver: uma linha muda comportamento de formas que uma metrica agregada
    nao revela. Se nao couber na mensagem, e dito explicitamente que foi cortada
    — nunca truncada em silencio.
    """
    if not alteracao:
        return ""
    linhas = [f"*Codigo* — `{alteracao['ficheiro']}`"]
    gasto = 0
    for i, edicao in enumerate(alteracao.get("edicoes", [])):
        antes = edicao["procurar"].rstrip("\n")
        depois = edicao["substituir"].rstrip("\n")
        bloco = "```\n" + "\n".join(
            [*(f"- {l}" for l in antes.splitlines()),
             *(f"+ {l}" for l in depois.splitlines())]
        ) + "\n```"
        if gasto + len(bloco) > limite:
            restantes = len(alteracao["edicoes"]) - i
            linhas.append(
                f"_(+{restantes} altera\u00e7\u00e3o(oes) n\u00e3o cabem aqui — "
                f"v\u00ea o ramo git depois de aprovares)_"
            )
            break
        gasto += len(bloco)
        linhas.append(bloco)
    return "\n".join(linhas)


def _fmt_params(params: dict, anteriores: dict | None) -> str:
    linhas = []
    for chave in sorted(params):
        novo = params[chave]
        if anteriores and chave in anteriores and anteriores[chave] != novo:
            linhas.append(f"  {chave}: {anteriores[chave]:g} → {novo:g}")
        else:
            linhas.append(f"  {chave}: {novo:g}")
    return "\n".join(linhas)


def build_approval_message(
    *,
    exp_id: str,
    hipotese: str,
    params: dict,
    params_anteriores: dict | None,
    train: WindowMetrics,
    validation: WindowMetrics,
    verdict: Verdict,
    comentario: str | None = None,
    alteracao: dict | None = None,
) -> str:
    """Mensagem deterministica de pedido de aprovacao. Todos os numeros vem daqui."""
    cabecalho = "🟢 Proposta passou no gate" if verdict.passed else "🔴 Proposta chumbou no gate"
    partes = [
        f"{cabecalho}\n`{exp_id}`",
        f"\n*Hipotese*\n{hipotese}" if hipotese else "",
        f"\n{_fmt_alteracao(alteracao)}" if alteracao else
        f"\n*Parametros*\n{_fmt_params(params, params_anteriores)}",
        f"\n{_fmt_janela('Treino (in-sample)', train)}",
        f"\n{_fmt_janela('Validacao (out-of-sample)', validation)}",
        f"\n*Gate*\n{verdict.summary()}",
    ]
    if comentario:
        partes.append(f"\n_{comentario}_")
    partes.append(
        f"\n⚠️ O holdout NAO foi tocado. Estes numeros sao de validacao, "
        f"depois de {verdict.n_trials} ensaios neste estudo."
    )
    return "\n".join(p for p in partes if p)


class CommentAgent(Agent):
    """Uma frase de leitura do resultado. Opcional por desenho."""

    role = "report"
    json_mode = False

    def system_prompt(self) -> str:
        return SYSTEM

    def build_prompt(self, *, verdict: Verdict, hipotese: str, **_: Any) -> str:
        estado = "passou" if verdict.passed else "chumbou"
        falhas = "; ".join(c.detail for c in verdict.failures) or "nenhum criterio falhou"
        return (
            f"Hipotese testada: {hipotese or '(nao especificada)'}\n"
            f"Resultado: {estado} no gate apos {verdict.n_trials} ensaios.\n"
            f"Criterios falhados: {falhas}\n"
            f"Deflated Sharpe: {verdict.dsr:.3f}\n"
            f"Avisos: {'; '.join(verdict.warnings) or 'nenhum'}\n\n"
            f"Escreve a tua frase."
        )

    def parse(self, data: Any, **_: Any) -> str:
        texto = str(data).strip().strip('"').strip()
        if not texto:
            raise ValueError("comentario vazio")
        if len(texto) > 400:
            texto = texto[:397] + "..."
        return texto

    def comment_or_none(self, *, verdict: Verdict, hipotese: str, experiment_id: str | None = None) -> str | None:
        """Nunca deixa o relatorio falhar por causa do comentario."""
        try:
            return self.run(verdict=verdict, hipotese=hipotese, experiment_id=experiment_id)
        except AgentError:
            return None
