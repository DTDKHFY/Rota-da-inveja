p = 'work57d.html'
s = open(p, encoding='utf-8').read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:80], s.count(a)); s = s.replace(a, b)

mod = open('caixa57.jsx', encoding='utf-8').read()

# 1. módulo novo antes da aba
rep('function FinanceTab({ finance, fin }) {', mod + '\nfunction FinanceTab({ finance, fin }) {')

# 2. dado novo: finance.cmd (normalização e criação)
rep('''      reserve: { ...base.finance.reserve, ...((p.finance && p.finance.reserve) || {}) },
    },''', '''      reserve: { ...base.finance.reserve, ...((p.finance && p.finance.reserve) || {}) },
      cmd: { ...((p.finance && p.finance.cmd && typeof p.finance.cmd === "object") ? p.finance.cmd : {}) },
    },''')
rep('''  reserve: { target: 0, saved: 0 },
});''', '''  reserve: { target: 0, saved: 0 },
  cmd: {},
});''')
rep('''    setPlan: (p) => finUpdate((f) => ({ ...f, plan: { ...f.plan, ...p } })),''', '''    setPlan: (p) => finUpdate((f) => ({ ...f, plan: { ...f.plan, ...p } })),
    setCmd: (p) => finUpdate((f) => ({ ...f, cmd: { ...(f.cmd || {}), ...p } })),''')

# 3. aba: comando em cima, o que existia vai para o Arquivo
rep('''  return (
    <>
      {isCurrent && <CardHero finance={finance} today={today} />}
      <CaixaHome finance={finance} today={today} period={period} setPeriod={setPeriod} kind={kind} setKind={setKind} sel={sel} setSel={setSel} />
      {isCurrent && <DueStrip finance={finance} today={today} fin={fin} />}
      <FreeToSpend plan={plan} finance={finance} isCurrent={isCurrent} />
      <OwePanel finance={finance} today={today} fin={fin} />''', '''  return (
    <>
      <CaixaCommand finance={finance} fin={fin} today={today} />
      <CxFold id="arquivo" title="Arquivo · contas, cartões, histórico">
      {isCurrent && <CardHero finance={finance} today={today} />}
      <CaixaHome finance={finance} today={today} period={period} setPeriod={setPeriod} kind={kind} setKind={setKind} sel={sel} setSel={setSel} />
      <FreeToSpend plan={plan} finance={finance} isCurrent={isCurrent} />
      <OwePanel finance={finance} today={today} fin={fin} />''')
rep('''      <EntryList finance={finance} period={period} fin={fin} />

      <p style={{ fontSize: 12, color: T.muted, marginTop: 18, lineHeight: 1.5 }}>''', '''      <EntryList finance={finance} period={period} fin={fin} />
      </CxFold>

      <p style={{ fontSize: 12, color: T.muted, marginTop: 18, lineHeight: 1.5 }}>''')

# 4. lançamento: envelope "livre sem culpa" + custo de oportunidade
rep('''  const [toast, setToast] = useState(null);
  const timer = useRef(null);
  const cats = type === "in" ? FIN_IN : FIN_OUT;''', '''  const [toast, setToast] = useState(null);
  const [free, setFree] = useState(false);
  const timer = useRef(null);
  const cats = type === "in" ? FIN_IN : FIN_OUT;''')
rep('''      fin.addEntry({ id, type: t, amount: v, cat, date: d, note: n, accountId: acc });''', '''      fin.addEntry({ id, type: t, amount: v, cat, date: d, note: n, accountId: acc, ...(t === "out" && free && value == null ? { free: true } : {}) });''')
rep('''    setAmount(""); setNote(""); setMsg(""); setMore(false); setInst(1);
  };''', '''    setAmount(""); setNote(""); setMsg(""); setMore(false); setInst(1); setFree(false);
  };''')
rep('''      <div style={{ fontSize: 13, color: T.muted, margin: "12px 0 6px" }}>Toque na categoria para salvar</div>''', '''      {type === "out" && !card && (
        <div style={{ display: "flex", gap: 6, marginTop: 10 }} role="group" aria-label="Envelope">
          {[[false, "Variáveis"], [true, "Livre sem culpa"]].map(([v, label]) => (
            <button key={label} onClick={() => setFree(v)} aria-pressed={free === v} style={{ ...chipStyle(free === v), flex: 1 }}>{label}</button>
          ))}
        </div>
      )}
      {type === "out" && parseMoney(amount) > 0 && <CxOppLine finance={finance} today={today} value={parseMoney(amount)} />}
      <div style={{ fontSize: 13, color: T.muted, margin: "12px 0 6px" }}>Toque na categoria para salvar</div>''')

# 5. botão principal legível na pele de sistema (contraste 2,7 → 4,9)
rep('''const btnPrimary = { get background() { return T.glow; },''', '''const btnPrimary = { get background() { return T.sys ? "#1d6fd8" : T.glow; },''')

# 6. animações do Caixa
rep('  input, select, button { font-family: inherit; }\n', '''  input, select, button { font-family: inherit; }
  .cx-rise{animation:cxRise .5s cubic-bezier(.2,.8,.2,1) both}
  .cx-fill{transform-origin:left;animation:cxFill 1.1s cubic-bezier(.2,.8,.2,1) both}
  .cx-pulse{animation:cxPulse 2.2s ease-in-out infinite}
  .cx-breath{animation:cxRise .5s cubic-bezier(.2,.8,.2,1) both,cxBreath 3.4s ease-in-out .5s infinite}
  .cx-flow{animation:cxFlow 3.2s linear infinite}
  .cx-draw{stroke-dasharray:1000;stroke-dashoffset:1000;animation:cxDraw 1.8s .25s cubic-bezier(.3,.7,.2,1) forwards}
  .cx-hit{animation:cxHit .5s 1s both}
  .cx-band{animation:cxBand 3s ease-in-out infinite}
  @keyframes cxRise{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  @keyframes cxFill{from{transform:scaleX(0)}to{transform:scaleX(1)}}
  @keyframes cxPulse{0%,100%{opacity:1}50%{opacity:.5}}
  @keyframes cxBreath{0%,100%{box-shadow:0 0 18px rgba(60,160,255,.16),inset 0 0 26px rgba(40,120,220,.09)}50%{box-shadow:0 0 34px rgba(60,160,255,.38),inset 0 0 30px rgba(40,120,220,.18)}}
  @keyframes cxFlow{0%{top:0;opacity:0}10%{opacity:1}90%{opacity:1}100%{top:calc(100% - 6px);opacity:0}}
  @keyframes cxDraw{to{stroke-dashoffset:0}}
  @keyframes cxHit{0%,100%{transform:none}20%{transform:translateX(-3px)}40%{transform:translateX(3px)}60%{transform:translateX(-2px)}}
  @keyframes cxBand{0%,100%{opacity:.25}50%{opacity:.45}}
  @media (prefers-reduced-motion: reduce){.cx-rise,.cx-fill,.cx-pulse,.cx-breath,.cx-flow,.cx-hit,.cx-band{animation:none!important}.cx-draw{animation:none!important;stroke-dashoffset:0}}
''')
open(p, 'w', encoding='utf-8').write(s)
print('ok')
s = open(p, encoding='utf-8').read()
rep('''width: 62, height: 62, borderRadius: "50%", border: "none", background: T.accent, color: "#fff", fontSize: 34, lineHeight: 1, cursor: "pointer", boxShadow: `0 6px 22px ${T.mist1}, 0 0 0 1px ${T.border}`, zIndex: 45 }}>''', '''width: 58, height: 58, borderRadius: T.sys ? 0 : "50%", border: T.sys ? `1px solid ${T.accent}` : "none", background: T.sys ? "rgba(16,56,100,.94)" : T.accent, color: T.sys ? T.accent : "#fff", fontSize: 32, lineHeight: 1, cursor: "pointer", boxShadow: T.sys ? `0 0 24px ${T.accent}66` : `0 6px 22px ${T.mist1}, 0 0 0 1px ${T.border}`, zIndex: 45 }}>''')
open(p, 'w', encoding='utf-8').write(s)
print('fab ok')
