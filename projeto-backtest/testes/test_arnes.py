"""Testes do arnes. O agente nao mexe aqui, mas se alguem mexer isto avisa.

O desfasamento de uma barra e o unico motivo pelo qual este backtest e
credivel. Se algum dia desaparecer numa refatoracao, e aqui que se descobre.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dados import Barra                                  # noqa: E402
from metricas import para_json, simular, sharpe          # noqa: E402


def barras_de(precos):
    return [Barra(f"2020-01-{i%28+1:02d}", p, p, p, p, 1000.0)
            for i, p in enumerate(precos)]


class Desfasamento(unittest.TestCase):
    def test_sinal_aplica_se_na_barra_seguinte(self):
        """O sinal calculado no fecho de hoje so pode ser executado amanha.

        Precos 100 -> 110 -> 121. Um sinal que so aparece na barra 1 nao pode
        capturar o movimento da barra 0 para a barra 1; so apanha o seguinte.
        """
        barras = barras_de([100.0, 110.0, 121.0])
        r = simular(barras, [0, 1, 1], custo_por_trade=0, slippage=0)
        self.assertAlmostEqual(r.retornos[0], 0.0, places=9,
                               msg="capturou um movimento que ainda nao conhecia")
        self.assertAlmostEqual(r.retornos[1], 0.10, places=9)

    def test_sinal_reativo_nao_captura_o_movimento_que_o_gerou(self):
        """Uma estrategia que reage a um salto nao pode lucrar com esse salto.

        Precos 100, 100, 120, 120. O sinal liga-se na barra 2, que e a barra do
        salto — ou seja, reagiu ao que acabou de acontecer. O retorno total tem
        de ser zero: quem so decidiu depois de ver o salto nao estava dentro
        quando ele aconteceu.
        """
        barras = barras_de([100.0, 100.0, 120.0, 120.0])
        r = simular(barras, [0, 0, 1, 1], custo_por_trade=0, slippage=0)
        self.assertAlmostEqual(r.retorno_total, 0.0, places=9)

    def test_sinal_antecipado_captura_o_movimento_seguinte(self):
        """O contraponto: quem ja estava posicionado na barra anterior apanha."""
        barras = barras_de([100.0, 100.0, 120.0])
        r = simular(barras, [0, 1, 1], custo_por_trade=0, slippage=0)
        self.assertAlmostEqual(r.retorno_total, 0.20, places=9)


class Custos(unittest.TestCase):
    def test_custos_reduzem_o_retorno(self):
        barras = barras_de([100.0, 110.0, 121.0])
        sem = simular(barras, [1, 1, 1], custo_por_trade=0, slippage=0)
        com = simular(barras, [1, 1, 1], custo_por_trade=0.01, slippage=0.01)
        self.assertGreater(sem.retorno_total, com.retorno_total)
        self.assertGreater(com.custo_total, 0)

    def test_conta_trades_nas_mudancas_de_posicao(self):
        barras = barras_de([100.0] * 6)
        self.assertEqual(simular(barras, [0, 1, 1, 0, 1, 1]).trades, 3)
        self.assertEqual(simular(barras, [0, 0, 0, 0, 0, 0]).trades, 0)


class Contrato(unittest.TestCase):
    def test_recusa_numero_errado_de_sinais(self):
        with self.assertRaises(ValueError):
            simular(barras_de([1.0, 2.0, 3.0]), [1, 1])

    def test_json_tem_os_campos_que_o_orquestrador_espera(self):
        barras = barras_de([100.0 + i for i in range(50)])
        d = para_json(simular(barras, [1] * 50))
        for campo in ("returns", "trades", "max_drawdown", "periods_per_year"):
            self.assertIn(campo, d)
        self.assertEqual(len(d["returns"]), 49)
        self.assertIsInstance(d["trades"], int)

    def test_drawdown_de_queda_conhecida(self):
        r = simular(barras_de([100.0, 100.0, 50.0]), [1, 1, 1],
                    custo_por_trade=0, slippage=0)
        self.assertAlmostEqual(r.drawdown_maximo, 0.5, places=6)

    def test_sharpe_de_serie_sem_variacao(self):
        self.assertEqual(sharpe([0.0] * 50), 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
