import random

import pytest

from orq.agents import ProposerAgent, ResearchAgent, perturb_params, random_params
from orq.agents.base import AgentError
from orq.agents.report import CommentAgent, build_approval_message
from orq.config import ExperimentConfig, ParamSpec
from orq.gate import Check, Verdict
from orq.llm import FakeProvider, extract_json
from orq.llm.base import LLMError
from orq.metrics import parse_window_metrics

SCHEMA = {
    "sma_fast": ParamSpec("sma_fast", "int", 2, 50),
    "stop_atr": ParamSpec("stop_atr", "float", 0.5, 6.0),
}
EXP = ExperimentConfig(mode="params", params_schema=SCHEMA)
HIPOTESE = {"nome": "n", "raciocinio": "r", "parametros_alvo": ["stop_atr"], "direcao": "aumentar"}


# --- extractor de JSON ----------------------------------------------------

@pytest.mark.parametrize("texto,esperado", [
    ('{"a": 1}', {"a": 1}),
    ('Aqui esta:\n```json\n{"a": 2}\n```\nEspero que ajude', {"a": 2}),
    ('vou propor {"a": 3} porque sim', {"a": 3}),
    ('```\n[1, 2]\n```', [1, 2]),
    ('{"t": "aspas \\" e {chaveta}", "n": 4}', {"t": 'aspas " e {chaveta}', "n": 4}),
])
def test_extrai_json_de_resposta_suja(texto, esperado):
    """Um modelo de 7B quase nunca devolve so o JSON."""
    assert extract_json(texto) == esperado


def test_sem_json_levanta_erro():
    with pytest.raises(LLMError, match="JSON valido"):
        extract_json("desculpa, nao percebi")


# --- ciclo de correcao ----------------------------------------------------

def test_pesquisa_corrige_se_com_o_erro_concreto():
    provider = FakeProvider([
        '{"hipoteses":[{"nome":"x","raciocinio":"y","parametros_alvo":["rsi"],"direcao":"aumentar"}]}',
        '{"hipoteses":[{"nome":"Stop largo","raciocinio":"saidas cedo","parametros_alvo":["stop_atr"],"direcao":"aumentar"}]}',
    ])
    agente = ResearchAgent(provider, "m", max_retries=3)
    saida = agente.run(objetivo="reduzir drawdown", schema=SCHEMA, historico=[])

    assert saida[0]["nome"] == "Stop largo"
    assert len(provider.calls) == 2
    assert "rsi" in provider.calls[1]["user"], "o modelo tem de ver o erro concreto"


def test_pesquisa_rejeita_direcao_invalida():
    provider = FakeProvider([
        '{"hipoteses":[{"nome":"x","raciocinio":"y","parametros_alvo":["stop_atr"],"direcao":"turbinar"}]}',
    ])
    with pytest.raises(AgentError, match="turbinar|falhou"):
        ResearchAgent(provider, "m", max_retries=1).run(
            objetivo="o", schema=SCHEMA, historico=[]
        )


def test_proponente_rejeita_valor_fora_dos_limites():
    provider = FakeProvider([
        '{"params":{"sma_fast":900,"stop_atr":2.0}}',
        '{"params":{"sma_fast":18,"stop_atr":3.5},"justificacao":"ok"}',
    ])
    saida = ProposerAgent(provider, "m", EXP, max_retries=3).run(
        hipotese=HIPOTESE, params_atuais={"sma_fast": 10, "stop_atr": 2.0}, historico=[]
    )
    assert saida["params"] == {"sma_fast": 18, "stop_atr": 3.5}
    assert "fora dos limites" in provider.calls[1]["user"]


def test_proponente_desiste_ao_fim_das_tentativas():
    with pytest.raises(AgentError, match="falhou 2 tentativas"):
        ProposerAgent(FakeProvider(["lixo"] * 5), "m", EXP, max_retries=2).run(
            hipotese=HIPOTESE, params_atuais={}, historico=[]
        )


def test_rede_de_seguranca_quando_o_modelo_falha():
    """O estudo nao pode parar por o Llama ter tido um mau dia."""
    saida = ProposerAgent(FakeProvider(["lixo"] * 5), "m", EXP, max_retries=2).propose_with_fallback(
        hipotese=HIPOTESE, params_atuais={"sma_fast": 10, "stop_atr": 2.0},
        historico=[], rng=random.Random(1),
    )
    assert saida["fallback"] is True
    assert 2 <= saida["params"]["sma_fast"] <= 50
    assert 0.5 <= saida["params"]["stop_atr"] <= 6.0


def test_amostragem_respeita_sempre_os_limites():
    rng = random.Random(0)
    for _ in range(200):
        for params in (random_params(SCHEMA, rng), perturb_params({"sma_fast": 49, "stop_atr": 5.9}, SCHEMA, rng=rng)):
            assert 2 <= params["sma_fast"] <= 50
            assert 0.5 <= params["stop_atr"] <= 6.0
            assert isinstance(params["sma_fast"], int)


def test_agente_regista_tentativas(store):
    provider = FakeProvider(['{"params":{"sma_fast":18,"stop_atr":3.5}}'])
    ProposerAgent(provider, "modelo-x", EXP, store=store).run(
        hipotese=HIPOTESE, params_atuais={}, historico=[], experiment_id="exp_1"
    )
    registos = list(store._conn.execute("SELECT * FROM agent_runs"))
    assert len(registos) == 1
    assert registos[0]["role"] == "proposer"
    assert registos[0]["model"] == "modelo-x"
    assert registos[0]["ok"] == 1


# --- relatorio ------------------------------------------------------------

def _verdict(passed=True):
    return Verdict(
        passed=passed,
        checks=[Check("dsr", passed, 0.97, 0.95, "Deflated Sharpe: 0.970 apos 4 ensaios")],
        dsr=0.97, n_trials=4,
    )


def _janela(mu=0.0008):
    rng = random.Random(1)
    return parse_window_metrics({
        "returns": [rng.gauss(mu, 0.01) for _ in range(600)],
        "trades": 300, "max_drawdown": 0.12, "periods_per_year": 252,
    })


def test_mensagem_de_aprovacao_e_deterministica():
    """Os numeros que sustentam a tua decisao nunca sao escritos pelo modelo."""
    msg = build_approval_message(
        exp_id="exp_1", hipotese="stop mais largo",
        params={"sma_fast": 20, "stop_atr": 3.0},
        params_anteriores={"sma_fast": 10, "stop_atr": 3.0},
        train=_janela(), validation=_janela(0.0007), verdict=_verdict(),
    )
    assert "exp_1" in msg
    assert "sma_fast: 10 → 20" in msg, "a mudanca devia estar visivel"
    assert "stop_atr: 3" in msg
    assert "holdout NAO foi tocado" in msg
    assert "Deflated Sharpe" in msg


def test_mensagem_funciona_sem_comentario_do_modelo():
    msg = build_approval_message(
        exp_id="exp_2", hipotese="", params={"sma_fast": 20},
        params_anteriores=None, train=_janela(), validation=_janela(),
        verdict=_verdict(False), comentario=None,
    )
    assert "chumbou no gate" in msg


def test_comentario_falhado_nao_derruba_o_relatorio():
    agente = CommentAgent(FakeProvider([]), "m", max_retries=1)
    assert agente.comment_or_none(verdict=_verdict(), hipotese="x") is None
