"""O orquestrador: recebe uma tarefa tua, delega, mede, e pergunta.

O fluxo de uma tarefa vinda do Telegram:

    tarefa -> Agente Pesquisa (hipoteses)
           -> Agente Proponente (valores concretos, por hipotese)
           -> fila de ensaios (SQLite, sobrevive a crash)
           -> sandbox: backtest em treino e em validacao
           -> gate deterministico
           -> se passar: pedido de aprovacao para ti
           -> se aprovares: ramo git novo no projeto-alvo, para tu fazeres merge

Duas coisas que este modulo NUNCA faz, e que sao o ponto todo:
  - correr o que quer que seja sobre o holdout;
  - escrever no ramo ativo do teu projeto.
"""
from __future__ import annotations

import json
import random
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .agents import (
    CodeAgent, CommentAgent, ProposerAgent, ResearchAgent, build_approval_message,
)
from .agents.base import AgentError
from .config import Config
from .gate import Verdict, evaluate
from .llm.base import LLMProvider
from .metrics import MetricsError, WindowMetrics, parse_window_metrics
from .sandbox import HoldoutViolation, Sandbox, SandboxError
from .store import Store


class Notifier(Protocol):
    def send(self, text: str) -> None: ...
    def ask_approval(self, text: str, exp_id: str) -> None: ...


class NullNotifier:
    """Guarda as mensagens em vez de as enviar. Para testes e para `--dry-run`."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.approvals: list[tuple[str, str]] = []

    def send(self, text: str) -> None:
        self.sent.append(text)

    def ask_approval(self, text: str, exp_id: str) -> None:
        self.approvals.append((exp_id, text))


@dataclass
class ExperimentOutcome:
    exp_id: str
    ok: bool
    verdict: Verdict | None = None
    error: str | None = None


class Orchestrator:
    def __init__(
        self,
        config: Config,
        store: Store,
        provider: LLMProvider,
        notifier: Notifier | None = None,
        *,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.store = store
        self.provider = provider
        self.notifier = notifier or NullNotifier()
        self.rng = rng or random.Random()

        llm = config.llm
        self.research = ResearchAgent(
            provider, llm.model_for("research"), max_retries=llm.max_json_retries, store=store
        )
        self.proposer = ProposerAgent(
            provider,
            llm.model_for("proposer"),
            config.experiment,
            max_retries=llm.max_json_retries,
            store=store,
        )
        self.commenter = CommentAgent(
            provider, llm.model_for("report"), max_retries=1, store=store
        )
        # O Agente de Desenvolvimento so existe no modo `code`. No modo `params`
        # nao ha codigo a escrever, e ter o agente instanciado seria convidar a
        # que fosse usado por engano.
        self.coder = (
            CodeAgent(
                provider,
                llm.model_for("coder"),
                editable_paths=config.target.editable_paths,
                max_edit_lines=config.experiment.max_edit_lines,
                max_retries=llm.max_json_retries,
                store=store,
            )
            if config.experiment.mode == "code"
            else None
        )

    # ------------------------------------------------------------------
    # Estudos
    # ------------------------------------------------------------------
    def ensure_study(self, objetivo: str) -> Any:
        """Devolve o estudo aberto, ou cria um novo com este objetivo.

        Um estudo e a unidade de contagem de trials. Abrir um estudo novo
        reinicia essa contagem — e por isso que abrir estudos as dezenas para
        "limpar" o DSR e enganar-se a si proprio, e o sistema avisa quando o
        limite e atingido em vez de deixar continuar.
        """
        study = self.store.open_study()
        if study is not None:
            return study
        study_id = self.store.create_study(name=objetivo[:60], goal=objetivo)
        self.notifier.send(
            f"📚 Estudo novo aberto: `{study_id}`\nObjetivo: {objetivo}\n"
            f"Orcamento: {self.config.protocol.max_trials_per_study} ensaios."
        )
        return self.store.get_study(study_id)

    def _historico(self, study_id: str, limite: int = 20) -> list[dict]:
        historico = []
        for row in reversed(self.store.list_experiments(study_id, limit=limite)):
            if row["status"] != "done":
                continue
            metrics = json.loads(row["metrics"]) if row["metrics"] else {}
            validation = metrics.get("validation") or {}
            historico.append(
                {
                    "params": json.loads(row["params"]),
                    "oos_sharpe": validation.get("sharpe_anualizado"),
                    "oos_drawdown": validation.get("max_drawdown"),
                }
            )
        return historico

    def _prior_sharpes(self, study_id: str) -> list[float]:
        return [
            h["oos_sharpe"]
            for h in self._historico(study_id, limite=500)
            if h["oos_sharpe"] is not None
        ]

    def _params_vivos(self) -> dict:
        """Os parametros em producao. No modo `code` ficam fixos: a hipotese
        esta no codigo, e mexer nas duas coisas ao mesmo tempo tornaria
        impossivel saber qual delas produziu a diferenca."""
        caminho = self.config.target.path / self.config.target.params_file
        if not caminho.is_file():
            return {}
        try:
            return json.loads(caminho.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _melhores_params(self, study_id: str) -> dict:
        melhor, melhor_sharpe = {}, float("-inf")
        for h in self._historico(study_id, limite=500):
            if h["oos_sharpe"] is not None and h["oos_sharpe"] > melhor_sharpe:
                melhor, melhor_sharpe = h["params"], h["oos_sharpe"]
        return melhor

    # ------------------------------------------------------------------
    # Tarefas
    # ------------------------------------------------------------------
    def handle_task(self, task: Any) -> int:
        """Transforma uma ordem tua em ensaios na fila. Devolve quantos enfileirou."""
        objetivo = task["text"]
        study = self.ensure_study(objetivo)
        study_id = study["id"]

        usados = self.store.trial_count(study_id)
        orcamento = self.config.protocol.max_trials_per_study
        if usados >= orcamento:
            self.store.close_study(study_id, "orcamento de ensaios esgotado")
            self.notifier.send(
                f"🛑 Estudo `{study_id}` fechado: {usados}/{orcamento} ensaios gastos.\n\n"
                "Isto nao e uma falha tecnica — e a trava de multiple testing. "
                "Ao fim de tantas tentativas, o melhor resultado ja e explicavel "
                "por acaso. Se queres continuar, abre um estudo novo com /estudo "
                "e comeca com uma hipotese nova, nao com a continuacao desta busca."
            )
            return 0

        historico = self._historico(study_id)
        try:
            hipoteses = self.research.run(
                objetivo=objetivo,
                schema=self.config.experiment.params_schema,
                historico=historico,
                n_hipoteses=3,
                task_id=task["id"],
            )
        except AgentError as exc:
            self.notifier.send(f"⚠️ O agente de pesquisa nao produziu hipoteses: {exc}")
            return 0

        if self.config.experiment.mode == "code":
            enfileirados, nomes = self._enfileirar_codigo(
                study_id, task, hipoteses, orcamento
            )
        else:
            enfileirados, nomes = self._enfileirar_params(
                study_id, task, hipoteses, historico, orcamento
            )

        if enfileirados == 0:
            self.notifier.send(
                f"⚠️ Nenhuma das {len(hipoteses)} hipoteses deu uma proposta "
                f"aplicavel. Nada foi para a fila."
            )
            return 0

        self.notifier.send(
            f"🧪 {enfileirados} ensaios em fila para: _{objetivo}_\n"
            + "\n".join(f"• {n}" for n in nomes)
        )
        return enfileirados

    def _enfileirar_params(
        self, study_id: str, task: Any, hipoteses: list[dict], historico: list[dict],
        orcamento: int,
    ) -> tuple[int, list[str]]:
        params_base = self._melhores_params(study_id)
        enfileirados, nomes = 0, []
        for hipotese in hipoteses:
            if self.store.trial_count(study_id) + enfileirados >= orcamento:
                break
            proposta = self.proposer.propose_with_fallback(
                hipotese=hipotese,
                params_atuais=params_base,
                historico=historico,
                rng=self.rng,
            )
            exp_id = self.store.enqueue_experiment(
                study_id=study_id,
                params=proposta["params"],
                hypothesis=f"{hipotese['nome']} — {hipotese['raciocinio']}",
                task_id=task["id"],
            )
            enfileirados += 1
            nomes.append(hipotese["nome"])
            if proposta.get("fallback"):
                self.store.log_event("proposer.fallback", exp_id)
        return enfileirados, nomes

    def _enfileirar_codigo(
        self, study_id: str, task: Any, hipoteses: list[dict], orcamento: int
    ) -> tuple[int, list[str]]:
        """Pede ao Agente de Desenvolvimento uma alteracao por hipotese.

        Os ficheiros sao lidos uma unica vez: todas as hipoteses partem do mesmo
        HEAD, e abrir um worktree por hipotese so para ler seria desperdicio.
        """
        params = self._params_vivos()
        enfileirados, nomes = 0, []

        with Sandbox(
            self.config.target, self.config.storage.worktrees_dir, f"{task['id']}_leitura"
        ) as sb:
            ficheiros = sb.read_editable(self.config.target.editable_paths)

        if not ficheiros:
            self.notifier.send(
                "⚠️ Nenhum ficheiro versionado corresponde a "
                f"target.editable_paths ({', '.join(self.config.target.editable_paths)}). "
                "O agente de desenvolvimento nao tem onde mexer."
            )
            return 0, []

        for hipotese in hipoteses:
            if self.store.trial_count(study_id) + enfileirados >= orcamento:
                break
            try:
                proposta = self.coder.run(
                    hipotese=hipotese, ficheiros=ficheiros, task_id=task["id"]
                )
            except AgentError as exc:
                # Sem rede de seguranca aqui, ao contrario dos parametros: nao
                # ha forma sensata de "amostrar" uma alteracao de codigo.
                self.store.log_event("coder.failed", None, hipotese=hipotese["nome"])
                self.notifier.send(f"⚠️ Nao consegui implementar _{hipotese['nome']}_: {exc}")
                continue

            exp_id = self.store.enqueue_experiment(
                study_id=study_id,
                params=params,
                hypothesis=f"{hipotese['nome']} — {hipotese['raciocinio']}",
                task_id=task["id"],
                diff=json.dumps(
                    {"ficheiro": proposta["ficheiro"], "edicoes": proposta["edicoes"]},
                    ensure_ascii=False,
                ),
            )
            enfileirados += 1
            nomes.append(f"{hipotese['nome']} ({proposta['linhas_tocadas']} linhas)")
        return enfileirados, nomes

    # ------------------------------------------------------------------
    # Ensaios
    # ------------------------------------------------------------------
    def run_experiment(self, exp: Any) -> ExperimentOutcome:
        exp_id = exp["id"]
        params = json.loads(exp["params"])
        protocol = self.config.protocol

        try:
            with Sandbox(self.config.target, self.config.storage.worktrees_dir, exp_id) as sb:
                self.store.heartbeat_experiment(exp_id)

                if exp["diff"]:
                    proposta = json.loads(exp["diff"])
                    aplicado, detalhe = sb.apply_edits(
                        proposta["ficheiro"],
                        proposta["edicoes"],
                        self.config.target.editable_paths,
                    )
                    if not aplicado:
                        return self._fail(exp_id, f"alteracao rejeitada: {detalhe}")

                    # Os testes do projeto correm antes do backtest: um erro de
                    # sintaxe apanhado em dois segundos poupa quarenta minutos.
                    testes = sb.run_tests()
                    if testes is not None and not testes.ok:
                        return self._fail(
                            exp_id,
                            "os testes do projeto falharam depois da alteracao",
                            testes.stdout,
                        )

                train_raw, run_train = sb.run_backtest(
                    params,
                    protocol.train_start,
                    protocol.train_end,
                    holdout_start=protocol.holdout_start,
                )
                if train_raw is None:
                    return self._fail(exp_id, "backtest de treino falhou", run_train.stdout)

                self.store.heartbeat_experiment(exp_id)
                val_raw, run_val = sb.run_backtest(
                    params,
                    protocol.validation_start,
                    protocol.validation_end,
                    holdout_start=protocol.holdout_start,
                )
                if val_raw is None:
                    return self._fail(exp_id, "backtest de validacao falhou", run_val.stdout)

        except HoldoutViolation as exc:
            # Bug nosso, nao condicao normal. Grita alto.
            self.notifier.send(f"🚨 VIOLACAO DE HOLDOUT em `{exp_id}`: {exc}")
            return self._fail(exp_id, f"violacao de holdout: {exc}")
        except SandboxError as exc:
            return self._fail(exp_id, f"sandbox: {exc}")

        try:
            train = parse_window_metrics(train_raw)
            validation = parse_window_metrics(val_raw)
        except MetricsError as exc:
            return self._fail(exp_id, f"metricas invalidas: {exc}")

        study_id = exp["study_id"]
        verdict = evaluate(
            train=train,
            validation=validation,
            config=self.config.gate,
            n_trials=self.store.trial_count(study_id) + 1,
            prior_sharpes=self._prior_sharpes(study_id),
            baseline=self._baseline(study_id),
        )

        self.store.finish_experiment(
            exp_id,
            status="done",
            metrics={
                "train": self._metrics_dict(train),
                "validation": self._metrics_dict(validation),
            },
            verdict=verdict.to_dict(),
            approval="pending" if verdict.passed else "none",
        )

        if verdict.passed:
            comentario = self.commenter.comment_or_none(
                verdict=verdict, hipotese=exp["hypothesis"] or "", experiment_id=exp_id
            )
            mensagem = build_approval_message(
                exp_id=exp_id,
                hipotese=exp["hypothesis"] or "",
                params=params,
                params_anteriores=self._melhores_params(study_id),
                train=train,
                validation=validation,
                verdict=verdict,
                comentario=comentario,
                alteracao=json.loads(exp["diff"]) if exp["diff"] else None,
            )
            self.notifier.ask_approval(mensagem, exp_id)
        else:
            falhas = ", ".join(c.name for c in verdict.failures)
            self.notifier.send(
                f"❌ `{exp_id}` chumbou ({falhas}) — "
                f"Sharpe OOS {validation.sharpe_annualised:.2f}, DSR {verdict.dsr:.3f}"
            )

        return ExperimentOutcome(exp_id=exp_id, ok=True, verdict=verdict)

    @staticmethod
    def _metrics_dict(m: WindowMetrics) -> dict:
        return {
            "sharpe_periodo": m.sharpe,
            "sharpe_anualizado": m.sharpe_annualised,
            "max_drawdown": m.max_drawdown,
            "trades": m.trades,
            "n_obs": m.n_obs,
            "skew": m.skew,
            "kurtosis": m.kurt,
            "total_return": m.total_return,
            "periods_per_year": m.periods_per_year,
        }

    def _baseline(self, study_id: str) -> WindowMetrics | None:
        study = self.store.get_study(study_id)
        if not study or not study["baseline"]:
            return None
        try:
            return parse_window_metrics(json.loads(study["baseline"]))
        except (MetricsError, json.JSONDecodeError):
            return None

    def _fail(self, exp_id: str, error: str, stdout: str | None = None) -> ExperimentOutcome:
        self.store.finish_experiment(exp_id, status="failed", error=error, stdout_tail=stdout)
        self.notifier.send(f"⚠️ `{exp_id}` falhou: {error}")
        return ExperimentOutcome(exp_id=exp_id, ok=False, error=error)

    # ------------------------------------------------------------------
    # Baseline e holdout
    # ------------------------------------------------------------------
    def measure_baseline(self, params: dict, study_id: str) -> WindowMetrics:
        """Mede a estrategia atual na janela de validacao, para servir de referencia."""
        protocol = self.config.protocol
        with Sandbox(self.config.target, self.config.storage.worktrees_dir, "baseline") as sb:
            raw, result = sb.run_backtest(
                params,
                protocol.validation_start,
                protocol.validation_end,
                holdout_start=protocol.holdout_start,
            )
        if raw is None:
            raise SandboxError(f"baseline falhou: {result.summary}\n{result.stdout[-800:]}")
        self.store.set_baseline(study_id, raw)
        return parse_window_metrics(raw)

    def run_holdout(self, exp_id: str) -> WindowMetrics:
        """Corre o holdout. So por ordem humana explicita, e so uma vez por ensaio.

        Cada vez que se corre o holdout, ele perde valor: passa a fazer parte do
        processo de selecao. Por isso e recusado se ja tiver sido corrido.
        """
        exp = self.store.get_experiment(exp_id)
        if exp is None:
            raise ValueError(f"ensaio {exp_id} nao existe")
        if exp["holdout"]:
            raise ValueError(
                f"o holdout de {exp_id} ja foi corrido uma vez. Correr outra vez nao "
                "te da informacao nova — da-te a ilusao de confirmacao. "
                "Se precisas de outra medicao independente, precisas de dados novos."
            )
        protocol = self.config.protocol
        params = json.loads(exp["params"])
        with Sandbox(self.config.target, self.config.storage.worktrees_dir, f"{exp_id}_holdout") as sb:
            raw, result = sb.run_backtest(
                params,
                protocol.holdout_start,
                protocol.holdout_end,
                holdout_start=protocol.holdout_start,
                allow_holdout=True,  # unico sitio no sistema onde isto e True
            )
        if raw is None:
            raise SandboxError(f"holdout falhou: {result.summary}\n{result.stdout[-800:]}")
        self.store.set_holdout(exp_id, raw)
        return parse_window_metrics(raw)

    # ------------------------------------------------------------------
    # Aplicar uma proposta aprovada
    # ------------------------------------------------------------------
    def apply_approved(self, exp_id: str) -> str:
        """Escreve os parametros aprovados num ramo git novo do projeto-alvo.

        Nao faz merge. Nao toca no ramo ativo. Devolve o nome do ramo para tu
        olhares para o diff e decidires — que e a unica pessoa que deve decidir.
        """
        exp = self.store.get_experiment(exp_id)
        if exp is None:
            raise ValueError(f"ensaio {exp_id} nao existe")
        if exp["approval"] != "approved":
            raise ValueError(f"ensaio {exp_id} nao esta aprovado (estado: {exp['approval']})")

        branch = f"orq/{exp_id}"
        params = json.loads(exp["params"])
        params_path = Path(self.config.target.params_file)

        with Sandbox(self.config.target, self.config.storage.worktrees_dir, f"{exp_id}_apply") as sb:
            if exp["diff"]:
                proposta = json.loads(exp["diff"])
                aplicado, detalhe = sb.apply_edits(
                    proposta["ficheiro"],
                    proposta["edicoes"],
                    self.config.target.editable_paths,
                )
                if not aplicado:
                    raise SandboxError(f"a alteracao ja nao aplica: {detalhe}")
                alvo_git = proposta["ficheiro"]
            else:
                destino = sb.root / params_path
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_text(
                    json.dumps(params, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                alvo_git = str(params_path)
            verdict = json.loads(exp["verdict"]) if exp["verdict"] else {}
            mensagem = (
                f"{'codigo' if exp['diff'] else 'params'}: proposta {exp_id}\n\n"
                f"{exp['hypothesis'] or 'sem hipotese registada'}\n\n"
                f"Sharpe OOS: {self._oos_sharpe(exp):.2f}\n"
                f"Deflated Sharpe: {verdict.get('dsr', 0):.3f} "
                f"apos {verdict.get('n_trials', 0)} ensaios\n"
                f"Holdout: NAO corrido\n"
            )
            for args in (
                ("checkout", "-b", branch),
                ("add", alvo_git),
                ("-c", "user.email=orq@local", "-c", "user.name=orquestrador",
                 "commit", "-m", mensagem),
            ):
                result = subprocess.run(
                    ["git", "-C", str(sb.root), *args],
                    capture_output=True, text=True, check=False, timeout=120,
                )
                if result.returncode != 0:
                    raise SandboxError(f"git {args[0]} falhou: {result.stderr.strip()}")

        # O worktree e descartado a saida do `with`, mas os ramos sao partilhados
        # entre worktrees: o ramo `branch` fica no repositorio a espera de ti.

        self.store.log_event("experiment.applied", exp_id, branch=branch)
        return branch

    @staticmethod
    def _oos_sharpe(exp: Any) -> float:
        if not exp["metrics"]:
            return 0.0
        return json.loads(exp["metrics"]).get("validation", {}).get("sharpe_anualizado", 0.0)
