"""O contrato que a estrategia tem de cumprir.

Estes testes correm ANTES de cada backtest, por ordem do orquestrador. Uma
alteracao do agente que os parta morre em dois segundos, em vez de morrer ao
fim de quarenta minutos — ou, pior, de nao morrer e produzir um numero errado.

Usam so a biblioteca padrao: correm com `python3 -m unittest` sem instalar nada.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dados import Barra                                    # noqa: E402
from estrategia import gerar_sinais, tamanho_posicao       # noqa: E402
from estrategia.sinal import media_movel                   # noqa: E402

PARAMS = {"sma_fast": 5, "sma_slow": 15, "risco_por_trade": 0.01}


def barras_de(precos: list[float]) -> list[Barra]:
    return [Barra(data=f"2020-01-{i%28+1:02d}", abertura=p, maxima=p * 1.01,
                  minima=p * 0.99, fecho=p, volume=1000.0)
            for i, p in enumerate(precos)]


def serie_sintetica(n: int = 300) -> list[Barra]:
    import random
    rng = random.Random(11)
    precos, p = [], 100.0
    for _ in range(n):
        p *= 1 + rng.gauss(0.0003, 0.01)
        precos.append(p)
    return barras_de(precos)


class ContratoDaEstrategia(unittest.TestCase):
    def test_um_sinal_por_barra(self):
        barras = serie_sintetica(200)
        self.assertEqual(len(gerar_sinais(barras, PARAMS)), len(barras))

    def test_so_valores_permitidos(self):
        sinais = gerar_sinais(serie_sintetica(200), PARAMS)
        self.assertTrue(set(sinais) <= {-1, 0, 1}, f"valores invalidos: {set(sinais)}")

    def test_devolve_lista_de_inteiros(self):
        sinais = gerar_sinais(serie_sintetica(100), PARAMS)
        self.assertIsInstance(sinais, list)
        self.assertTrue(all(isinstance(s, int) for s in sinais))

    def test_sem_lookahead(self):
        """O teste que mais importa aqui.

        Se a estrategia so usa informacao ate a barra i, entao truncar a serie
        depois da barra k nao pode mudar nenhum sinal anterior a k. Se mudar, a
        estrategia esta a espreitar o futuro — e o backtest passa a produzir
        resultados maravilhosos e impossiveis.
        """
        barras = serie_sintetica(300)
        completo = gerar_sinais(barras, PARAMS)
        for k in (50, 120, 250):
            truncado = gerar_sinais(barras[:k], PARAMS)
            self.assertEqual(
                truncado, completo[:k],
                f"os sinais mudaram ao truncar em {k}: a estrategia esta a usar "
                f"dados do futuro")

    def test_aguenta_serie_curta(self):
        """Menos barras do que o periodo da media lenta nao pode rebentar."""
        for n in (0, 1, 3, 14):
            sinais = gerar_sinais(serie_sintetica(n) if n else [], PARAMS)
            self.assertEqual(len(sinais), n)

    def test_medias_invertidas_nao_rebentam(self):
        sinais = gerar_sinais(serie_sintetica(100), {"sma_fast": 60, "sma_slow": 10})
        self.assertEqual(len(sinais), 100)

    def test_preco_constante_nao_gera_trades(self):
        """Sem movimento nao ha cruzamento. Se houver, o sinal e ruido puro."""
        sinais = gerar_sinais(barras_de([100.0] * 200), PARAMS)
        self.assertEqual(set(sinais), {0}, "preco constante nao devia gerar posicao")

    def test_tendencia_clara_gera_posicao(self):
        """Numa subida monotona a estrategia tem de estar comprada a certa altura."""
        sinais = gerar_sinais(barras_de([100.0 + i for i in range(200)]), PARAMS)
        self.assertIn(1, sinais, "numa tendencia limpa devia haver posicao comprada")


class MediaMovel(unittest.TestCase):
    def test_none_ate_haver_historico(self):
        m = media_movel([1, 2, 3, 4, 5], 3)
        self.assertEqual(m[:2], [None, None])
        self.assertAlmostEqual(m[2], 2.0)
        self.assertAlmostEqual(m[4], 4.0)

    def test_comprimento_preservado(self):
        self.assertEqual(len(media_movel(list(range(50)), 10)), 50)

    def test_janela_de_um(self):
        self.assertEqual(media_movel([3.0, 7.0], 1), [3.0, 7.0])


class Risco(unittest.TestCase):
    def test_dentro_dos_limites(self):
        for r in (0.0, 0.001, 0.01, 0.05, 10.0):
            t = tamanho_posicao({"risco_por_trade": r})
            self.assertGreaterEqual(t, 0.1)
            self.assertLessEqual(t, 2.0, "sem tecto, o otimizador so aumenta alavancagem")

    def test_omissao_razoavel(self):
        self.assertGreater(tamanho_posicao({}), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
