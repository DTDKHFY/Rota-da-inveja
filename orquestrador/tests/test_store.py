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
