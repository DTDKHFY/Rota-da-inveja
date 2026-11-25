#!/usr/bin/env python3
"""v65: Trading dentro do Caixa (lucro pela conta x pelas operações) e árvore em 20 estágios pintados."""
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()

def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:100], s.count(a))
    s = s.replace(a, b)

# ---------- CSS: entrada suave da pintura nova ----------
rep('''  @keyframes cxBand{0%,100%{opacity:.25}50%{opacity:.45}}
''', '''  @keyframes cxBand{0%,100%{opacity:.25}50%{opacity:.45}}
  .ts-in{animation:tsIn .8s ease-out both}
  @keyframes tsIn{from{opacity:0}to{opacity:1}}
''')
rep('''.cx-hit,.cx-band{animation:none!important}''', '''.cx-hit,.cx-band,.ts-in{animation:none!important}''')

# ---------- estado: bloco trading ----------
rep('''  finance: newFinance(),
  nofap:''', '''  finance: newFinance(),
  trading: newTrading(),
  nofap:''')
rep('''    finals: Array.isArray(p.finals) ? p.finals : [],
    history:''', '''    finals: Array.isArray(p.finals) ? p.finals : [],
    trading: normTrading(p.trading),
    history:''')
# recomeçar a rota mantém o dinheiro do trading
rep('''finance: s.finance, scroll: s.scroll,''', '''finance: s.finance, trading: s.trading, scroll: s.scroll,''')

# ---------- App: funções do trading ----------
rep('''  const fin = {
    addEntry:''', '''  const trdUpdate = (fn) => setState((s) => ({ ...s, trading: fn(normTrading(s.trading)) }));
  const trd = {
    add: (kind, item, patch) => trdUpdate((t) => ({ ...t, ...(patch || {}), [kind]: [...t[kind], { ...item, id: Date.now() + Math.random() }] })),
    remove: (kind, id) => trdUpdate((t) => ({ ...t, [kind]: t[kind].filter((x) => x.id !== id) })),
    set: (patch) => trdUpdate((t) => ({ ...t, ...patch })),
  };
  const fin = {
    addEntry:''')
rep('''<FinanceTab finance={state.finance || newFinance()} fin={fin} />''',
    '''<FinanceTab finance={state.finance || newFinance()} fin={fin} trading={state.trading} trd={trd} />''')

# ---------- Caixa: chave Caixa | Trading ----------
rep('''function FinanceTab({ finance, fin }) {
  const today = dayKey(new Date());''', '''function FinanceTab({ finance, fin, trading, trd }) {
  const today = dayKey(new Date());
  const [view, setViewRaw] = useState(() => { try { return localStorage.getItem("rb.cx.view") === "trading" ? "trading" : "caixa"; } catch (e) { return "caixa"; } });
  const setView = (v) => { setViewRaw(v); try { localStorage.setItem("rb.cx.view", v); } catch (e) { /* sem armazenamento */ } };''')
rep('''  return (
    <>
      <CaixaCommand finance={finance} fin={fin} today={today} />''', '''  return (
    <>
      <CxSwitch view={view} setView={setView} />
      {view === "trading" ? <TradingTab trading={trading} trd={trd} today={today} /> : (<>
      <CaixaCommand finance={finance} fin={fin} today={today} />''')
rep('''            <QuickEntry finance={finance} today={today} fin={fin} />
          </div>
        </div>
      )}
    </>
  );
}''', '''            <QuickEntry finance={finance} today={today} fin={fin} />
          </div>
        </div>
      )}
      </>)}
    </>
  );
}''')

TRADING = r'''/* ==================== TRADING (v65) ==================== */
/* Lucro real = saldo + saques − depósitos. O saldo da corretora já desconta taxa, swap e comissão;
   depósito e saque mexem no saldo mas não são lucro. O diário de operações confere esse número:
   a diferença entre os dois é o que ficou sem lançar. O saldo vale sem posição aberta (lucro flutuante não entra). */
function newTrading() { return { moeda: "USD", meta: 5000, stopsDia: 2, perdaMes: 6, movs: [], ops: [], saldos: [] }; }
function normTrading(t) {
  const x = t && typeof t === "object" && !Array.isArray(t) ? t : {};
  return { ...newTrading(), ...x, movs: Array.isArray(x.movs) ? x.movs : [], ops: Array.isArray(x.ops) ? x.ops : [], saldos: Array.isArray(x.saldos) ? x.saldos : [] };
}
const TD_MOV = {
  dep: { name: "Depósito", hint: "dinheiro que você colocou na corretora" },
  saq: { name: "Saque", hint: "dinheiro que você tirou da corretora" },
  custo: { name: "Custo", hint: "taxa, swap ou comissão que não está no resultado das operações" },
};
const tdMoney = (v, moeda) => (Number(v) || 0).toLocaleString("pt-BR", { style: "currency", currency: moeda === "BRL" ? "BRL" : "USD" });
const tdSigned = (v, moeda) => `${v > 0.004 ? "+" : v < -0.004 ? "−" : ""}${tdMoney(Math.abs(v), moeda)}`;
const tdFix = (n, d = 2) => (n == null || !isFinite(n) ? "—" : n.toFixed(d).replace(".", ","));
/* ordem: data; no mesmo dia, ordem de lançamento (o id nasce do relógio) */
const tdCmp = (a, b) => (a.date < b.date ? -1 : a.date > b.date ? 1 : (Number(a.id) || 0) - (Number(b.id) || 0));
function tradeModel(t, today) {
  const val = (x) => Number(x.valor) || 0;
  const res = (o) => Number(o.res) || 0;
  const sumV = (arr) => round2(arr.reduce((a, x) => a + val(x), 0));
  const ops = [...t.ops].filter((o) => o && o.date).sort(tdCmp);
  const movs = [...t.movs].filter((x) => x && x.date);
  const saldos = [...t.saldos].filter((x) => x && x.date).sort(tdCmp);
  const saldo = saldos.length ? saldos[saldos.length - 1] : null;
  /* o saldo vale como fechamento do dia: tudo o que tem a data dele ou antes entra na conferência */
  const upTo = (x) => !saldo || x.date <= saldo.date;
  const of = (arr, tipo) => arr.filter((x) => x.tipo === tipo);
  const dep = sumV(of(movs, "dep")), saq = sumV(of(movs, "saq")), custos = sumV(of(movs, "custo"));
  const opsRes = round2(ops.reduce((a, o) => a + res(o), 0));
  const lucroOps = round2(opsRes - custos);
  /* conferência na data do último saldo */
  let lucroConta = null, gap = null, opsAte = null, depois = 0, tarde = 0;
  if (saldo) {
    const mv = movs.filter(upTo);
    const depD = sumV(of(mv, "dep")), saqD = sumV(of(mv, "saq")), custD = sumV(of(mv, "custo"));
    opsAte = round2(ops.filter(upTo).reduce((a, o) => a + res(o), 0) - custD);
    lucroConta = round2(val(saldo) + saqD - depD);
    gap = round2(lucroConta - opsAte);
    depois = ops.filter((o) => !upTo(o)).length + movs.filter((x) => !upTo(x)).length;
    /* lançado no dia do saldo, depois dele: pode ser operação feita depois de informar o saldo */
    tarde = ops.filter((o) => o.date === saldo.date && Number(o.id) > Number(saldo.id)).length;
  }
  /* o lucro da meta: o conferido pela conta + o que as operações fizeram depois dele */
  const lucro = saldo ? round2(lucroConta + (lucroOps - opsAte)) : lucroOps;
  const saldoAgora = saldo ? round2(val(saldo) + (lucroOps - opsAte) + sumV(of(movs.filter((x) => !upTo(x)), "dep")) - sumV(of(movs.filter((x) => !upTo(x)), "saq"))) : round2(dep - saq + lucroOps);
  const rs = ops.map(res);
  const wins = rs.filter((v) => v > 0), losses = rs.filter((v) => v < 0);
  const sw = wins.reduce((a, b) => a + b, 0), sl = -losses.reduce((a, b) => a + b, 0);
  const n = rs.length;
  const avgW = wins.length ? sw / wins.length : 0, avgL = losses.length ? sl / losses.length : 0;
  let cum = 0, peak = 0, dd = 0;
  const curve = [0];
  rs.forEach((v) => { cum += v; curve.push(round2(cum)); if (cum > peak) peak = cum; if (peak - cum > dd) dd = peak - cum; });
  const rList = ops.filter((o) => o.r != null && o.r !== "" && isFinite(Number(o.r))).map((o) => Number(o.r));
  const regN = ops.filter((o) => o.regras === true || o.regras === false).length;
  const reg = ops.filter((o) => o.regras === true).length;
  const mes = today.slice(0, 7), semana = weekStartKey(new Date(`${today}T12:00:00`));
  const opsMes = ops.filter((o) => o.date.slice(0, 7) === mes);
  const resMes = round2(opsMes.reduce((a, o) => a + res(o), 0));
  const opsSem = ops.filter((o) => weekStartKey(new Date(`${o.date}T12:00:00`)) === semana).length;
  const stopsHoje = ops.filter((o) => o.date === today && res(o) < 0).length;
  const baseMes = saldoAgora - resMes; /* capital no começo do mês, aproximado */
  const perdaMesPct = resMes < 0 && baseMes > 0 ? (-resMes / baseMes) * 100 : 0;
  return {
    ops, dep, saq, custos, opsRes, lucroOps, saldo, lucroConta, gap, depois, tarde, lucro, saldoAgora, n, wins: wins.length, losses: losses.length,
    acerto: n ? wins.length / n : null, payoff: wins.length && avgL > 0 ? avgW / avgL : null, expect: n ? opsRes / n : null,
    pf: sl > 0 ? sw / sl : sw > 0 ? Infinity : null, dd: round2(dd), curve,
    rMed: rList.length ? rList.reduce((a, b) => a + b, 0) / rList.length : null, rSum: rList.length ? rList.reduce((a, b) => a + b, 0) : null,
    reg, regN, opsMes: opsMes.length, resMes, opsSem, stopsHoje, perdaMesPct,
  };
}

function CxSwitch({ view, setView }) {
  const b = (id, label) => (
    <button key={id} onClick={() => setView(id)} aria-pressed={view === id}
      style={{ flex: 1, minHeight: 44, border: `1px solid ${view === id ? T.accent : T.border}`, background: view === id ? `${T.accent}22` : T.panel, color: view === id ? T.accent : T.muted, fontFamily: F.ui, fontSize: 13.5, fontWeight: 700, letterSpacing: "0.16em", textTransform: "uppercase", cursor: "pointer" }}>
      {label}
    </button>
  );
  return <div role="group" aria-label="Seção do Caixa" style={{ display: "flex", gap: 6, margin: "2px 0 12px" }}>{b("caixa", "Caixa")}{b("trading", "Trading")}</div>;
}

function TdCurve({ curve, color }) {
  if (curve.length < 3) return null;
  const W = 320, H = 96, pad = 6;
  const min = Math.min(0, ...curve), max = Math.max(0, ...curve);
  const span = max - min || 1;
  const x = (i) => pad + (i / (curve.length - 1)) * (W - pad * 2);
  const y = (v) => pad + (1 - (v - min) / span) * (H - pad * 2);
  const d = curve.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} width="100%" height={H} preserveAspectRatio="none" role="img" aria-label={`Curva do resultado acumulado em ${curve.length - 1} operações`} style={{ display: "block" }}>
      <line x1={pad} x2={W - pad} y1={y(0)} y2={y(0)} stroke={T.muted} strokeOpacity=".5" strokeDasharray="3 4" vectorEffect="non-scaling-stroke" />
      <path d={d} fill="none" stroke={color} strokeWidth="2" strokeLinejoin="round" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}

function TdForm({ kind, t, today, onSave, onClose }) {
  const [date, setDate] = useState(today);
  const [val, setVal] = useState("");
  const [sign, setSign] = useState(1);
  const [ativo, setAtivo] = useState(t.lastAtivo || "");
  const [r, setR] = useState("");
  const [setup, setSetup] = useState("");
  const [regras, setRegras] = useState(null);
  const [obs, setObs] = useState("");
  const [err, setErr] = useState("");
  const isOp = kind === "op", isSaldo = kind === "saldo";
  const title = isOp ? "Nova operação" : isSaldo ? "Saldo da corretora" : TD_MOV[kind].name;
  const setups = [...new Set(t.ops.map((o) => o.setup).filter(Boolean))].slice(-12);
  const lbl = { display: "block", fontSize: 12.5, color: T.muted, marginTop: 9 };
  const seg = (on, col) => ({ minHeight: 44, border: `1px solid ${on ? col : T.border}`, background: on ? `${col}22` : "none", color: on ? col : T.muted, fontWeight: 700, fontSize: 14.5, cursor: "pointer", fontFamily: F.ui });
  const save = () => {
    const v = parseMoney(val);
    if (!isFinite(v) || v < 0 || (!isOp && !isSaldo && v === 0)) { setErr(isSaldo ? "Informe o saldo." : "Informe o valor."); return; }
    if (!date || date > today) { setErr("Data inválida."); return; }
    if (isOp && regras == null) { setErr("Marque se a operação seguiu as regras."); return; }
    if (isOp) {
      const rv = r.trim() === "" ? NaN : parseMoney(r);
      const a = ativo.trim().toUpperCase().slice(0, 24);
      onSave("ops", { date, ativo: a || undefined, res: round2(sign * Math.abs(v)), r: isFinite(rv) ? round2(sign * Math.abs(rv)) : undefined, setup: setup.trim().slice(0, 40) || undefined, regras, obs: obs.trim().slice(0, 200) || undefined }, a ? { lastAtivo: a } : null);
    } else if (isSaldo) onSave("saldos", { date, valor: round2(v) });
    else onSave("movs", { date, tipo: kind, valor: round2(Math.abs(v)), obs: obs.trim().slice(0, 200) || undefined });
    onClose();
  };
  return (
    <div data-pn="" className="cx-rise" style={{ marginTop: 12, border: `1px solid ${T.accent}88`, background: T.panel, padding: "12px 12px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <b style={{ flex: 1, fontSize: 15.5, color: T.accentSoft }}>{title}</b>
        <button onClick={onClose} style={{ background: "none", border: "none", color: T.muted, fontFamily: F.ui, fontSize: 14.5, padding: "8px 4px", cursor: "pointer" }}>Fechar</button>
      </div>
      {!isOp && <div style={{ fontSize: 13, color: T.muted, lineHeight: 1.4 }}>{isSaldo ? "O saldo que a corretora mostra no fim do dia, sem posição aberta. É ele que confere o lucro real." : TD_MOV[kind].hint}</div>}
      {isOp && (
        <div role="group" aria-label="Resultado da operação" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 8 }}>
          <button aria-pressed={sign === 1} onClick={() => setSign(1)} style={seg(sign === 1, T.good)}>Ganho</button>
          <button aria-pressed={sign === -1} onClick={() => setSign(-1)} style={seg(sign === -1, T.danger)}>Perda</button>
        </div>
      )}
      <label style={lbl}>{isOp ? `Resultado líquido (${t.moeda === "BRL" ? "R$" : "US$"})` : `${isSaldo ? "Saldo" : "Valor"} (${t.moeda === "BRL" ? "R$" : "US$"})`}
        <input value={val} onChange={(e) => { setVal(e.target.value); setErr(""); }} inputMode="decimal" placeholder="0,00" style={{ ...inputStyle, width: "100%", marginTop: 4, ...cxMono, fontSize: 18 }} />
      </label>
      <label style={lbl}>Data
        <input type="date" value={date} max={today} onChange={(e) => setDate(e.target.value)} style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
      </label>
      {isOp && (<>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <label style={lbl}>Ativo
            <input value={ativo} onChange={(e) => setAtivo(e.target.value)} placeholder="EURUSD" autoCapitalize="characters" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
          </label>
          <label style={lbl}>R (opcional)
            <input value={r} onChange={(e) => setR(e.target.value)} inputMode="decimal" placeholder="1,5" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
          </label>
        </div>
        <label style={lbl}>Setup (opcional)
          <input value={setup} onChange={(e) => setSetup(e.target.value)} list="td-setups" placeholder="rompimento, pullback…" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
          <datalist id="td-setups">{setups.map((x) => <option key={x} value={x} />)}</datalist>
        </label>
        <div style={lbl}>Seguiu as regras do plano?</div>
        <div role="group" aria-label="Seguiu as regras" style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 6, marginTop: 4 }}>
          <button aria-pressed={regras === true} onClick={() => { setRegras(true); setErr(""); }} style={seg(regras === true, T.good)}>Sim</button>
          <button aria-pressed={regras === false} onClick={() => { setRegras(false); setErr(""); }} style={seg(regras === false, T.gold)}>Não</button>
        </div>
      </>)}
      {!isSaldo && (
        <label style={lbl}>Observação (opcional)
          <input value={obs} onChange={(e) => setObs(e.target.value)} placeholder={isOp ? "o que aconteceu, o que repetir ou evitar" : ""} style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
        </label>
      )}
      {err && <div role="alert" style={{ color: T.danger, fontSize: 13.5, marginTop: 8 }}>{err}</div>}
      <button onClick={save} style={{ ...btnPrimary, width: "100%", minHeight: 46, marginTop: 12 }}>Salvar</button>
    </div>
  );
}

function TdSettings({ t, trd }) {
  const [v, setV] = useState({ meta: String(t.meta), stopsDia: String(t.stopsDia), perdaMes: String(t.perdaMes) });
  const [ok, setOk] = useState("");
  const lbl = { display: "block", fontSize: 12.5, color: T.muted, marginTop: 9 };
  const save = () => {
    const meta = parseMoney(v.meta), sd = Math.round(parseMoney(v.stopsDia)), pm = parseMoney(v.perdaMes);
    if (!(meta > 0) || !(sd >= 1 && sd <= 10) || !(pm > 0 && pm <= 50)) { setOk("Confira os valores: meta > 0, stops de 1 a 10, perda de 0 a 50%."); return; }
    trd.set({ meta: round2(meta), stopsDia: sd, perdaMes: round2(pm) });
    setOk("Salvo.");
  };
  const segB = (on) => ({ flex: 1, minHeight: 42, border: `1px solid ${on ? T.accent : T.border}`, background: on ? `${T.accent}22` : "none", color: on ? T.accent : T.muted, fontWeight: 700, cursor: "pointer", fontFamily: F.ui, fontSize: 14 });
  return (
    <div style={{ padding: "4px 2px 6px" }}>
      <div style={lbl}>Moeda da conta</div>
      <div role="group" aria-label="Moeda" style={{ display: "flex", gap: 6, marginTop: 4 }}>
        <button aria-pressed={t.moeda !== "BRL"} onClick={() => trd.set({ moeda: "USD" })} style={segB(t.moeda !== "BRL")}>US$</button>
        <button aria-pressed={t.moeda === "BRL"} onClick={() => trd.set({ moeda: "BRL" })} style={segB(t.moeda === "BRL")}>R$</button>
      </div>
      <label style={lbl}>Meta de lucro líquido
        <input value={v.meta} onChange={(e) => setV({ ...v, meta: e.target.value })} inputMode="decimal" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
      </label>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <label style={lbl}>Stops por dia (encerra)
          <input value={v.stopsDia} onChange={(e) => setV({ ...v, stopsDia: e.target.value })} inputMode="numeric" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
        </label>
        <label style={lbl}>Perda máx. no mês (%)
          <input value={v.perdaMes} onChange={(e) => setV({ ...v, perdaMes: e.target.value })} inputMode="decimal" style={{ ...inputStyle, width: "100%", marginTop: 4 }} />
        </label>
      </div>
      <button onClick={save} style={{ ...btnPrimary, width: "100%", minHeight: 44, marginTop: 12 }}>Salvar ajustes</button>
      {ok && <div role="status" style={{ fontSize: 13.5, marginTop: 6, color: ok === "Salvo." ? T.good : T.danger }}>{ok}</div>}
    </div>
  );
}

function TradingTab({ trading, trd, today }) {
  const t = normTrading(trading);
  const m = tradeModel(t, today);
  const [form, setForm] = useState(null);
  const [del, setDel] = useState(null);
  const M = (v) => tdMoney(v, t.moeda);
  const S = (v) => tdSigned(v, t.moeda);
  const col = (v) => (v > 0.004 ? T.good : v < -0.004 ? T.danger : T.text);
  const pctMeta = t.meta > 0 ? Math.max(0, Math.min(100, (m.lucro / t.meta) * 100)) : 0;
  const alerts = [];
  if (m.stopsHoje >= t.stopsDia) alerts.push({ red: true, text: `Contrato de Ulisses: ${m.stopsHoje} stop${m.stopsHoje > 1 ? "s" : ""} hoje. O dia acabou. Feche a plataforma.` });
  else if (t.stopsDia > 1 && m.stopsHoje === t.stopsDia - 1) alerts.push({ red: false, text: `${m.stopsHoje} stop hoje. Mais um e o dia acaba.` });
  if (m.perdaMesPct >= t.perdaMes) alerts.push({ red: true, text: `Perda de ${tdFix(m.perdaMesPct, 1)}% no mês (limite ${t.perdaMes}%). Só simulador até o dia 1.` });
  if (m.regN >= 5 && m.reg / m.regN < 0.8) alerts.push({ red: false, text: `Só ${Math.round((m.reg / m.regN) * 100)}% das operações seguiram as regras. O problema não é o mercado.` });
  const rec = !m.saldo ? { c: T.muted, text: "Informe o saldo da corretora (sem posição aberta) para conferir o lucro real. Até lá, a meta usa o lucro das operações." }
    : Math.abs(m.gap) < 0.01 ? { c: T.good, text: `Conta e diário batem no saldo de ${fmtDM(m.saldo.date)}.` }
    : m.gap < 0 ? { c: T.danger, text: `Faltam ${M(-m.gap)} no diário: taxa, swap, comissão ou operação não lançada. Lance como custo ou operação.` }
    : { c: T.gold, text: `Sobram ${M(m.gap)} na conta: ganho, depósito ou reembolso não lançado.` };
  const add = (kind, item, patch) => { trd.add(kind, item, patch); };
  const hist = [
    ...t.ops.map((o) => ({ k: "ops", o })), ...t.movs.map((o) => ({ k: "movs", o })), ...t.saldos.map((o) => ({ k: "saldos", o })),
  ].filter((x) => x.o && x.o.date).sort((a, b) => -tdCmp(a.o, b.o)).slice(0, 80);
  const rowS = { display: "flex", alignItems: "center", gap: 8, padding: "8px 0", borderTop: `1px solid ${T.border}55`, fontSize: 14 };
  const smallBtn = { flex: 1, minHeight: 44, background: T.panel, border: `1px solid ${T.border}`, color: T.accentSoft, fontFamily: F.ui, fontSize: 13.5, fontWeight: 700, cursor: "pointer", padding: "0 4px" };
  const line = (k, v, c) => (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 8, padding: "6px 0", borderTop: `1px solid ${T.border}55`, fontSize: 14 }}>
      <span style={{ color: T.muted }}>{k}</span><span style={{ ...cxMono, color: c || T.text, fontWeight: 700, textAlign: "right" }}>{v}</span>
    </div>
  );
  return (
    <>
      <section data-pn="" className="cx-rise" style={{ border: `1px solid ${T.border}`, background: T.panel, padding: "16px 14px 14px", textAlign: "center" }}>
        <div style={{ ...cxLbl, letterSpacing: "0.26em" }}>Lucro real · {m.saldo ? "pela conta" : "pelas operações"}</div>
        <div style={{ ...cxMono, fontSize: 36, fontWeight: 700, lineHeight: 1.1, marginTop: 6, color: col(m.lucro) }}>{S(m.lucro)}</div>
        <div style={{ fontSize: 13.5, color: T.muted, margin: "6px 0 8px" }}>meta {M(t.meta)} · <b style={{ color: T.gold }}>{Math.round(pctMeta)}%</b>{m.lucro < t.meta ? ` · faltam ${M(t.meta - Math.max(0, m.lucro))}` : " · meta batida"}</div>
        <CxBar pct={pctMeta} color={T.gold} h={8} />
      </section>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, marginTop: 8 }}>
        <CxTile label="Pela conta" value={m.saldo ? S(m.lucroConta) : "—"} sub={m.saldo ? `saldo + saques − depósitos · ${fmtDM(m.saldo.date)}` : "falta o saldo"} color={m.saldo ? col(m.lucroConta) : T.muted} small />
        <CxTile label="Pelas operações" value={S(m.lucroOps)} sub={`${m.n} op. − custos lançados`} color={col(m.lucroOps)} small />
      </div>
      <div data-pn="" role="status" style={{ marginTop: 8, padding: "10px 12px", border: `1px solid ${rec.c}66`, background: T.panel, fontSize: 14, lineHeight: 1.45 }}>
        {m.saldo && Math.abs(m.gap) >= 0.01 && <div style={{ ...cxMono, fontWeight: 700, color: rec.c, marginBottom: 2 }}>Diferença não explicada: {S(m.gap)}</div>}
        <span style={{ color: m.saldo ? T.text : T.muted }}>{rec.text}</span>
        {m.saldo && m.tarde > 0 && Math.abs(m.gap) >= 0.01 && <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>Operou depois de informar o saldo de {fmtDM(m.saldo.date)}? Informe o saldo de novo: o saldo vale como fechamento do dia.</div>}
        {m.saldo && m.depois > 0 && <div style={{ fontSize: 13, color: T.muted, marginTop: 4 }}>{m.depois} lançamento(s) depois desse saldo entram no lucro, mas só são conferidos no próximo saldo.</div>}
      </div>

      {alerts.map((a, i) => (
        <div key={i} data-pn="" role="alert" className="cx-rise" style={{ marginTop: 8, padding: "10px 12px", border: `1px solid ${a.red ? T.danger : T.gold}aa`, background: a.red ? "rgba(255,83,104,.09)" : "rgba(255,201,92,.07)", fontSize: 14.5, lineHeight: 1.4, fontWeight: a.red ? 700 : 500 }}>
          {a.text}
        </div>
      ))}

      <button onClick={() => setForm(form === "op" ? null : "op")} style={{ ...btnPrimary, width: "100%", minHeight: 48, marginTop: 12, fontSize: 15 }}>+ Operação</button>
      <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
        <button onClick={() => setForm("dep")} style={smallBtn}>Depósito</button>
        <button onClick={() => setForm("saq")} style={smallBtn}>Saque</button>
        <button onClick={() => setForm("custo")} style={smallBtn}>Custo</button>
        <button onClick={() => setForm("saldo")} style={{ ...smallBtn, color: T.gold, borderColor: `${T.gold}88` }}>Saldo</button>
      </div>
      {form && <TdForm key={form} kind={form} t={t} today={today} onSave={add} onClose={() => setForm(null)} />}

      <Section title="Desempenho">
        {m.n === 0 ? (
          <div style={{ fontSize: 14, color: T.muted, lineHeight: 1.45 }}>Nenhuma operação lançada. Lance cada operação real com o resultado líquido: acerto, payoff e expectativa só aparecem com dados.</div>
        ) : (<>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <CxTile label="Acerto" value={`${Math.round(m.acerto * 100)}%`} sub={`${m.wins} de ${m.n} no lucro`} color={T.text} small />
            <CxTile label="Payoff" value={tdFix(m.payoff)} sub="ganho médio ÷ perda média" color={m.payoff != null && m.payoff >= 1 ? T.good : T.text} small />
            <CxTile label="Expectativa" value={S(m.expect)} sub="por operação" color={col(m.expect)} small />
            <CxTile label="Fator de lucro" value={m.pf === Infinity ? "∞" : tdFix(m.pf)} sub="ganhos ÷ perdas" color={m.pf != null && m.pf >= 1 ? T.good : T.danger} small />
            <CxTile label="Rebaixamento" value={m.dd > 0 ? `−${M(m.dd)}` : M(0)} sub="maior queda do pico" color={m.dd > 0 ? T.danger : T.text} small />
            <CxTile label="Regras" value={m.regN ? `${Math.round((m.reg / m.regN) * 100)}%` : "—"} sub={`${m.reg} de ${m.regN} no plano`} color={m.regN && m.reg / m.regN >= 0.8 ? T.good : T.gold} small />
            <CxTile label="Semana" value={String(m.opsSem)} sub="operações nesta semana" color={T.text} small />
            <CxTile label="Mês" value={S(m.resMes)} sub={`${m.opsMes} operação(ões)`} color={col(m.resMes)} small />
            {m.rMed != null && <CxTile label="R médio" value={tdFix(m.rMed)} sub={`soma ${tdFix(m.rSum, 1)}R`} color={col(m.rMed)} small />}
          </div>
          {m.curve.length >= 3 && (
            <div data-pn="" style={{ marginTop: 8, border: `1px solid ${T.border}`, background: T.panel, padding: "10px 10px 8px" }}>
              <div style={{ ...cxLbl, marginBottom: 6 }}>Curva · resultado acumulado</div>
              <TdCurve curve={m.curve} color={m.curve[m.curve.length - 1] >= 0 ? T.good : T.danger} />
            </div>
          )}
        </>)}
      </Section>

      <Section title="Capital">
        {line("Depósitos", M(m.dep))}
        {line("Saques", M(m.saq))}
        {line("Custos lançados", M(m.custos), m.custos > 0 ? T.danger : null)}
        {line("Último saldo informado", m.saldo ? `${M(m.saldo.valor)} · ${fmtDM(m.saldo.date)}` : "—", T.gold)}
        {line("Saldo estimado hoje", M(m.saldoAgora))}
        <div style={{ fontSize: 12.5, color: T.muted, lineHeight: 1.45, marginTop: 8 }}>
          Lucro real = saldo + saques − depósitos. Taxas e swap já estão no saldo; lançá-los como custo só serve para o diário bater. Conta em dólar: a conversão para reais muda com o câmbio e o IOF.
        </div>
      </Section>

      <CxFold id="td-hist" title={`Histórico · ${hist.length} lançamento(s)`}>
        <div data-pn="" style={{ border: `1px solid ${T.border}`, borderTop: "none", background: T.panel, padding: "2px 12px 8px" }}>
          {!hist.length && <div style={{ fontSize: 14, color: T.muted, padding: "10px 0" }}>Nada lançado ainda.</div>}
          {hist.map(({ k, o }) => {
            const key = `${k}:${o.id}`;
            const isOp = k === "ops", isSaldo = k === "saldos";
            const v = isOp ? Number(o.res) || 0 : Number(o.valor) || 0;
            const title = isOp ? `Operação${o.ativo ? ` · ${o.ativo}` : ""}` : isSaldo ? "Saldo informado" : (TD_MOV[o.tipo] || { name: o.tipo }).name;
            const sub = isOp ? [o.setup, o.r != null ? `${tdFix(Number(o.r), 1)}R` : null, o.regras === false ? "fora das regras" : null, o.obs].filter(Boolean).join(" · ") : o.obs || "";
            const shown = isOp ? S(v) : isSaldo ? M(v) : o.tipo === "dep" ? `+${M(v)}` : `−${M(v)}`;
            const c = isOp ? col(v) : isSaldo ? T.gold : o.tipo === "dep" ? T.text : T.danger;
            return (
              <div key={key} style={rowS}>
                <span style={{ ...cxMono, flex: "0 0 42px", fontSize: 12.5, color: T.muted }}>{fmtDM(o.date)}</span>
                <span style={{ flex: 1, minWidth: 0 }}>
                  <span style={{ display: "block", fontWeight: 600 }}>{title}</span>
                  {sub && <span style={{ display: "block", fontSize: 12.5, color: o.regras === false ? T.gold : T.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{sub}</span>}
                </span>
                <span style={{ ...cxMono, fontWeight: 700, color: c, whiteSpace: "nowrap" }}>{shown}</span>
                {del === key ? (
                  <button onClick={() => { trd.remove(k, o.id); setDel(null); }} style={{ minHeight: 36, background: "none", border: `1px solid ${T.danger}`, color: T.danger, fontFamily: F.ui, fontSize: 12.5, fontWeight: 700, cursor: "pointer", padding: "0 8px" }}>Apagar</button>
                ) : (
                  <button onClick={() => setDel(key)} aria-label={`Apagar ${title} de ${fmtDM(o.date)}`} style={{ minWidth: 36, minHeight: 36, background: "none", border: "none", color: T.muted, fontSize: 17, cursor: "pointer" }}>×</button>
                )}
              </div>
            );
          })}
        </div>
      </CxFold>

      <CxFold id="td-cfg" title="Ajustes do trading">
        <div data-pn="" style={{ border: `1px solid ${T.border}`, borderTop: "none", background: T.panel, padding: "4px 12px 12px" }}>
          <TdSettings t={t} trd={trd} />
        </div>
      </CxFold>

      <p style={{ fontSize: 12, color: T.muted, marginTop: 18, lineHeight: 1.5 }}>
        Trading fica separado das moedas do Sistema e não gera XP. Sincroniza com o resto e entra no backup. Não é recomendação de investimento.
      </p>
    </>
  );
}

function FinanceTab('''
rep('''function FinanceTab(''', TRADING)

# ---------- árvore: cresce pelo % da rota ----------
rep('''    level: Math.round(4 + 21 * (done / total)), rankIdx:''', '''    level: Math.round(4 + 21 * routeProgress(state.finals)), rankIdx:''')
rep('''      {line("Tronco", `Dia ${Math.min(rp.day, rp.days)} de ${rp.days} da rota (até ${fmtDM(state.deadline)})`)}''',
    '''      {line("Tronco", `Rota ${Math.round(routeProgress(state.finals) * 100)}% · estágio ${treeStageInfo(state).i + 1} de ${TREE_STAGES.length} · dia ${Math.min(rp.day, rp.days)} de ${rp.days} (até ${fmtDM(state.deadline)})`)}''')

STAGES = r'''/* ==================== ÁRVORE EM 20 ESTÁGIOS (v65) ==================== */
/* uma pintura a cada 5% da rota; o % é o mesmo do mapa (marcos dos objetivos finais) */
const TREE_STAGES = ["Solo marcado", "Semente desperta", "Broto", "Plântula", "Muda", "Muda lenhosa", "Arvoreta", "Primeiros galhos", "Árvore jovem", "Copa formada",
  "Árvore firme", "Tronco grosso", "Árvore adulta", "Runas acesas", "Primeiros frutos", "Árvore antiga", "Raízes de pedra", "Árvore anciã", "Colosso", "Árvore-mundo"];
const treeArt = (i) => `arvore/e${String(i + 1).padStart(2, "0")}.webp`;
const treeThumb = (i) => `arvore/t${String(i + 1).padStart(2, "0")}.webp`;
function treeStageInfo(state) {
  const fs = state.finals || [];
  const p = routeProgress(fs);
  const pct = Math.round(p * 100);
  const N = TREE_STAGES.length;
  const i = Math.max(0, Math.min(N - 1, Math.floor(pct / 5)));
  const last = i === N - 1;
  const nextAt = last ? 100 : (i + 1) * 5;
  /* quantos marcos, em média, levam ao próximo estágio */
  const steps = fs.filter((f) => !f.done).map((f) => 100 / (((f.milestones || []).length + 1) * fs.length));
  const avg = steps.length ? steps.reduce((a, b) => a + b, 0) / steps.length : 0;
  const need = nextAt - 0.5 - p * 100;
  const marcos = last || !(avg > 0) ? null : Math.max(1, Math.ceil(need / avg - 1e-9));
  return { p, pct, i, name: TREE_STAGES[i], nextAt, last, marcos };
}
/* a pintura do estágio; quando o estágio muda, a nova entra por cima da antiga */
function StageArt({ i, ratio, pos, alt, children }) {
  const [cur, setCur] = useState(i);
  const [prev, setPrev] = useState(null);
  useEffect(() => {
    if (i === cur) return undefined;
    setPrev(cur); setCur(i);
    const tm = setTimeout(() => setPrev(null), 900);
    return () => clearTimeout(tm);
  }, [i]);
  const im = { position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", objectPosition: pos || "50% 62%" };
  return (
    <div style={{ position: "relative", aspectRatio: ratio || "3 / 4", overflow: "hidden", background: "#0b1626", border: `1px solid ${T.border}` }}>
      {prev != null && <img key={`p${prev}`} src={treeArt(prev)} alt="" style={im} />}
      <img key={`c${cur}`} src={treeArt(cur)} alt={alt || ""} decoding="async" className={prev != null ? "ts-in" : undefined} style={im} />
      {children}
    </div>
  );
}
function TreeStages({ state }) {
  const st = treeStageInfo(state);
  const N = TREE_STAGES.length;
  const [view, setView] = useState(null);
  const i = view == null ? st.i : view;
  const stripRef = useRef(null);
  useEffect(() => { try { localStorage.setItem("rb.tree.stage", String(st.i)); } catch (e) { /* sem armazenamento */ } setView(null); }, [st.i]);
  useEffect(() => {
    const el = stripRef.current;
    const b = el && el.children[st.i];
    if (b) el.scrollLeft = Math.max(0, b.offsetLeft - el.clientWidth / 2 + b.clientWidth / 2);
  }, [st.i]);
  const into = st.last ? 100 : Math.max(0, Math.min(100, ((st.pct - st.i * 5) / 5) * 100));
  const preview = i !== st.i;
  return (
    <Section title="Árvore · estágio da rota">
      <StageArt i={i} ratio="3 / 4" pos="50% 48%" alt={`Árvore no estágio ${i + 1} de ${N}: ${TREE_STAGES[i]}`}>
        <div style={{ position: "absolute", left: 10, top: 10, padding: "6px 10px", background: "rgba(4,8,16,.8)", border: `1px solid ${preview ? T.border : `${T.gold}99`}`, pointerEvents: "none" }}>
          <div style={{ fontSize: 11, fontWeight: 700, letterSpacing: "0.16em", color: preview ? T.muted : T.gold }}>{preview ? (i < st.i ? "JÁ PASSOU" : "PRÓXIMO") : "AGORA"} · {i + 1}/{N}</div>
          <div style={{ fontSize: 17, fontWeight: 700, color: "#f3f6fb" }}>{TREE_STAGES[i]}</div>
        </div>
        {preview && (
          <button onClick={() => setView(null)} style={{ position: "absolute", right: 10, top: 10, minHeight: 40, background: "rgba(4,8,16,.85)", border: `1px solid ${T.gold}`, color: T.gold, padding: "0 12px", fontWeight: 700, fontSize: 13.5, cursor: "pointer", fontFamily: F.ui }}>Voltar ao atual</button>
        )}
        <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, padding: "34px 12px 11px", background: "linear-gradient(rgba(4,8,16,0), rgba(4,8,16,.92))", pointerEvents: "none" }}>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 13.5, color: "#dfe7f1", marginBottom: 6 }}>
            <span>Rota <b style={{ color: T.gold }}>{st.pct}%</b></span>
            <span>{st.last ? "último estágio" : `estágio ${st.i + 2} em ${st.nextAt}%`}</span>
          </div>
          <CxBar pct={into} color={T.gold} h={6} />
        </div>
      </StageArt>
      <div style={{ fontSize: 13.5, color: T.muted, lineHeight: 1.45, margin: "8px 2px 0" }}>
        {st.last ? "A árvore chegou ao último estágio." : (<>Faltam <b style={{ color: T.text }}>{Math.max(1, st.nextAt - st.pct)}%</b> de rota para o próximo estágio{st.marcos ? <> (≈ {st.marcos} marco{st.marcos > 1 ? "s" : ""})</> : null}. O % vem dos marcos dos objetivos finais.</>)}
      </div>
      <div ref={stripRef} role="group" aria-label="Os 20 estágios da árvore" style={{ display: "flex", gap: 4, overflowX: "auto", padding: "10px 1px 4px", scrollbarWidth: "thin" }}>
        {TREE_STAGES.map((nm, k) => {
          const open = k <= st.i, next = k === st.i + 1, can = open || next;
          return (
            <button key={k} onClick={() => can && setView(k === st.i ? null : k)} disabled={!can} aria-pressed={k === i}
              aria-label={`Estágio ${k + 1}: ${nm}, a partir de ${k * 5}%${open ? "" : next ? ", o próximo" : ", trancado"}`}
              style={{ flex: "0 0 46px", height: 82, padding: 0, position: "relative", overflow: "hidden", background: "#0b1626", border: `1px solid ${k === i ? T.gold : k === st.i ? `${T.gold}77` : T.border}`, cursor: can ? "pointer" : "default" }}>
              <img src={treeThumb(k)} alt="" loading="lazy" style={{ display: "block", width: "100%", height: "100%", objectFit: "cover", filter: open ? "none" : next ? "grayscale(.6) brightness(.55)" : "grayscale(1) brightness(.22)" }} />
              <span style={{ position: "absolute", left: 0, right: 0, bottom: 0, fontSize: 10.5, fontWeight: 700, lineHeight: "16px", textAlign: "center", color: open ? "#f3f6fb" : "#9aa7b8", background: "rgba(4,8,16,.72)" }}>{open ? k + 1 : `🔒${k * 5}%`}</span>
            </button>
          );
        })}
      </div>
    </Section>
  );
}

/* ==================== ÁRVORE DE EVOLUÇÃO ==================== */'''
rep('''/* ==================== ÁRVORE DE EVOLUÇÃO ==================== */''', STAGES)

# ---------- Perfil: o painel dos estágios logo acima da árvore de galhos ----------
rep('''      <div id="arvore" style={{ scrollMarginTop: 70 }} />
''', '''      <div id="arvore" style={{ scrollMarginTop: 70 }} />
      <TreeStages state={state} />
''')

# ---------- Status: o card mostra a pintura do estágio ----------
rep('''function TreeCard({ state, onOpen }) {
  const wx = treeWeather(state);''', '''function TreeCard({ state, onOpen }) {
  const st = treeStageInfo(state);
  const [seen, setSeen] = useState(() => { try { const v = localStorage.getItem("rb.tree.stage"); return v == null ? null : Number(v); } catch (e) { return null; } });
  const keep = (n) => { try { localStorage.setItem("rb.tree.stage", String(n)); } catch (e) { /* sem armazenamento */ } setSeen(n); };
  /* primeira vez ou estágio que voltou (marco desmarcado): só guarda, sem aviso */
  useEffect(() => { if (seen == null || st.i < seen) keep(st.i); }, [st.i]);
  const grew = seen != null && st.i > seen;
  const wx = treeWeather(state);''')
rep('''    <Section title="Árvore do Renascimento">
      <div style={{ display: "flex", gap: 12, alignItems: "stretch" }}>
        <div style={{ flex: "0 0 44%" }}><RebirthTree state={state} compact onOpen={onOpen} /></div>''', '''    <Section title="Árvore do Renascimento">
      {grew && (
        <div role="status" className="cx-rise" style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 10, padding: "9px 11px", border: `1px solid ${T.gold}`, background: "rgba(255,201,92,.08)", boxShadow: `0 0 18px ${T.gold}33` }}>
          <span style={{ flex: 1, fontSize: 14.5, lineHeight: 1.35 }}><b style={{ color: T.gold }}>◆ A árvore cresceu.</b> Estágio {st.i + 1} de {TREE_STAGES.length}: {st.name}.</span>
          <button onClick={() => keep(st.i)} style={{ minHeight: 40, background: "none", border: `1px solid ${T.border}`, color: T.muted, padding: "0 12px", cursor: "pointer", fontFamily: F.ui, fontSize: 13.5 }}>Ok</button>
        </div>
      )}
      <div style={{ display: "flex", gap: 12, alignItems: "stretch" }}>
        <div style={{ flex: "0 0 44%" }}>
          <button onClick={() => { keep(st.i); onOpen(); }} aria-label={`Abrir a árvore. Estágio ${st.i + 1} de ${TREE_STAGES.length}: ${st.name}`}
            style={{ display: "block", width: "100%", padding: 0, border: "none", background: "none", cursor: "pointer" }}>
            <StageArt i={st.i} ratio="3 / 4" pos="50% 62%">
              <div style={{ position: "absolute", left: 0, right: 0, bottom: 0, padding: "22px 8px 7px", background: "linear-gradient(rgba(4,8,16,0), rgba(4,8,16,.9))", textAlign: "left" }}>
                <div style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.14em", color: T.gold }}>ESTÁGIO {st.i + 1}/{TREE_STAGES.length}</div>
                <div style={{ fontSize: 13.5, fontWeight: 700, color: "#f3f6fb", lineHeight: 1.15 }}>{st.name}</div>
              </div>
            </StageArt>
          </button>
        </div>''')
rep('''            {row("Tronco", `dia ${Math.min(p.day, p.days)}/${p.days}`)}''',
    '''            {row("Rota", `${st.pct}% · dia ${Math.min(p.day, p.days)}`, T.gold)}
            {row("Próximo", st.last ? "máximo" : `${st.nextAt}%${st.marcos ? ` · ~${st.marcos} marco${st.marcos > 1 ? "s" : ""}` : ""}`)}''')

open(p, 'w', encoding='utf-8').write(s)
print('ok', len(s))
