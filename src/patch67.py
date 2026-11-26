#!/usr/bin/env python3
"""v67: 8 pinturas novas da árvore (estágios 4, 6, 7, 8, 10, 11, 12 e 14) com as posições do vento."""
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()

def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:100], s.count(a))
    s = s.replace(a, b)

rep('''  H: { moon: [0.18, 0.08], trunk: [0.62, 0.45, 0.70], gold: true, tree: { base: [0.60, 0.72], top: 0.0, cx: 0.62, hw: 0.55, amp: 0.0098, canopy: [0.62, 0.22, 0.44, 0.17] } },
};''', '''  H: { moon: [0.18, 0.08], trunk: [0.62, 0.45, 0.70], gold: true, tree: { base: [0.60, 0.72], top: 0.0, cx: 0.62, hw: 0.55, amp: 0.0098, canopy: [0.62, 0.22, 0.44, 0.17] } },
  /* v67: pinturas novas */
  P4: { moon: [0.24, 0.15], rune: [0.51, 0.765, 0.31, 0.043], tree: { base: [0.51, 0.75], top: 0.47, cx: 0.51, hw: 0.22, amp: 0.015 } },
  P6: { moon: [0.235, 0.15], trunk: [0.52, 0.60, 0.78], tree: { base: [0.52, 0.80], top: 0.26, cx: 0.51, hw: 0.40, amp: 0.013, canopy: [0.51, 0.39, 0.36, 0.13] } },
  P7: { moon: [0.225, 0.13], trunk: [0.56, 0.66, 0.74], tree: { base: [0.53, 0.81], top: 0.19, cx: 0.51, hw: 0.46, amp: 0.012, canopy: [0.51, 0.37, 0.42, 0.15] } },
  P8: { moon: [0.225, 0.145], trunk: [0.52, 0.61, 0.73], tree: { base: [0.53, 0.79], top: 0.28, cx: 0.51, hw: 0.46, amp: 0.012, canopy: [0.51, 0.45, 0.42, 0.15] } },
  P10: { moon: [0.23, 0.145], trunk: [0.56, 0.65, 0.74], tree: { base: [0.54, 0.77], top: 0.31, cx: 0.52, hw: 0.46, amp: 0.0115, canopy: [0.52, 0.48, 0.42, 0.15] } },
  P11: { moon: [0.215, 0.115], trunk: [0.53, 0.55, 0.74], tree: { base: [0.53, 0.83], top: 0.09, cx: 0.50, hw: 0.48, amp: 0.0105, canopy: [0.50, 0.30, 0.44, 0.19] } },
  P12: { moon: [0.225, 0.13], trunk: [0.54, 0.61, 0.71], tree: { base: [0.55, 0.79], top: 0.165, cx: 0.52, hw: 0.52, amp: 0.010, canopy: [0.52, 0.37, 0.46, 0.18] } },
  P14: { moon: [0.20, 0.10], trunk: [0.55, 0.50, 0.75], tree: { base: [0.55, 0.80], top: 0.07, cx: 0.50, hw: 0.55, amp: 0.0095, canopy: [0.50, 0.30, 0.46, 0.20] } },
};''')
rep('''const TREE_FX_PLAN = "A B D D E E E E F F F F G G G G H H H H".split(" ");''',
    '''const TREE_FX_PLAN = "A B D P4 E P6 P7 P8 F P10 P11 P12 G P14 P14 P14 H H H H".split(" ");''')
open(p, 'w', encoding='utf-8').write(s)
print('ok', len(s))
