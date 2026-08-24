"""Execucao isolada de um ensaio.

Tres garantias, por esta ordem de importancia:

1. O codigo que corre a serio nunca e tocado. Cada ensaio vive num `git
   worktree` proprio, descartavel. Se o agente estragar alguma coisa, estraga
   uma copia.
2. O subprocesso nao ve os segredos do orquestrador. O ambiente e construido
   de raiz, nao herdado — o TELEGRAM_BOT_TOKEN nao passa para o backtest.
3. Sem rede por defeito. Um backtest que precisa de internet a meio nao e
   reproduzivel, e um agente com rede e uma superficie que nao quero.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .config import TargetConfig
from .patching import (
    Edit, PatchError, apply_edits, check_frozen_functions, ensure_path_allowed,
    path_allowed,
)

STDOUT_HEAD = 2000
STDOUT_TAIL = 4000


class SandboxError(Exception):
    """Falha a preparar ou limpar o ambiente isolado."""


class HoldoutViolation(Exception):
    """Alguem tentou correr um ensaio automatico sobre o holdout.

    Isto e sempre um bug, nunca uma condicao normal. O holdout so vale enquanto
    for visto uma unica vez, no fim, por decisao humana.
    """


@dataclass(frozen=True)
class RunResult:
    ok: bool
    returncode: int
    stdout: str
    duration_sec: float
    timed_out: bool = False

    @property
    def summary(self) -> str:
        if self.timed_out:
            return f"timeout ao fim de {self.duration_sec:.0f}s"
        return f"saiu com codigo {self.returncode} em {self.duration_sec:.0f}s"


def truncate_output(text: str, head: int = STDOUT_HEAD, tail: int = STDOUT_TAIL) -> str:
    """Guarda o inicio e o fim. O meio de um log de backtest raramente interessa;
    o traceback esta no fim e o arranque no inicio."""
    if len(text) <= head + tail:
        return text
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted} caracteres omitidos ...]\n\n{text[-tail:]}"


@lru_cache(maxsize=1)
def _network_isolation_prefix() -> tuple[str, ...]:
    """Prefixo que corre o comando sem rede, se o sistema deixar.

    `unshare -rn` cria um namespace de rede vazio sem precisar de root. Nem
    todos os kernels/containers permitem; quando nao permite devolve vazio e
    quem chama decide se avisa.
    """
    for candidate in (("unshare", "-rn"), ("unshare", "-n")):
        if shutil.which(candidate[0]) is None:
            continue
        try:
            probe = subprocess.run(
                [*candidate, "true"], capture_output=True, timeout=10, check=False
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return ()


def network_isolation_available() -> bool:
    return bool(_network_isolation_prefix())


# Lista de permissao, nao de proibicao: o que nao esta aqui nao passa, e e assim
# que o token do Telegram nao chega ao backtest.
#
# As do Windows nao sao opcionais. Sem SystemRoot o Python nem arranca: a
# inicializacao da aleatoriedade das hashes pede numeros ao sistema, nao encontra
# a pasta do sistema, e morre com "_Py_HashRandomization_Init: failed to get
# random numbers" antes de chegar ao codigo do utilizador.
SYSTEM_VARS_COMMON = ("PATH", "LANG", "LC_ALL", "TZ", "HOME")
SYSTEM_VARS_WINDOWS = (
    "SystemRoot", "SystemDrive", "WINDIR", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "ProgramData",
    "ProgramFiles", "ProgramFiles(x86)", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "OS", "USERNAME", "HOMEDRIVE", "HOMEPATH",
)


def _clean_env(extra: dict[str, str] | None = None,
               passthrough: tuple[str, ...] = ()) -> dict[str, str]:
    """Ambiente construido a partir de uma lista de permissao.

    Nao herda `os.environ` de proposito: o processo do orquestrador tem o token
    do Telegram carregado, e o backtest nao tem nada que ver com isso. Mas
    "limpo" nao pode significar "vazio" — ha variaveis sem as quais o
    interpretador nao arranca.
    """
    names = list(SYSTEM_VARS_COMMON)
    if os.name == "nt":
        names += list(SYSTEM_VARS_WINDOWS)
    names += list(passthrough)

    env = {name: os.environ[name] for name in names if name in os.environ}
    env.setdefault("PATH", os.defpath)
    if os.name != "nt":
        env.setdefault("HOME", "/tmp")
        env.setdefault("LANG", "C.UTF-8")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def quote_path(path: str) -> str:
    """Cita um caminho para entrar num comando, conforme o sistema.

    `shlex.quote` usa aspas simples, que no Windows nao sao aspas — sao
    caracteres normais que iriam parar ao nome do ficheiro.
    """
    if os.name == "nt":
        return f'"{path}"' if (" " in path or not path) else path
    return shlex.quote(path)


def split_command(command: str) -> list[str]:
    """Parte um comando em argumentos, conforme o sistema.

    No modo POSIX o `shlex` trata `\\` como escape, e portanto
    `C:\\Python310\\python.exe` chega ao subprocesso como
    `C:Python310python.exe` — um caminho inexistente, com um erro que nao
    explica nada. No Windows uso o modo nao-POSIX, que preserva as barras.
    """
    if os.name == "nt":
        return [t[1:-1] if len(t) > 1 and t[0] == t[-1] == '"' else t
                for t in shlex.split(command, posix=False)]
    return shlex.split(command)


def link_into(source: Path, destination: Path) -> None:
    """Liga um ficheiro ou pasta para dentro do worktree, sem copiar.

    No Windows, criar um symlink exige Modo Programador ou privilegios de
    administrador — a maioria das pessoas nao tem nenhum. Para pastas ha
    alternativa: uma junction (`mklink /J`), que nao precisa de privilegios e
    serve o mesmo proposito aqui.
    """
    try:
        destination.symlink_to(source, target_is_directory=source.is_dir())
        return
    except OSError as exc:
        if os.name != "nt":
            raise SandboxError(f"nao consegui ligar {source} para {destination}: {exc}") from exc

    if source.is_dir():
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(destination), str(source)],
            capture_output=True, text=True, check=False, timeout=60,
        )
        if result.returncode == 0:
            return
    raise SandboxError(
        f"nao consegui ligar {source} para dentro do worktree.\n"
        "No Windows resolve-se de uma destas formas:\n"
        "  1. Definicoes -> Privacidade e seguranca -> Para programadores -> "
        "ligar o Modo Programador\n"
        "  2. tirar essa pasta de target.link_paths e usar no backtest o caminho "
        "absoluto dos dados"
    )


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=check,
        timeout=120,
    )


def git_root(path: Path) -> Path | None:
    """A raiz do repositorio a que este caminho pertence, ou None."""
    try:
        result = _git(path, "rev-parse", "--show-toplevel", check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    saida = result.stdout.strip()
    return Path(saida).resolve() if result.returncode == 0 and saida else None


def is_git_repo(path: Path) -> bool:
    """O caminho e a RAIZ de um repositorio git?

    Pertencer a um nao chega. Se o projeto-alvo estiver dentro de outro
    repositorio, `git worktree add` cria a arvore do repositorio de fora e o
    backtest procura os ficheiros um nivel acima de onde eles estao. O erro que
    dai sai nao aponta para a causa.
    """
    root = git_root(path)
    return root is not None and root == Path(path).resolve()


def head_sha(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


class Sandbox:
    """Um worktree descartavel para um ensaio. Usar como context manager."""

    def __init__(
        self,
        target: TargetConfig,
        worktrees_dir: Path,
        exp_id: str,
        *,
        base_ref: str = "HEAD",
    ):
        self.target = target
        self.exp_id = exp_id
        self.base_ref = base_ref
        self.root = Path(worktrees_dir) / exp_id
        self.created = False
        self.base_sha: str | None = None

    # -- ciclo de vida ---------------------------------------------------
    def __enter__(self) -> "Sandbox":
        self.create()
        return self

    def __exit__(self, *exc: object) -> None:
        self.cleanup()

    def create(self) -> Path:
        target = self.target.path
        if not target.is_dir():
            raise SandboxError(f"target.path nao existe: {target}")
        if not is_git_repo(target):
            externo = git_root(target)
            if externo is not None:
                raise SandboxError(
                    f"{target} esta dentro do repositorio {externo}, mas nao e a raiz "
                    f"dele. Precisa de ser um repositorio proprio:\n"
                    f'    cd {target} && git init && git add -A && git commit -m "inicial"'
                )
            raise SandboxError(
                f"{target} nao e um repositorio git. Faz `git init` e um commit inicial: "
                "sem versionamento nao ha como reverter uma alteracao automatica, "
                "e este sistema recusa-se a trabalhar assim."
            )
        self.base_sha = head_sha(target)
        self.root.parent.mkdir(parents=True, exist_ok=True)
        if self.root.exists():
            self.cleanup()
        try:
            _git(target, "worktree", "add", "--detach", str(self.root), self.base_ref)
        except subprocess.CalledProcessError as exc:
            raise SandboxError(f"git worktree add falhou: {exc.stderr.strip()}") from exc
        self.created = True
        self._link_extra_paths()
        return self.root

    def _link_extra_paths(self) -> None:
        """Liga dados nao versionados (series historicas, cache) para dentro do worktree.

        Symlink e nao copia: um worktree por ensaio a copiar 4 GB de candles
        enche o disco ao decimo ensaio.
        """
        for rel in self.target.link_paths:
            source = (self.target.path / rel).resolve()
            if not source.exists():
                continue
            destination = self.root / rel
            if not destination.exists() and not destination.is_symlink():
                destination.parent.mkdir(parents=True, exist_ok=True)
                link_into(source, destination)
            elif destination.is_dir() and source.is_dir():
                # A pasta ja existe no worktree — o caso comum, versionada com um
                # .gitkeep e o conteudo no .gitignore. Ligar a pasta inteira e
                # impossivel, portanto ligo o conteudo. Sem isto a pasta chega
                # vazia ao backtest e ele nao encontra os dados.
                for child in source.iterdir():
                    target = destination / child.name
                    if not target.exists() and not target.is_symlink():
                        link_into(child, target)

    def cleanup(self) -> None:
        if not self.root.exists() and not self.created:
            return
        try:
            _git(self.target.path, "worktree", "remove", "--force", str(self.root), check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        if self.root.exists():
            shutil.rmtree(self.root, ignore_errors=True)
        try:
            _git(self.target.path, "worktree", "prune", check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        self.created = False

    # -- execucao --------------------------------------------------------
    def run(self, command: str, timeout: int | None = None) -> RunResult:
        """Corre um comando dentro do worktree.

        O comando e dividido com shlex, sem shell: operadores como `&&` ou `|`
        nao funcionam de proposito. Se precisares deles, poe-os num script e
        chama o script.
        """
        if not self.created:
            raise SandboxError("sandbox nao foi criado; usa `with Sandbox(...)`")
        argv = split_command(command)
        if not argv:
            raise SandboxError("comando vazio")
        prefix: tuple[str, ...] = ()
        if not self.target.network:
            prefix = _network_isolation_prefix()
        timeout = timeout or self.target.timeout_sec

        started = time.monotonic()
        try:
            proc = subprocess.run(
                [*prefix, *argv],
                cwd=self.root,
                env=_clean_env(passthrough=self.target.env_passthrough),
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            partial = (exc.stdout or b"")
            if isinstance(partial, bytes):
                partial = partial.decode("utf-8", "replace")
            return RunResult(
                ok=False,
                returncode=-1,
                stdout=truncate_output(partial),
                duration_sec=elapsed,
                timed_out=True,
            )
        except FileNotFoundError as exc:
            raise SandboxError(f"comando nao encontrado: {argv[0]} ({exc})") from exc

        elapsed = time.monotonic() - started
        combined = proc.stdout + ("\n[stderr]\n" + proc.stderr if proc.stderr else "")
        return RunResult(
            ok=proc.returncode == 0,
            returncode=proc.returncode,
            stdout=truncate_output(combined),
            duration_sec=elapsed,
        )

    def run_backtest(
        self,
        params: dict,
        start: str,
        end: str,
        *,
        holdout_start: str,
        allow_holdout: bool = False,
    ) -> tuple[dict | None, RunResult]:
        """Escreve os parametros, corre o backtest e le o JSON de metricas.

        `holdout_start` e obrigatorio e a janela e verificada aqui, no ponto
        mais estreito por onde todo o ensaio passa. Um ensaio automatico que
        tocasse no holdout queimava-o em silencio, e depois nao havia forma de
        saber se o resultado final significava alguma coisa.
        """
        if not allow_holdout and end >= holdout_start:
            raise HoldoutViolation(
                f"ensaio tentou correr ate {end}, dentro do holdout que comeca em "
                f"{holdout_start}. Ensaios automaticos param em {holdout_start}."
            )

        io_dir = self.root / ".orq"
        io_dir.mkdir(exist_ok=True)
        params_file = io_dir / "params.json"
        metrics_file = io_dir / "metrics.json"
        params_file.write_text(
            json.dumps(params, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        if metrics_file.exists():
            metrics_file.unlink()

        command = self.target.backtest_cmd.format(
            params_file=quote_path(str(params_file)),
            metrics_file=quote_path(str(metrics_file)),
            start=start,
            end=end,
            workdir=quote_path(str(self.root)),
        )
        result = self.run(command)
        if not result.ok:
            return None, result
        if not metrics_file.is_file():
            return None, RunResult(
                ok=False,
                returncode=result.returncode,
                stdout=truncate_output(
                    result.stdout
                    + f"\n\n[orq] o backtest terminou bem mas nao escreveu {metrics_file.name}. "
                    "O teu script tem de gravar o JSON de metricas no caminho que recebe "
                    "no placeholder {metrics_file} do backtest_cmd."
                ),
                duration_sec=result.duration_sec,
            )
        try:
            metrics = json.loads(metrics_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return None, RunResult(
                ok=False,
                returncode=result.returncode,
                stdout=truncate_output(result.stdout + f"\n\n[orq] metrics.json invalido: {exc}"),
                duration_sec=result.duration_sec,
            )
        return metrics, result

    def tracked_files(self) -> list[str]:
        """Ficheiros versionados, caminhos relativos. Base para a lista branca."""
        saida = _git(self.root, "ls-files", check=False)
        if saida.returncode != 0:
            return []
        return [linha for linha in saida.stdout.splitlines() if linha]

    def read_visible(self, limit_bytes: int = 200_000) -> dict[str, str]:
        """Tudo o que o agente pode LER — por omissao, o projeto inteiro.

        Ver o codigo que mede nao lhe da poder nenhum sobre ele: a lista de
        edicao e verificada em separado, duas vezes. O que ver lhe da e
        contexto — sem ele, escreve codigo que nao encaixa na interface que o
        resto do sistema espera.

        Os editaveis vem primeiro, para serem os ultimos a cair se o contexto
        acabar.
        """
        tracked = [r for r in self.tracked_files()
                   if path_allowed(r, self.target.visible_paths)]
        tracked.sort(key=lambda r: (not path_allowed(r, self.target.editable_paths), r))

        out, spent = {}, 0
        for rel in tracked[: self.target.max_visible_files]:
            caminho = self.root / rel
            try:
                if caminho.stat().st_size > 120_000:
                    continue
                texto = caminho.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if spent + len(texto) > limit_bytes:
                continue
            spent += len(texto)
            out[rel] = texto
        return out

    def read_editable(self, patterns: tuple[str, ...]) -> dict[str, str]:
        """Le os ficheiros que o agente de desenvolvimento pode alterar.

        Le de dentro do worktree e nao do projeto original: e sobre esta copia
        que ele vai trabalhar, e sao estes os bytes que os blocos
        procurar/substituir tem de encontrar.
        """
        ficheiros: dict[str, str] = {}
        for rel in self.tracked_files():
            if not path_allowed(rel, patterns):
                continue
            caminho = self.root / rel
            try:
                ficheiros[rel] = caminho.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binarios e afins nao interessam ao agente
        return ficheiros

    def apply_edits(
        self, ficheiro: str, edicoes: list[dict], patterns: tuple[str, ...]
    ) -> tuple[bool, str]:
        """Aplica as edicoes propostas, revalidando a lista branca.

        A lista branca ja foi verificada quando a proposta foi aceite. E
        verificada outra vez aqui de proposito: entre uma coisa e outra a
        proposta passou por SQLite, e o custo de reverificar e nenhum comparado
        com o de deixar passar uma edicao ao arnes de metricas.
        """
        try:
            ensure_path_allowed(ficheiro, patterns)
            blocos = [Edit(e["procurar"], e["substituir"]) for e in edicoes]
            alvo = self.root / ficheiro
            if not alvo.is_file():
                return False, f"`{ficheiro}` nao existe no worktree"
            antes = alvo.read_text(encoding="utf-8")
            novo = apply_edits(antes, blocos)
        except (PatchError, KeyError, TypeError) as exc:
            return False, str(exc)

        # A segunda guarda, para quando estrategia e metricas partilham ficheiro.
        queixa = check_frozen_functions(antes, novo, self.target.frozen_functions)
        if queixa:
            return False, queixa

        alvo.write_text(novo, encoding="utf-8")
        return True, f"{ficheiro} alterado"

    def run_tests(self) -> RunResult | None:
        """Corre os testes do projeto-alvo, se estiverem configurados.

        Corre depois de aplicar a alteracao e antes do backtest: um erro de
        sintaxe descoberto em dois segundos vale mais do que o mesmo erro
        descoberto ao fim de quarenta minutos de backtest.
        """
        if not self.target.test_cmd:
            return None
        return self.run(self.target.test_cmd, timeout=min(self.target.timeout_sec, 600))

    def apply_diff(self, diff: str) -> tuple[bool, str]:
        """Tenta aplicar um patch proposto pelo LLM. Valida antes de aplicar.

        `git apply --check` corre primeiro: um patch mal formado — o erro mais
        comum de um modelo pequeno a escrever diffs — e rejeitado sem deixar o
        worktree a meio.
        """
        patch_file = self.root / ".orq" / "proposta.patch"
        patch_file.parent.mkdir(exist_ok=True)
        patch_file.write_text(diff if diff.endswith("\n") else diff + "\n", encoding="utf-8")
        check = _git(self.root, "apply", "--check", str(patch_file), check=False)
        if check.returncode != 0:
            return False, f"patch invalido: {check.stderr.strip()}"
        applied = _git(self.root, "apply", str(patch_file), check=False)
        if applied.returncode != 0:
            return False, f"patch falhou ao aplicar: {applied.stderr.strip()}"
        return True, "patch aplicado"
