import json
import os
import subprocess

import pytest

from orq.sandbox import (
    HoldoutViolation, Sandbox, SandboxError, is_git_repo, truncate_output,
)


def test_worktree_isolado_e_limpo(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_a") as sb:
        assert sb.root.is_dir()
        assert (sb.root / "run_backtest.py").is_file()
        (sb.root / "run_backtest.py").write_text("estragado", encoding="utf-8")
    assert not sb.root.exists(), "o worktree devia ter sido apagado"
    original = (config.target.path / "run_backtest.py").read_text(encoding="utf-8")
    assert "estragado" not in original, "o projeto-alvo foi modificado — isto nunca pode acontecer"


def test_dados_nao_versionados_ficam_acessiveis(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_b") as sb:
        assert (sb.root / "data").is_symlink()
        assert (sb.root / "data" / "candles.csv").is_file()


def test_liga_conteudo_quando_a_pasta_ja_existe(config, tmp_path):
    """O caso comum: pasta de dados versionada com .gitkeep, conteudo no gitignore.

    O worktree ja traz a pasta (por causa do .gitkeep), portanto nao da para
    ligar a pasta inteira. Se nao ligarmos o conteudo, o backtest recebe uma
    pasta vazia e nao encontra os dados.
    """
    (config.target.path / "data" / ".gitkeep").write_text("", encoding="utf-8")
    subprocess.run(["git", "-C", str(config.target.path), "add", "-f", "data/.gitkeep"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(config.target.path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "gitkeep"],
                   check=True, capture_output=True)

    with Sandbox(config.target, config.storage.worktrees_dir, "exp_link") as sb:
        assert (sb.root / "data").is_dir()
        assert (sb.root / "data" / "candles.csv").is_file(), \
            "os dados nao versionados tem de chegar ao worktree"


def test_recusa_alvo_sem_git(config, tmp_path):
    sem_git = tmp_path / "sem_git"
    sem_git.mkdir()
    alterado = type(config.target)(**{**config.target.__dict__, "path": sem_git})
    with pytest.raises(SandboxError, match="git init"):
        Sandbox(alterado, config.storage.worktrees_dir, "exp_c").create()


def test_recusa_projeto_dentro_de_outro_repo(config, tmp_path):
    """Pertencer a um repositorio nao chega: tem de ser a raiz.

    Se o projeto-alvo estiver numa subpasta de outro repositorio, o worktree
    sai com a arvore do repositorio de fora e o backtest procura os ficheiros
    um nivel acima de onde eles estao.
    """
    interior = config.target.path / "subprojeto"
    interior.mkdir()
    (interior / "x.py").write_text("", encoding="utf-8")
    alterado = type(config.target)(**{**config.target.__dict__, "path": interior})
    with pytest.raises(SandboxError, match="nao e a raiz"):
        Sandbox(alterado, config.storage.worktrees_dir, "exp_nested").create()


def test_backtest_produz_metricas(config):
    p = config.protocol
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_d") as sb:
        metrics, resultado = sb.run_backtest(
            {"sma_fast": 20}, p.train_start, p.train_end, holdout_start=p.holdout_start
        )
    assert resultado.ok, resultado.stdout
    assert metrics["trades"] == 420
    assert len(metrics["returns"]) == 600


def test_holdout_bloqueado_por_omissao(config):
    """A guarda mais importante do sistema."""
    p = config.protocol
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_e") as sb:
        with pytest.raises(HoldoutViolation, match="holdout"):
            sb.run_backtest(
                {"sma_fast": 20}, p.train_start, "2025-06-01", holdout_start=p.holdout_start
            )


def test_holdout_so_com_autorizacao_explicita(config):
    p = config.protocol
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_f") as sb:
        metrics, resultado = sb.run_backtest(
            {"sma_fast": 20}, p.holdout_start, p.holdout_end,
            holdout_start=p.holdout_start, allow_holdout=True,
        )
    assert resultado.ok
    assert metrics["janela"] == [p.holdout_start, p.holdout_end]


def test_segredos_nao_chegam_ao_subprocesso(config, monkeypatch):
    """O backtest nunca precisa do token do bot, portanto nunca o ve."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SEGREDO-QUE-NAO-PODE-VAZAR")
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_g") as sb:
        (sb.root / "espia.py").write_text(
            "import os; print(os.environ.get('TELEGRAM_BOT_TOKEN', 'AUSENTE'))",
            encoding="utf-8",
        )
        resultado = sb.run("python3 espia.py")
    assert "AUSENTE" in resultado.stdout
    assert "SEGREDO" not in resultado.stdout


def test_comando_que_falha(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_h") as sb:
        resultado = sb.run("python3 -c raise_erro")
    assert not resultado.ok
    assert resultado.returncode != 0


def test_timeout(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_i") as sb:
        resultado = sb.run("python3 -c import~time;time.sleep(30)".replace("~", " "), timeout=2)
    assert resultado.timed_out or not resultado.ok


def test_metricas_em_falta_dao_erro_explicativo(config):
    alterado = type(config.target)(
        **{**config.target.__dict__, "backtest_cmd": "python3 -c print(1)"}
    )
    p = config.protocol
    with Sandbox(alterado, config.storage.worktrees_dir, "exp_j") as sb:
        metrics, resultado = sb.run_backtest(
            {"sma_fast": 20}, p.train_start, p.train_end, holdout_start=p.holdout_start
        )
    assert metrics is None
    assert "metrics.json" in resultado.stdout


def test_diff_invalido_e_rejeitado_antes_de_aplicar(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_k") as sb:
        ok, detalhe = sb.apply_diff("isto nao e um patch\n")
        assert not ok
        assert "invalido" in detalhe
        assert (sb.root / "run_backtest.py").read_text(encoding="utf-8").startswith("import")


def test_diff_valido_aplica(config):
    with Sandbox(config.target, config.storage.worktrees_dir, "exp_l") as sb:
        patch = (
            "diff --git a/params.json b/params.json\n"
            "--- a/params.json\n"
            "+++ b/params.json\n"
            "@@ -1 +1 @@\n"
            '-{"sma_fast": 8, "sma_slow": 60, "stop_atr": 2.0}\n'
            '+{"sma_fast": 9, "sma_slow": 60, "stop_atr": 2.0}\n'
        )
        ok, detalhe = sb.apply_diff(patch)
        assert ok, detalhe
        assert json.loads((sb.root / "params.json").read_text())["sma_fast"] == 9


def test_truncagem_guarda_inicio_e_fim():
    texto = "A" * 100 + "MEIO" + "Z" * 100
    saida = truncate_output(texto, head=10, tail=10)
    assert saida.startswith("A" * 10)
    assert saida.endswith("Z" * 10)
    assert "omitidos" in saida
    assert truncate_output("curto", head=10, tail=10) == "curto"
