import json
import time

from orq.store import Store


def test_fila_atomica(store):
    sid = store.create_study("s", "objetivo")
    a = store.enqueue_experiment(sid, {"x": 1})
    b = store.enqueue_experiment(sid, {"x": 2})

    primeiro = store.claim_experiment()
    segundo = store.claim_experiment()
    terceiro = store.claim_experiment()

    assert {primeiro["id"], segundo["id"]} == {a, b}
    assert terceiro is None, "nenhum ensaio pode ser reclamado duas vezes"


def test_ordem_fifo(store):
    sid = store.create_study("s", "o")
    ids = [store.enqueue_experiment(sid, {"x": i}) for i in range(3)]
    assert [store.claim_experiment()["id"] for _ in range(3)] == ids


def test_contagem_de_ensaios_so_conta_concluidos(store):
    sid = store.create_study("s", "o")
    a = store.enqueue_experiment(sid, {"x": 1})
    store.enqueue_experiment(sid, {"x": 2})
    assert store.trial_count(sid) == 0
    store.finish_experiment(a, status="done")
    assert store.trial_count(sid) == 1


def test_recuperacao_apos_crash(store):
    """Um worker morto a meio nao pode deixar a fila presa em silencio."""
    sid = store.create_study("s", "o")
    exp_id = store.enqueue_experiment(sid, {"x": 1})
    store.claim_experiment()
    assert store.get_experiment(exp_id)["status"] == "running"

    # heartbeat antigo = worker morreu
    store._conn.execute(
        "UPDATE experiments SET heartbeat=? WHERE id=?", (time.time() - 9999, exp_id)
    )
    recuperados = store.recover_stale(stale_after_sec=60)

    assert recuperados["experiments"] == 1
    assert store.get_experiment(exp_id)["status"] == "queued"
    assert store.claim_experiment()["id"] == exp_id


def test_recuperacao_nao_mexe_em_trabalho_vivo(store):
    sid = store.create_study("s", "o")
    exp_id = store.enqueue_experiment(sid, {"x": 1})
    store.claim_experiment()
    store.heartbeat_experiment(exp_id)
    assert store.recover_stale(stale_after_sec=3600)["experiments"] == 0
    assert store.get_experiment(exp_id)["status"] == "running"


def test_estado_sobrevive_a_reabertura(config, store):
    """O que interessa e que nada vive so em memoria."""
    sid = store.create_study("s", "o")
    exp_id = store.enqueue_experiment(sid, {"x": 1}, "hipotese")
    store.finish_experiment(exp_id, status="done", approval="pending",
                            metrics={"validation": {"sharpe_anualizado": 1.2}})
    store.close()

    with Store(config.storage.db_path) as novo:
        assert novo.open_study()["id"] == sid
        assert [e["id"] for e in novo.pending_approvals()] == [exp_id]
        assert json.loads(novo.get_experiment(exp_id)["metrics"])["validation"]["sharpe_anualizado"] == 1.2


def test_estudo_fechado_deixa_de_ser_o_aberto(store):
    a = store.create_study("a", "o1")
    store.close_study(a, "orcamento")
    assert store.open_study() is None
    b = store.create_study("b", "o2")
    assert store.open_study()["id"] == b


def test_offset_do_telegram_persiste(config, store):
    store.kv_set("telegram_offset", "12345")
    store.close()
    with Store(config.storage.db_path) as novo:
        assert novo.kv_get("telegram_offset") == "12345"
        assert novo.kv_get("inexistente", "omissao") == "omissao"


def test_cancelar_fila_nao_afeta_o_que_esta_a_correr(store):
    a = store.enqueue_task(1, "tarefa a")
    store.enqueue_task(1, "tarefa b")
    store.claim_task()  # 'a' passa a running
    assert store.cancel_queued_tasks() == 1
    assert store.get_task(a)["status"] == "running"


# --- threads --------------------------------------------------------------
#
# Estes testes existem porque a suite passava toda com um bug que rebentava
# assim que o worker arrancava a serio: o Store era aberto na thread principal
# e usado na thread do worker. Os testes chamavam `step()` diretamente, na
# mesma thread, e nunca tocaram no caminho real.

def test_ligacao_nao_atravessa_threads(config, store):
    """Documenta a regra que o bug violava."""
    import sqlite3
    import threading

    erro = []

    def usar_noutra_thread():
        try:
            store.create_study("x", "y")
        except sqlite3.ProgrammingError as exc:
            erro.append(str(exc))

    t = threading.Thread(target=usar_noutra_thread)
    t.start()
    t.join(timeout=10)
    assert erro, "o SQLite devia ter recusado a ligacao vinda de outra thread"
    assert "thread" in erro[0].lower()


def test_store_aberto_dentro_da_thread_funciona(config):
    """A forma correta: cada thread abre a sua ligacao."""
    import threading

    resultados = {}

    def trabalhar():
        with Store(config.storage.db_path) as proprio:
            sid = proprio.create_study("estudo na thread", "objetivo")
            exp = proprio.enqueue_experiment(sid, {"x": 1})
            proprio.finish_experiment(exp, status="done")
            resultados["trials"] = proprio.trial_count(sid)

    t = threading.Thread(target=trabalhar)
    t.start()
    t.join(timeout=10)
    assert not t.is_alive(), "a thread ficou pendurada"
    assert resultados.get("trials") == 1

    # E o que ela gravou tem de ser visivel da thread principal.
    with Store(config.storage.db_path) as principal:
        assert principal.open_study()["name"] == "estudo na thread"


def test_duas_ligacoes_em_simultaneo(config):
    """WAL: uma thread escreve enquanto a outra le, sem bloquear."""
    import threading

    with Store(config.storage.db_path) as principal:
        sid = principal.create_study("s", "o")

    barreira = threading.Barrier(2, timeout=10)
    erros = []

    def escrever():
        try:
            with Store(config.storage.db_path) as s:
                barreira.wait()
                for i in range(20):
                    s.enqueue_experiment(sid, {"i": i})
        except Exception as exc:  # noqa: BLE001
            erros.append(f"escritor: {exc}")

    def ler():
        try:
            with Store(config.storage.db_path) as s:
                barreira.wait()
                for _ in range(20):
                    s.list_experiments(sid, limit=5)
        except Exception as exc:  # noqa: BLE001
            erros.append(f"leitor: {exc}")

    ts = [threading.Thread(target=escrever), threading.Thread(target=ler)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=15)
    assert not erros, erros
    with Store(config.storage.db_path) as s:
        assert len(s.list_experiments(sid, limit=50)) == 20


def test_guarda_de_thread_falha_com_mensagem_util(config, store):
    """A mensagem do sqlite3 aparece a meio de uma transacao e nao diz o que fazer."""
    import threading

    erro = []

    def noutra_thread():
        try:
            store.assert_same_thread("o worker")
        except RuntimeError as exc:
            erro.append(str(exc))

    t = threading.Thread(target=noutra_thread)
    t.start()
    t.join(timeout=10)
    assert erro
    assert "o worker" in erro[0]
    assert "DENTRO da funcao" in erro[0]
    store.assert_same_thread()   # na propria thread, nao levanta
