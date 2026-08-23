"""Modo `code`: o Agente de Desenvolvimento altera a estrategia — e so a estrategia.

O teste que importa mais neste ficheiro e
`test_recusa_editar_o_arnes_de_metricas`. Um agente cuja tarefa e melhorar uma
metrica tem um atalho obvio: reescrever a funcao que a calcula. Nao e um cenario
rebuscado — e o caminho de menor resistencia, e um modelo capaz encontra-o.
"""
import json
import subprocess

import pytest

from orq.agents import CodeAgent
from orq.agents.base import AgentError
from orq.config import ConfigError, load_config
from orq.llm import FakeProvider
from orq.orchestrator import NullNotifier, Orchestrator
from orq.patching import Edit, PatchError, PathNotAllowed, apply_edits, ensure_path_allowed, path_allowed
from orq.sandbox import Sandbox
from orq.worker import Worker

HIPOTESE = {"nome": "Sinal mais forte", "raciocinio": "o filtro esta a cortar demais",
            "parametros_alvo": ["sma_fast"], "direcao": "aumentar"}
FICHEIROS = {"estrategia/sinal.py": "def forca():\n    return 1.0\n"}


def _proposta(ficheiro="estrategia/sinal.py", procurar="    return 1.0", substituir="    return 2.0"):
    return json.dumps({
        "ficheiro": ficheiro,
        "edicoes": [{"procurar": procurar, "substituir": substituir}],
        "justificacao": "duplica a forca do sinal",
    })


def _coder(provider, editable=("estrategia",), max_edit_lines=40, max_retries=2):
    return CodeAgent(provider, "m", editable_paths=editable,
                     max_edit_lines=max_edit_lines, max_retries=max_retries)


# --- lista branca ---------------------------------------------------------

@pytest.mark.parametrize("caminho,padroes,esperado", [
    ("estrategia/sinal.py", ["estrategia"], True),
    ("estrategia/a/b/c.py", ["estrategia"], True),
    ("estrategia", ["estrategia"], True),
    ("run_backtest.py", ["estrategia"], False),
    ("estrategia_falsa/x.py", ["estrategia"], False),
    ("estrategia/a/b.py", ["estrategia/**/*.py"], True),
    ("estrategia/a/b.txt", ["estrategia/**/*.py"], False),
    ("sinal.py", ["*.py"], True),
    ("a/sinal.py", ["*.py"], False),
    ("../../etc/passwd", ["estrategia"], False),
    ("/etc/passwd", ["estrategia"], False),
    ("qualquer.py", [], False),
])
def test_lista_branca(caminho, padroes, esperado):
    assert path_allowed(caminho, padroes) is esperado


def test_erro_da_lista_branca_explica():
    with pytest.raises(PathNotAllowed, match="intocaveis"):
        ensure_path_allowed("run_backtest.py", ["estrategia"])


def test_modo_code_exige_lista_branca(tmp_path, alvo, monkeypatch):
    """Sem lista branca, o modo code nem arranca."""
    import yaml
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:A")
    corpo = {
        "telegram": {"allowed_chat_ids": [1]},
        "target": {"path": str(alvo), "backtest_cmd": "x"},
        "experiment": {"mode": "code"},
        "protocol": {
            "train_start": "2015-01-01", "train_end": "2020-12-31",
            "validation_start": "2021-01-01", "validation_end": "2022-12-31",
            "holdout_start": "2023-01-01", "holdout_end": "2024-12-31",
        },
    }
    caminho = tmp_path / "c.yaml"
    caminho.write_text(yaml.safe_dump(corpo))
    with pytest.raises(ConfigError, match="editable_paths"):
        load_config(caminho, env_path=tmp_path / "vazio")


# --- aplicacao de edicoes -------------------------------------------------

def test_edicao_aplica():
    novo = apply_edits("a\nb\nc\n", [Edit("b", "B")])
    assert novo == "a\nB\nc\n"


def test_bloco_inexistente_da_erro_util():
    with pytest.raises(PatchError, match="nao aparece"):
        apply_edits("a\nb\n", [Edit("z", "Z")])


def test_bloco_ambiguo_pede_mais_contexto():
    """Adivinhar qual das ocorrencias seria alterar codigo ao acaso."""
    with pytest.raises(PatchError, match="aparece 3 vezes"):
        apply_edits("x\nx\nx\n", [Edit("x", "y")])


def test_pista_quando_so_a_indentacao_difere():
    with pytest.raises(PatchError, match="indentacao ou as linhas seguintes"):
        apply_edits("    return 1.0\n", [Edit("return 1.0\nmais", "z")])


def test_edicoes_aplicam_por_ordem():
    novo = apply_edits("a\nb\n", [Edit("a", "b"), Edit("b\nb", "c")])
    assert novo == "c\n"


# --- o agente de desenvolvimento -----------------------------------------

def test_recusa_editar_o_arnes_de_metricas():
    """O atalho obvio para 'melhorar' um Sharpe e reescrever quem o calcula."""
    provider = FakeProvider([
        _proposta(ficheiro="run_backtest.py", procurar='"sharpe"', substituir='"sharpe_falso"'),
        _proposta(ficheiro="run_backtest.py", procurar='"sharpe"', substituir='"x"'),
    ])
    with pytest.raises(AgentError, match="falhou"):
        _coder(provider).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)

    reenviado = provider.calls[1]["user"]
    assert "run_backtest.py" in reenviado
    assert "intocaveis" in reenviado, "o modelo tem de saber porque foi recusado"


def test_recusa_ficheiro_que_nao_viu():
    provider = FakeProvider([_proposta(ficheiro="estrategia/outro.py")] * 3)
    with pytest.raises(AgentError):
        _coder(provider).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)
    assert "nao esta entre os ficheiros" in provider.calls[1]["user"]


def test_recusa_alteracao_desproporcionada():
    grande = "\n".join(f"linha {i}" for i in range(60))
    provider = FakeProvider([
        json.dumps({"ficheiro": "estrategia/sinal.py",
                    "edicoes": [{"procurar": "    return 1.0", "substituir": grande}]}),
    ] * 3)
    with pytest.raises(AgentError):
        _coder(provider, max_edit_lines=20).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)
    assert "o maximo e 20" in provider.calls[1]["user"]


def test_recusa_alteracao_que_nao_muda_nada():
    provider = FakeProvider([_proposta(substituir="    return 1.0")] * 3)
    with pytest.raises(AgentError):
        _coder(provider).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)
    assert "nao mudam nada" in provider.calls[1]["user"]


def test_corrige_se_com_o_erro_do_bloco():
    """O ciclo de correcao aplicado a codigo: bloco errado -> erro -> acerta."""
    provider = FakeProvider([
        _proposta(procurar="return 1.0 # comentario que nao existe"),
        _proposta(),
    ])
    saida = _coder(provider, max_retries=3).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)
    assert saida["ficheiro"] == "estrategia/sinal.py"
    assert saida["conteudo_novo"] == "def forca():\n    return 2.0\n"
    assert "nao aparece" in provider.calls[1]["user"]


def test_proposta_valida_devolve_conteudo_e_tamanho():
    saida = _coder(FakeProvider([_proposta()])).run(hipotese=HIPOTESE, ficheiros=FICHEIROS)
    assert saida["linhas_tocadas"] == 2
    assert saida["justificacao"] == "duplica a forca do sinal"


# --- sandbox --------------------------------------------------------------

def test_sandbox_le_so_os_ficheiros_editaveis(config_code):
    with Sandbox(config_code.target, config_code.storage.worktrees_dir, "exp_le") as sb:
        ficheiros = sb.read_editable(config_code.target.editable_paths)
    assert "estrategia/sinal.py" in ficheiros
    assert "run_backtest.py" not in ficheiros, "o arnes nunca e mostrado ao agente"
    assert "params.json" not in ficheiros


def test_sandbox_revalida_a_lista_branca(config_code):
    """Defesa em profundidade: a proposta passou por SQLite pelo meio."""
    with Sandbox(config_code.target, config_code.storage.worktrees_dir, "exp_rev") as sb:
        ok, detalhe = sb.apply_edits(
            "run_backtest.py",
            [{"procurar": "import argparse", "substituir": "import argparse  # tocado"}],
            config_code.target.editable_paths,
        )
    assert not ok
    assert "intocaveis" in detalhe


def test_testes_do_alvo_correm(config_code):
    with Sandbox(config_code.target, config_code.storage.worktrees_dir, "exp_t") as sb:
        resultado = sb.run_tests()
    assert resultado is not None
    assert resultado.ok, resultado.stdout


# --- ciclo completo em modo code -----------------------------------------

@pytest.fixture
def sistema_code(config_code, store_code):
    pesquisa = json.dumps({"hipoteses": [
        {"nome": "Dobrar a forca do sinal", "raciocinio": "o filtro corta demais",
         "parametros_alvo": ["sma_fast"], "direcao": "aumentar"},
    ]})
    provider = FakeProvider([pesquisa, _proposta(), *["Resultado consistente."] * 4])
    notifier = NullNotifier()
    return Orchestrator(config_code, store_code, provider, notifier), store_code, notifier, config_code


def _correr(worker, limite=20):
    n = 0
    while worker.step() and n < limite:
        n += 1


def test_ciclo_completo_em_modo_code(sistema_code):
    orq, store, notifier, config = sistema_code
    worker = Worker(orq, store, idle_sleep=0)

    store.enqueue_task(42, "aumentar o retorno mexendo no sinal")
    _correr(worker)

    ensaios = store.list_experiments(store.open_study()["id"], limit=10)
    assert len(ensaios) == 1
    ensaio = ensaios[0]
    assert ensaio["status"] == "done", ensaio["error"]

    alteracao = json.loads(ensaio["diff"])
    assert alteracao["ficheiro"] == "estrategia/sinal.py"

    # A alteracao tem de ter tido efeito real no backtest.
    metricas = json.loads(ensaio["metrics"])
    assert metricas["validation"]["sharpe_anualizado"] > 1.0

    # E o projeto original continua intacto.
    original = (config.target.path / "estrategia" / "sinal.py").read_text()
    assert "return 1.0" in original


def test_alteracao_aparece_na_mensagem_de_aprovacao(sistema_code):
    orq, store, notifier, config = sistema_code
    worker = Worker(orq, store, idle_sleep=0)
    store.enqueue_task(42, "melhorar")
    _correr(worker)

    assert notifier.approvals, "a proposta devia ter chegado para aprovacao"
    _, mensagem = notifier.approvals[0]
    assert "estrategia/sinal.py" in mensagem
    assert "- " in mensagem and "+ " in mensagem, "tens de ver o que muda"


def test_aprovacao_commita_o_codigo_num_ramo(sistema_code):
    orq, store, notifier, config = sistema_code
    worker = Worker(orq, store, idle_sleep=0)
    store.enqueue_task(42, "melhorar")
    _correr(worker)

    exp_id = next(exp for exp, _ in notifier.approvals)
    store.set_approval(exp_id, "approved")
    ramo = orq.apply_approved(exp_id)

    no_ramo = subprocess.run(
        ["git", "-C", str(config.target.path), "show", f"{ramo}:estrategia/sinal.py"],
        capture_output=True, text=True, check=True,
    ).stdout
    assert "return 2.0" in no_ramo
    # Sem merge: o ficheiro vivo nao mudou.
    assert "return 1.0" in (config.target.path / "estrategia" / "sinal.py").read_text()


def test_testes_do_alvo_a_falhar_travam_antes_do_backtest(config_code, store_code):
    """Codigo partido tem de morrer em segundos, nao ao fim do backtest."""
    pesquisa = json.dumps({"hipoteses": [
        {"nome": "Alteracao que parte tudo", "raciocinio": "x",
         "parametros_alvo": ["sma_fast"], "direcao": "aumentar"},
    ]})
    quebra = json.dumps({
        "ficheiro": "estrategia/sinal.py",
        "edicoes": [{"procurar": "    return 1.0", "substituir": "    return -1.0"}],
    })
    orq = Orchestrator(config_code, store_code, FakeProvider([pesquisa, quebra]), NullNotifier())
    worker = Worker(orq, store_code, idle_sleep=0)

    store_code.enqueue_task(42, "mexer")
    _correr(worker)

    ensaio = store_code.list_experiments(store_code.open_study()["id"], limit=5)[0]
    assert ensaio["status"] == "failed"
    assert "testes do projeto falharam" in ensaio["error"]


def test_falha_do_agente_de_codigo_nao_enfileira_nada(config_code, store_code):
    """Sem rede de seguranca: nao ha como 'amostrar' uma alteracao de codigo."""
    pesquisa = json.dumps({"hipoteses": [
        {"nome": "h", "raciocinio": "r", "parametros_alvo": ["sma_fast"], "direcao": "explorar"},
    ]})
    orq = Orchestrator(config_code, store_code, FakeProvider([pesquisa] + ["lixo"] * 10), NullNotifier())
    worker = Worker(orq, store_code, idle_sleep=0)

    store_code.enqueue_task(42, "mexer")
    _correr(worker)

    assert store_code.list_experiments(store_code.open_study()["id"], limit=5) == []
    assert "coder.failed" in [e["kind"] for e in store_code.recent_events(50)]
