#!/usr/bin/env python3
"""v68: as 5 últimas pinturas da árvore (estágios 15, 16, 18, 19 e 20) com as posições do vento."""
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()

def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:100], s.count(a))
    s = s.replace(a, b)

rep('''  P14: { moon: [0.20, 0.10], trunk: [0.55, 0.50, 0.75], tree: { base: [0.55, 0.80], top: 0.07, cx: 0.50, hw: 0.55, amp: 0.0095, canopy: [0.50, 0.30, 0.46, 0.20] } },
};''', '''  P14: { moon: [0.20, 0.10], trunk: [0.55, 0.50, 0.75], tree: { base: [0.55, 0.80], top: 0.07, cx: 0.50, hw: 0.55, amp: 0.0095, canopy: [0.50, 0.30, 0.46, 0.20] } },
  /* v68: as cinco últimas */
  P15: { moon: [0.195, 0.115], trunk: [0.56, 0.60, 0.74], gold: true, tree: { base: [0.55, 0.80], top: 0.09, cx: 0.50, hw: 0.55, amp: 0.0092, canopy: [0.50, 0.35, 0.46, 0.22] } },
  P16: { moon: [0.19, 0.105], trunk: [0.58, 0.40, 0.70], gold: true, tree: { base: [0.57, 0.79], top: 0.065, cx: 0.50, hw: 0.55, amp: 0.009, canopy: [0.50, 0.32, 0.47, 0.23] } },
  P18: { moon: [0.195, 0.115], trunk: [0.58, 0.48, 0.76], gold: true, tree: { base: [0.56, 0.80], top: 0.09, cx: 0.50, hw: 0.55, amp: 0.0088, canopy: [0.50, 0.33, 0.47, 0.23] } },
  P19: { moon: [0.19, 0.105], trunk: [0.55, 0.55, 0.75], gold: true, tree: { base: [0.55, 0.80], top: 0.095, cx: 0.50, hw: 0.55, amp: 0.0085, canopy: [0.50, 0.32, 0.47, 0.22] } },
  P20: { moon: [0.215, 0.11], trunk: [0.54, 0.56, 0.74], gold: true, tree: { base: [0.53, 0.78], top: 0.19, cx: 0.50, hw: 0.50, amp: 0.0085, canopy: [0.50, 0.40, 0.45, 0.20] } },
};''')
rep('''const TREE_FX_PLAN = "A B D P4 E P6 P7 P8 F P10 P11 P12 G P14 P14 P14 H H H H".split(" ");''',
    '''const TREE_FX_PLAN = "A B D P4 E P6 P7 P8 F P10 P11 P12 G P14 P15 P16 H P18 P19 P20".split(" ");''')
open(p, 'w', encoding='utf-8').write(s)
print('ok', len(s))
