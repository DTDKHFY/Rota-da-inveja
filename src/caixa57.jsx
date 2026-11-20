/* ==================== CAIXA · CENTRO DE COMANDO (v57) ====================
   Modelo em claude/caixa-modelo.md (Project). Tudo aqui é cálculo sobre os dados que já existem
   (renda, contas fixas, cartões, dívidas, lançamentos); o único dado novo é finance.cmd. */
const cxId = () => Date.now() + Math.random();
const CMD_DEFAULTS = { incomeMode: "variavel", incomeFloor: 0, varGoal: 0, free: 0, hours: 180, bigLimit: 100, provisions: [], settled: [], debtPeak: 0 };
const cmdOf = (fin) => ({ ...CMD_DEFAULTS, ...((fin && fin.cmd) || {}) });
const CX_PHASES = [
  { id: 0, name: "Sangria", hint: "o pagamento não cobre os juros: estancar" },
  { id: 1, name: "Colchão", hint: "1 mês de reserva antes de atacar a dívida" },
  { id: 2, name: "Quitação", hint: "toda sobra vai para a dívida" },
  { id: 3, name: "Muralha", hint: "reserva de 6 meses" },
  { id: 4, name: "Ataque", hint: "investir e capital de trading" },
];
/* gerador estável: a previsão não muda a cada abertura do app */
function cxRng(seed) {
  let a = seed >>> 0;
  return () => { a = (a + 0x6d2b79f5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
}
const cxQuant = (arr, q) => { if (!arr.length) return 0; const s = [...arr].sort((a, b) => a - b); return s[Math.min(s.length - 1, Math.max(0, Math.floor(q * (s.length - 1))))]; };
const cxBrl0 = (n) => `R$ ${Math.round(n || 0).toLocaleString("pt-BR")}`;
const cxMonthLabel = (key) => `${monthShort(key)}/${key.slice(2, 4)}`;

/* DÍVIDA ≠ FATURA. Dívida é o saldo que gera juros: rotativo (o que sobrou de fatura paga em parte),
   parcelamento de fatura, dívida antiga cadastrada e fatura vencida sem pagamento (vira rotativo).
   Compra nova no cartão é gasto do mês e compromisso da fatura: só vira dívida se não for paga inteira. */
function cxDebts(fin, todayKey) {
  const list = [];
  let faturaMes = 0, commitFuture = 0;
  const monthEnd = `${todayKey.slice(0, 7)}-${String(daysIn(todayKey.slice(0, 7))).padStart(2, "0")}`;
  (fin.cards || []).forEach((c) => {
    const paid = c.paid || {};
    const sm = cardSummary(fin, c, todayKey);
    const byInv = {};
    sm.invoices.forEach((i) => { byInv[i.key] = i; });
    const unpaid = fin.entries.filter((e) => e.cardId === c.id && !e.cardPayment && paid[e.invoice] == null);
    const debtEntries = unpaid.filter((e) => e.oldDebt || e.carry);
    const inv = {};
    let bal = 0;
    debtEntries.forEach((e) => { bal += e.amount; inv[e.invoice] = round2((inv[e.invoice] || 0) + e.amount); });
    /* compras normais: na fatura vencida viram dívida; nas outras, são compromisso */
    const normalByInv = {};
    unpaid.filter((e) => !e.oldDebt && !e.carry).forEach((e) => { normalByInv[e.invoice] = round2((normalByInv[e.invoice] || 0) + e.amount); });
    Object.keys(normalByInv).forEach((k) => {
      const adv = (byInv[k] && byInv[k].advanced) || 0;
      const v = Math.max(0, round2(normalByInv[k] - adv));
      if (!(v > 0)) return;
      const i = byInv[k];
      if (i && i.overdue) { bal += v; inv[k] = round2((inv[k] || 0) + v); }
      else if (i && i.due <= monthEnd) faturaMes = round2(faturaMes + v);
      else commitFuture = round2(commitFuture + v);
    });
    bal = round2(bal);
    if (bal > 0.005) list.push({ id: `card:${c.id}`, name: c.name, balance: bal, rate: c.rate == null ? null : Number(c.rate), card: true, inv });
  });
  (fin.debts || []).filter((d) => d.balance > 0.005).forEach((d) => list.push({ id: d.id, name: d.name, balance: d.balance, rate: d.rate == null ? null : Number(d.rate), payment: d.payment || 0, inv: { [todayKey.slice(0, 7)]: d.balance } }));
  return { list: list.map((d) => ({ ...d, interestMonth: round2(d.balance * ((d.rate || 0) / 100)) })), faturaMes, commitFuture };
}

/* quitação só da dívida: a cada mês paga o orçamento da dívida, maior juro primeiro; o que sobra rende juros */
function cxSimDebt(debts, startKey, budget) {
  const cs = debts.map((d) => {
    const inv = {};
    Object.keys(d.inv || {}).forEach((k) => { const kk = k < startKey ? startKey : k; inv[kk] = round2((inv[kk] || 0) + d.inv[k]); });
    return { r: Math.max(0, (d.rate || 0) / 100), inv, carry: 0, last: Object.keys(inv).sort().pop() || startKey };
  });
  if (!cs.length) return { months: 0, interest: 0, end: startKey };
  if (!(budget > 0)) return { stuck: true };
  const lastKey = cs.map((c) => c.last).sort().pop();
  let key = startKey, months = 0, interest = 0, prev = Infinity;
  for (;;) {
    months++;
    if (months > 240) return { stuck: true };
    cs.forEach((c) => { c.due = round2(c.carry + (c.inv[key] || 0)); });
    let b = budget;
    [...cs].sort((a, z) => z.r - a.r).forEach((c) => { const p = Math.min(c.due, b); c.due = round2(c.due - p); b = round2(b - p); });
    cs.forEach((c) => { const i = round2(c.due * c.r); interest += i; c.carry = round2(c.due + i); });
    const carry = cs.reduce((a, c) => a + c.carry, 0);
    if (key >= lastKey && carry <= 0.005) break;
    if (key >= lastKey) { if (carry >= prev - 0.005) return { stuck: true }; prev = carry; }
    key = addMonthsKey(key, 1);
  }
  return { months, interest: round2(interest), end: key };
}

/* gasto variável por dia (inclui o "livre sem culpa") */
function cxDailyVar(fin) {
  const m = {};
  fin.entries.forEach((e) => { if (isVariableOut(e)) m[e.date] = round2((m[e.date] || 0) + e.amount); });
  return m;
}

function caixaModel(fin, todayKey) {
  const cmd = cmdOf(fin);
  const mk = todayKey.slice(0, 7);
  const day = Number(todayKey.slice(8));
  const last = daysIn(mk);
  const plan = fin.plan || {};
  const monthEntries = fin.entries.filter((e) => e.date.slice(0, 7) === mk);
  const inMonth = round2(sum(monthEntries.filter((e) => e.type === "in")));
  /* renda variável: o plano usa a renda de segurança (menor dos últimos 3 meses ou o mínimo informado);
     o que entrar acima disso é extra: 80% para a fase, 20% para o livre */
  const incomeHist = [1, 2, 3].map((n) => { const k = addMonthsKey(mk, -n); return { k, v: round2(sum(fin.entries.filter((e) => e.type === "in" && e.date.slice(0, 7) === k))) }; }).filter((x) => x.v > 0);
  let income, incomeSrc;
  if (cmd.incomeMode === "fixa") { income = plan.income || 0; incomeSrc = "renda fixa informada"; }
  else if (cmd.incomeFloor > 0) { income = cmd.incomeFloor; incomeSrc = "mínimo garantido que você informou"; }
  else if (incomeHist.length) { const lo = incomeHist.reduce((a, x) => (x.v < a.v ? x : a)); income = lo.v; incomeSrc = `menor dos últimos ${incomeHist.length === 1 ? "mês" : `${incomeHist.length} meses`} (${monthShort(lo.k)})`; }
  else { income = plan.income || 0; incomeSrc = income > 0 ? "estimativa que você informou" : ""; }
  const extra = cmd.incomeMode === "fixa" ? 0 : round2(Math.max(0, inMonth - income));
  const extraPhase = round2(extra * 0.8), extraFree = round2(extra - extraPhase);
  const fixed = round2((fin.recurring || []).filter((r) => r.type === "out").reduce((a, r) => a + r.amount, 0));
  const fixedPaid = round2(sum(monthEntries.filter((e) => e.recId && e.type === "out")));
  const prov = round2((cmd.provisions || []).reduce((a, p) => a + (Number(p.amount) || 0), 0) / 12);
  const mp = monthPlan(fin, mk, todayKey);
  const dd = cxDebts(fin, todayKey);
  const debts = dd.list;
  const debtTotal = round2(debts.reduce((a, d) => a + d.balance, 0));
  const interestMonth = round2(debts.reduce((a, d) => a + d.interestMonth, 0));
  const debtBudget = round2(plan.debtBudget > 0 ? plan.debtBudget : mp.debtMin);
  const debtPaid = round2(sum(monthEntries.filter((e) => e.type === "out" && (e.cardPayment || e.debtId))));
  const reserveMonthly = plan.reserveMonthly || 0;
  const reserveIn = round2(monthEntries.filter((e) => e.type === "transfer" && (fin.accounts || []).some((a) => a.id === e.toId && a.kind === "caixinha")).reduce((a, e) => a + e.amount, 0));

  /* gasto típico: média dos últimos 3 meses com registro; meta ambiciosa = típico − 20% */
  const pastVar = [1, 2, 3].map((n) => { const k = addMonthsKey(mk, -n); return sum(fin.entries.filter((e) => isVariableOut(e) && e.date.slice(0, 7) === k)); }).filter((v) => v > 0);
  const typical = pastVar.length ? round2(pastVar.reduce((a, v) => a + v, 0) / pastVar.length) : 0;
  const varCap = cmd.varGoal > 0 ? cmd.varGoal : (typical > 0 ? round2(typical * 0.8) : 0);
  const freeCap = round2((cmd.free || 0) + extraFree);
  const varUsed = round2(sum(monthEntries.filter((e) => isVariableOut(e) && !e.free)));
  const freeUsed = round2(sum(monthEntries.filter((e) => isVariableOut(e) && e.free)));
  const unassigned = round2(income - fixed - prov - debtBudget - reserveMonthly - varCap - freeCap);

  /* livre hoje: o que sobra de variáveis + livre, dividido pelos dias que faltam (em faixa, não exato) */
  const daysLeft = last - day + 1;
  const remaining = round2(Math.max(0, varCap - varUsed) + Math.max(0, freeCap - freeUsed));
  const perDay = varCap + freeCap > 0 ? remaining / daysLeft : null;
  const range = perDay == null ? null : [Math.floor(perDay * 0.8), Math.floor(perDay)];

  /* reserva e fôlego */
  const reserve = reserveSaved(fin);
  const essential = round2(fixed + debtBudget + (varCap || typical));
  const runway = essential > 0 ? reserve / essential : null;
  const fm = financeMetrics(fin, mk);
  const saveRate = fm.totalIn > 0 ? Math.round((fm.net / fm.totalIn) * 100) : null;

  /* quitação */
  let payoff = null;
  if (debtTotal > 0) {
    const sim = cxSimDebt(debts, mk, debtBudget);
    payoff = sim.stuck ? { stuck: true } : { months: sim.months, end: sim.end, interest: sim.interest };
    const alt = cxSimDebt(debts, mk, debtBudget + 200);
    payoff.plus200 = alt.stuck ? null : alt.months;
  }
  const peak = Math.max(cmd.debtPeak2 || 0, debtTotal);
  /* variação: saldo da dívida guardado mês a mês */
  const log = cmd.debtLog || {};
  const prevKey = Object.keys(log).filter((k) => k < mk).sort().pop();
  const debtDelta = prevKey != null ? round2(debtTotal - log[prevKey]) : null;
  /* fatura que vence no mês x o que sobra da renda depois de fixas, provisões, dívida e reserva */
  const faturaRoom = round2(income - fixed - prov - debtBudget - reserveMonthly);

  /* fase */
  let phase;
  if (debtTotal > 0) {
    if (interestMonth > 0 && debtBudget <= interestMonth) phase = 0;
    else if (essential > 0 && reserve < essential) phase = 1;
    else phase = 2;
  } else phase = runway != null && runway >= 6 ? 4 : 3;

  return { faturaMes: dd.faturaMes, commitFuture: dd.commitFuture, faturaRoom, debtLog: log, prevKey, debtDelta, cmd, mk, day, last, income, incomeSrc, incomeHist, extra, extraPhase, extraFree, inMonth, fixed, fixedPaid, prov, debts, debtTotal, interestMonth, costDay: round2(interestMonth / 30), debtBudget, debtPaid, reserveMonthly, reserveIn, typical, varCap, freeCap, varUsed, freeUsed, unassigned, daysLeft, remaining, range, reserve, essential, runway, saveRate, payoff, peak, phase };
}

/* previsão do fechamento: sorteia 2.000 vezes os seus dias reais de gasto para completar o mês */
function cxForecast(fin, todayKey, cap) {
  const daily = cxDailyVar(fin);
  const keys = Object.keys(daily).sort();
  if (!keys.length) return { ready: false, have: 0 };
  const first = keys[0] > addDays(todayKey, -60) ? keys[0] : addDays(todayKey, -60);
  const sample = [];
  for (let k = first; k < todayKey; k = addDays(k, 1)) sample.push(daily[k] || 0);
  if (sample.length < 10) return { ready: false, have: sample.length };
  const mk = todayKey.slice(0, 7);
  const day = Number(todayKey.slice(8)), last = daysIn(mk);
  const spent = round2(Object.keys(daily).filter((k) => k.slice(0, 7) === mk).reduce((a, k) => a + daily[k], 0));
  const rest = last - day;
  const rnd = cxRng(Number(mk.replace("-", "")) * 31 + day);
  const sims = [];
  for (let s = 0; s < 2000; s++) { let t = spent; for (let d = 0; d < rest; d++) t += sample[Math.floor(rnd() * sample.length)]; sims.push(t); }
  const over = cap > 0 ? sims.filter((t) => t > cap).length / sims.length : null;
  /* curva do mês até hoje */
  const curve = []; let acc = 0;
  for (let d = 1; d <= day; d++) { const k = `${mk}-${String(d).padStart(2, "0")}`; acc += daily[k] || 0; curve.push(round2(acc)); }
  return { ready: true, spent, p10: round2(cxQuant(sims, 0.1)), p50: round2(cxQuant(sims, 0.5)), p90: round2(cxQuant(sims, 0.9)), over, curve, day, last };
}

/* controle estatístico (gráfico XmR de Shewhart) por categoria, em semanas completas */
function cxControl(fin, todayKey) {
  const thisWeek = weekStartKey(parseKey(todayKey));
  const weeks = []; for (let i = 8; i >= 1; i--) weeks.push(addDays(thisWeek, -7 * i));
  const firstEntry = fin.entries.filter(isVariableOut).map((e) => e.date).sort()[0];
  if (!firstEntry) return { ready: false, weeksHave: 0, rows: [] };
  const usable = weeks.filter((w) => addDays(w, 6) >= firstEntry);
  if (usable.length < 6) return { ready: false, weeksHave: usable.length, rows: [] };
  const tot = {};
  fin.entries.filter((e) => isVariableOut(e) && e.date >= usable[0]).forEach((e) => { tot[e.cat] = (tot[e.cat] || 0) + e.amount; });
  const cats = Object.keys(tot).sort((a, b) => tot[b] - tot[a]).slice(0, 4);
  const rows = cats.map((cat) => {
    const series = usable.map((w) => round2(sum(fin.entries.filter((e) => isVariableOut(e) && e.cat === cat && e.date >= w && e.date <= addDays(w, 6)))));
    const base = series.slice(0, -1);
    const mean = base.reduce((a, v) => a + v, 0) / base.length;
    const mrs = base.slice(1).map((v, i) => Math.abs(v - base[i]));
    const mr = mrs.length ? mrs.reduce((a, v) => a + v, 0) / mrs.length : 0;
    const ucl = round2(mean + 2.66 * mr);
    const lastV = series[series.length - 1];
    const curWeek = round2(sum(fin.entries.filter((e) => isVariableOut(e) && e.cat === cat && e.date >= thisWeek && e.date <= todayKey)));
    const run = series.length >= 8 && series.slice(-8).every((v) => v > mean);
    const signal = lastV > ucl || curWeek > ucl || run;
    return { cat, series, mean: round2(mean), ucl, last: lastV, curWeek, signal, why: curWeek > ucl ? "esta semana" : lastV > ucl ? "semana passada" : run ? "8 semanas acima da média" : "" };
  });
  return { ready: true, rows };
}

function cxPareto(fin, todayKey) {
  const from = addDays(todayKey, -30);
  const tot = {};
  fin.entries.filter((e) => isVariableOut(e) && e.date > from).forEach((e) => { tot[e.cat] = (tot[e.cat] || 0) + e.amount; });
  const all = Object.values(tot).reduce((a, v) => a + v, 0);
  if (!(all > 0)) return null;
  const top = Object.keys(tot).sort((a, b) => tot[b] - tot[a]).slice(0, 3);
  return { top, pct: Math.round((top.reduce((a, c) => a + tot[c], 0) / all) * 100), n: Object.keys(tot).length };
}

/* custo de oportunidade de um gasto */
function cxOpportunity(fin, todayKey, value) {
  const m = caixaModel(fin, todayKey);
  if (!(value > 0)) return null;
  const hourly = m.income > 0 ? m.income / (m.cmd.hours || 180) : 0;
  const hours = hourly > 0 ? value / hourly : null;
  let delay = null, delayWhat = "";
  if (m.debtTotal > 0 && m.debtBudget > 0) { delay = value / (m.debtBudget / 30); delayWhat = "a quitação"; }
  else if (m.reserveMonthly > 0) { delay = value / (m.reserveMonthly / 30); delayWhat = "a reserva"; }
  const after = m.varCap + m.freeCap > 0 ? Math.max(0, m.remaining - value) / m.daysLeft : null;
  return { hours, delay, delayWhat, after: after == null ? null : [Math.floor(after * 0.8), Math.floor(after)], big: value >= (m.cmd.bigLimit || 100) };
}

/* ----- peças visuais ----- */
const cxGhost = { background: "transparent", get border() { return `1px solid ${T.accent}88`; }, get color() { return T.accentSoft; }, padding: "8px 14px", fontWeight: 700, fontSize: 14, fontFamily: "inherit", cursor: "pointer" };
const cxMono = { fontFamily: "'JetBrains Mono', ui-monospace, monospace", fontVariantNumeric: "tabular-nums" };
const cxLbl = { fontSize: 11, fontWeight: 700, letterSpacing: "0.16em", textTransform: "uppercase", get color() { return T.muted; } };
function CxBar({ pct, color, h = 8 }) {
  return (
    <div style={{ height: h, background: T.track, border: `1px solid ${T.border}`, overflow: "hidden" }}>
      <div className="cx-fill" style={{ width: `${Math.max(0, Math.min(100, pct))}%`, height: "100%", background: color }} />
    </div>
  );
}
function CxTile({ label, value, sub, color, pct, pulse, delay, small }) {
  return (
    <div data-pn="" className="cx-rise" style={{ animationDelay: `${delay || 0}s`, border: `1px solid ${T.border}`, background: T.panel, padding: "10px 11px", minWidth: 0 }}>
      <div style={cxLbl}>{label}</div>
      <div className={pulse ? "cx-pulse" : ""} style={{ ...cxMono, fontSize: small ? 16 : 20, fontWeight: 700, color, marginTop: 3, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{value}</div>
      <div style={{ fontSize: 12.5, color: T.muted, marginTop: 1 }}>{sub}</div>
      {pct != null && <div style={{ marginTop: 7 }}><CxBar pct={pct} color={color} h={6} /></div>}
    </div>
  );
}
function CxFold({ id, title, children, defaultOpen }) {
  const key = `rb.cx.${id}`;
  const [open, setOpen] = useState(() => { try { const v = localStorage.getItem(key); if (v != null) return v === "1"; return localStorage.getItem("rb.foldsOpen") === "1" || !!defaultOpen; } catch (e) { return !!defaultOpen; } });
  const toggle = () => { const n = !open; setOpen(n); try { localStorage.setItem(key, n ? "1" : "0"); } catch (e) { /* sem armazenamento */ } };
  return (
    <section style={{ marginTop: 18 }}>
      <button onClick={toggle} aria-expanded={open} data-pn=""
        style={{ width: "100%", minHeight: 48, display: "flex", alignItems: "center", gap: 10, padding: "0 14px", background: T.panel, border: `1px solid ${T.border}`, color: T.accentSoft, fontFamily: F.ui, fontSize: 13.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", cursor: "pointer", textAlign: "left" }}>
        <span aria-hidden="true" style={{ width: 7, height: 7, background: T.accent, transform: "rotate(45deg)", flex: "none" }} />
        <span style={{ flex: 1 }}>{title}</span>
        <span aria-hidden="true" style={{ transition: "transform .2s", transform: open ? "rotate(90deg)" : "none", fontSize: 16 }}>›</span>
      </button>
      {open && <div>{children}</div>}
    </section>
  );
}

/* ----- 1. COMANDO ----- */
function CaixaComando({ m, onSetup }) {
  if (!(m.income > 0)) {
    return (
      <Section title="Comando" accent={T.accent}>
        <div style={{ fontSize: 15, lineHeight: 1.45 }}>{m.cmd.incomeMode === "fixa"
          ? <>Informe a sua <b>renda líquida do mês</b> para o Caixa calcular quanto você pode gastar por dia, o custo da dívida e a sua fase.</>
          : <>Sua renda varia, então o plano usa a <b>renda de segurança</b>: o menor valor dos últimos 3 meses. Registre as entradas (salário, extras) no <b>+</b> ou informe agora o mínimo que entra com certeza.</>}</div>
        <button onClick={onSetup} style={{ ...btnPrimary, marginTop: 12, width: "100%", minHeight: 46 }}>Configurar o comando</button>
      </Section>
    );
  }
  const ph = CX_PHASES[m.phase];
  const alert = m.faturaMes > 0.005 && m.income > 0 && m.faturaMes > m.faturaRoom + 0.005 ? { text: `A fatura deste mês (${brl(m.faturaMes)}) passa do que sobra da renda depois de fixas, dívida e reserva (${brl(Math.max(0, m.faturaRoom))}). O que não for pago vira rotativo (média do Banco Central: 15,29% ao mês).`, red: true }
    : m.unassigned < -0.005 ? { text: `O plano passa da renda em ${brl(-m.unassigned)}. Corte um envelope.`, red: true }
    : m.phase === 0 ? { text: `O pagamento da dívida (${brl(m.debtBudget)}/mês) não cobre os juros (${brl(m.interestMonth)}/mês). A dívida não diminui.`, red: true }
    : m.extraPhase > 0.005 ? { text: `Entrou ${brl(m.extra)} acima da renda de segurança. Mande ${brl(m.extraPhase)} (80%) para ${m.debtTotal > 0 ? "a dívida" : "a reserva"}; ${brl(m.extraFree)} (20%) já foram para o livre.`, red: false }
    : m.unassigned > 0.005 ? { text: `${brl(m.unassigned)} da renda estão sem destino. Mande para ${m.debtTotal > 0 ? "a dívida" : "a reserva"}.`, red: false } : null;
  return (
    <section style={{ marginTop: 6 }}>
      <div data-pn="" className="cx-rise cx-breath" style={{ border: `1px solid ${T.border}`, background: T.panel, padding: "16px 14px 14px", textAlign: "center" }}>
        <div style={{ ...cxLbl, letterSpacing: "0.3em" }}>Livre hoje</div>
        {m.range ? (
          <div style={{ ...cxMono, fontSize: 38, fontWeight: 700, lineHeight: 1.05, marginTop: 6, color: m.remaining > 0 ? T.text : T.danger, textShadow: `0 0 18px ${T.accent}55` }}>
            {m.remaining > 0 ? (m.range[0] === m.range[1] ? `R$ ${m.range[1]}` : <>R$ {m.range[0]}<span style={{ color: T.accent, fontSize: 24 }}> – </span>{m.range[1]}</>) : "R$ 0"}
          </div>
        ) : <div style={{ fontSize: 15, marginTop: 8 }}>Defina a meta de variáveis em Ajustes.</div>}
        <div style={{ fontSize: 13.5, color: T.muted, marginTop: 6 }}>
          {m.range ? (m.remaining > 0 ? <>por dia até o fim do mês · {m.daysLeft} {m.daysLeft === 1 ? "dia" : "dias"} · sobra passa para o mês seguinte</> : "envelope de variáveis esgotado neste mês") : null}
        </div>
      </div>

      <div className="cx-rise" style={{ animationDelay: ".08s", display: "flex", gap: 2, margin: "12px 2px 4px" }} aria-label={`Fase atual: ${ph.name}`}>
        {CX_PHASES.map((p) => {
          const cur = p.id === m.phase, done = p.id < m.phase;
          const col = cur ? T.gold : done ? T.accent : T.muted;
          return (
            <div key={p.id} style={{ flex: 1, display: "flex", flexDirection: "column", alignItems: "center", gap: 5, minWidth: 0 }}>
              <span className={cur ? "cx-pulse" : ""} style={{ width: cur ? 13 : 9, height: cur ? 13 : 9, transform: "rotate(45deg)", background: cur || done ? col : "transparent", border: `1px solid ${col}`, boxShadow: cur ? `0 0 12px ${T.gold}` : "none" }} />
              <span style={{ fontSize: 10.5, fontWeight: 700, letterSpacing: "0.06em", color: col, textTransform: "uppercase" }}>{p.name}</span>
            </div>
          );
        })}
      </div>
      <div style={{ textAlign: "center", fontSize: 12.5, color: T.muted, marginBottom: 10 }}>{ph.hint}</div>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <CxTile label="Dívida custa" value={m.debtTotal > 0 ? `−${brl(m.costDay)}` : "R$ 0"} sub={m.debtTotal > 0 ? "por dia em juros" : "sem dívida"} color={m.debtTotal > 0 ? T.bad || T.danger : T.good} pulse={m.debtTotal > 0} delay={0.12} />
        <CxTile label="Quitação" value={m.debtTotal > 0 ? (m.payoff && !m.payoff.stuck ? cxMonthLabel(m.payoff.end) : "nunca") : "—"} sub={m.debtTotal > 0 ? (m.payoff && !m.payoff.stuck ? `${m.payoff.months} ${m.payoff.months === 1 ? "mês" : "meses"} no ritmo atual` : "pagamento ≤ juros") : "nada a quitar"} color={m.payoff && m.payoff.stuck ? T.danger : T.gold} pct={m.peak > 0 ? (1 - m.debtTotal / m.peak) * 100 : null} delay={0.18} />
        <CxTile label="Fôlego" value={m.runway == null ? "—" : `${m.runway.toFixed(1).replace(".", ",")} ${m.runway >= 1.95 ? "meses" : "mês"}`} sub="reserva ÷ custo essencial" color={T.accent} pct={m.runway == null ? null : (m.runway / 6) * 100} delay={0.24} />
        <CxTile label="Poupança" value={m.saveRate == null ? "—" : `${m.saveRate}%`} sub="da renda neste mês" color={m.saveRate != null && m.saveRate < 0 ? T.danger : T.good} pct={m.saveRate == null ? null : m.saveRate} delay={0.3} />
      </div>
      {alert && (
        <div data-pn="" className="cx-rise" role="status" style={{ animationDelay: ".36s", marginTop: 10, padding: "10px 12px", border: `1px solid ${alert.red ? T.danger : T.gold}88`, background: alert.red ? "rgba(255,83,104,.08)" : "rgba(255,201,92,.07)", fontSize: 14.5, lineHeight: 1.4 }}>
          {alert.text}
        </div>
      )}
    </section>
  );
}

/* ----- 2. INIMIGO ----- */
function CaixaInimigo({ m, finance, today, fin }) {
  const [adding, setAdding] = useState(false);
  const [f, setF] = useState({ name: "", saldo: "", rate: "", parcela: "", n: "" });
  const [msg, setMsg] = useState("");
  useEffect(() => {
    const upd = {};
    if (m.debtTotal > (m.cmd.debtPeak2 || 0) + 0.005) upd.debtPeak2 = m.debtTotal;
    if (m.debtLog[m.mk] == null || Math.abs(m.debtLog[m.mk] - m.debtTotal) > 0.005) upd.debtLog = { ...m.debtLog, [m.mk]: m.debtTotal };
    if (Object.keys(upd).length) fin.setCmd(upd);
  }, [m.debtTotal, m.mk]);
  const save = () => {
    const saldo = parseMoney(f.saldo), rate = parseMoney(f.rate), parcela = parseMoney(f.parcela), n = Math.round(Number(f.n));
    if (!f.name.trim()) { setMsg("Dê um nome, por exemplo Nubank."); return; }
    if (!(saldo > 0) && !(parcela > 0 && n > 0)) { setMsg("Informe o saldo devedor ou a parcela e quantas faltam."); return; }
    const id = cxId();
    const dd = Math.max(1, Math.min(28, Number(today.slice(8))));
    fin.addCard({ id, name: f.name.trim(), limit: saldo > 0 ? saldo : round2(parcela * n), closeDay: dd, dueDay: dd, rate: rate > 0 ? rate : null });
    fin.addOldDebt(id, parcela > 0 && n > 0 ? { mode: "parcelado", parcela, n, total: round2(parcela * n), note: f.name.trim() } : { mode: "saldo", saldo, note: f.name.trim() }, today);
    setF({ name: "", saldo: "", rate: "", parcela: "", n: "" }); setMsg(""); setAdding(false);
  };
  const lbl = (t, k, ph) => (
    <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13, color: T.muted, minWidth: 0 }}>{t}
      <input value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} inputMode={k === "name" ? "text" : "decimal"} placeholder={ph} style={{ ...inputStyle, width: "100%" }} />
    </label>
  );
  const form = adding && (
    <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
      <div style={{ gridColumn: "1 / -1" }}>{lbl("Nome da dívida", "name", "Nubank")}</div>
      {lbl("Saldo devedor hoje", "saldo", "R$ 0,00")}
      {lbl("Juros ao mês (%)", "rate", "9,37")}
      {lbl("Parcela (se parcelada)", "parcela", "R$ 0,00")}
      {lbl("Parcelas que faltam", "n", "0")}
      <div style={{ gridColumn: "1 / -1", fontSize: 12.5, color: T.muted }}>Os juros estão na fatura ou no app do banco (CET). Se não souber, deixe vazio: o Caixa avisa que falta.</div>
      {msg && <div role="alert" style={{ gridColumn: "1 / -1", color: T.danger, fontSize: 13.5 }}>{msg}</div>}
      <button onClick={save} style={{ ...btnPrimary, minHeight: 46 }}>Salvar dívida</button>
      <button onClick={() => { setAdding(false); setMsg(""); }} style={{ ...cxGhost, minHeight: 46 }}>Cancelar</button>
    </div>
  );
  if (!m.debts.length) {
    return (
      <Section title="Inimigo">
        <div style={{ fontSize: 14.5, color: T.muted }}>Nenhuma dívida aberta.{m.cmd.settled.length ? " Os inimigos derrotados estão logo abaixo." : ""}</div>
        {!adding && <button onClick={() => setAdding(true)} style={{ ...cxGhost, marginTop: 10, minHeight: 44 }}>+ Cadastrar dívida</button>}
        {form}
      </Section>
    );
  }
  const defeated = m.peak > 0 ? Math.round((1 - m.debtTotal / m.peak) * 100) : 0;
  const noRate = m.debts.filter((d) => !(d.rate > 0));
  /* caminho até zero: simulação simples com juros médios ponderados */
  const avgR = m.debtTotal > 0 ? m.debts.reduce((a, d) => a + d.balance * ((d.rate || 0) / 100), 0) / m.debtTotal : 0;
  const path = []; { let b = m.debtTotal; path.push(b); for (let i = 0; i < 36 && b > 0.005; i++) { b = b * (1 + avgR) - m.debtBudget; path.push(Math.max(0, b)); if (b >= path[0] * 1.5) break; } }
  const W = 330, H = 110;
  const pts = path.map((v, i) => `${i === 0 ? "M" : "L"} ${((i / Math.max(1, path.length - 1)) * W).toFixed(1)} ${(H - 8 - (Math.min(v, m.debtTotal) / m.debtTotal) * (H - 20)).toFixed(1)}`).join(" ");
  const reaches = path[path.length - 1] <= 0.005;
  const minToShrink = round2(m.interestMonth + 1);
  return (
    <Section title="Inimigo" accent={T.danger}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}>
        <span style={{ ...cxLbl, color: T.bad || T.danger, letterSpacing: "0.22em" }}>{m.debts.length === 1 ? m.debts[0].name : `${m.debts.length} dívidas`}</span>
        <span style={{ fontSize: 12.5, color: T.bad || T.danger }}>{avgR > 0 ? `${(avgR * 100).toFixed(2).replace(".", ",")}% ao mês` : "juros não informados"}</span>
      </div>
      <div className="cx-hit" style={{ ...cxMono, fontSize: 32, fontWeight: 700, marginTop: 6 }}>{brl(m.debtTotal)}</div>
      <div style={{ marginTop: 8 }}><CxBar pct={100 - defeated} color={`linear-gradient(90deg, #8E1F30, ${T.danger})`} h={12} /></div>
      <div style={{ display: "flex", justifyContent: "space-between", marginTop: 5, fontSize: 12.5, color: T.muted }}><span>maior saldo: {brl(m.peak)}</span><span style={cxMono}>−{defeated}% derrotado</span></div>
      {m.debtDelta != null && Math.abs(m.debtDelta) > 0.005 && (
        <div style={{ marginTop: 6, fontSize: 13.5 }}>Desde {monthShort(m.prevKey)}: <b style={{ ...cxMono, color: m.debtDelta > 0 ? T.danger : T.good }}>{m.debtDelta > 0 ? "subiu" : "caiu"} {brl(Math.abs(m.debtDelta))}</b></div>
      )}
      {(m.faturaMes > 0 || m.commitFuture > 0) && (
        <div style={{ marginTop: 6, fontSize: 12.5, color: T.muted, lineHeight: 1.4 }}>Fora da dívida (compras do cartão, sem juros se pagas inteiras): fatura do mês {brl(m.faturaMes)}{m.commitFuture > 0 ? ` · parcelas futuras ${brl(m.commitFuture)}` : ""}.</div>
      )}

      <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 7, marginTop: 12 }}>
        <CxTile small label="Por dia" value={brl(m.costDay)} sub="em juros" color={T.bad || T.danger} pulse />
        <CxTile small label="Por mês" value={cxBrl0(m.interestMonth)} sub="em juros" color={T.bad || T.danger} />
        <CxTile small label="Pagando" value={cxBrl0(m.debtBudget)} sub="por mês" color={T.text} />
      </div>

      {avgR > 0 && m.debtBudget > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={{ ...cxLbl, marginBottom: 6 }}>Caminho até zero</div>
          <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={reaches ? `Saldo chega a zero em ${path.length - 1} meses` : "Saldo não chega a zero com o pagamento atual"}>
            <line x1="0" y1={H - 8} x2={W} y2={H - 8} stroke={T.border} />
            <path className="cx-draw" d={pts} fill="none" stroke={T.danger} strokeWidth="2.5" strokeLinecap="round" pathLength="1000" />
            {reaches && <circle cx={W} cy={H - 8} r="5" fill={T.gold} className="cx-pulse" />}
          </svg>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 11.5, color: T.muted, ...cxMono }}>
            <span>{cxMonthLabel(m.mk)}</span><span>{reaches ? cxMonthLabel(addMonthsKey(m.mk, path.length - 1)) : "não zera"}</span>
          </div>
        </div>
      )}

      <div style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 6, fontSize: 14.5 }}>
        {m.payoff && m.payoff.plus200 != null && !m.payoff.stuck && m.payoff.months - m.payoff.plus200 > 0 && (
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><span>Pagando +R$ 200/mês</span><b style={{ ...cxMono, color: T.good }}>{m.payoff.months - m.payoff.plus200} {m.payoff.months - m.payoff.plus200 === 1 ? "mês" : "meses"} antes</b></div>
        )}
        {m.interestMonth > 0 && (
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><span>Mínimo para a dívida cair</span><b style={{ ...cxMono, color: m.debtBudget <= m.interestMonth ? T.danger : T.text }} className={m.debtBudget <= m.interestMonth ? "cx-pulse" : ""}>{brl(minToShrink)}/mês</b></div>
        )}
        {m.payoff && !m.payoff.stuck && m.payoff.interest > 0 && (
          <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}><span>Juros até quitar</span><b style={{ ...cxMono, color: T.bad || T.danger }}>{brl(m.payoff.interest)}</b></div>
        )}
      </div>
      {noRate.length > 0 && <div style={{ marginTop: 10, fontSize: 13, color: T.gold }}>Sem juros informados: {noRate.map((d) => d.name).join(", ")}. Informe no cartão (Arquivo → Cartões) para o custo ficar certo.</div>}
      {m.debts.length > 1 && (
        <div style={{ marginTop: 10, fontSize: 13.5, color: T.muted }}>Quem atacar primeiro (maior juro): {[...m.debts].sort((a, b) => (b.rate || 0) - (a.rate || 0)).map((d) => d.name).join(" → ")}</div>
      )}
      <div style={{ marginTop: 10, fontSize: 12, color: T.muted, lineHeight: 1.4 }}>Dívida = saldo que gera juros: rotativo, parcelamento, dívida antiga e fatura vencida. Compra nova no cartão é gasto do mês; só vira dívida se a fatura não for paga inteira.</div>
      {!adding && <button onClick={() => setAdding(true)} style={{ ...cxGhost, marginTop: 12, minHeight: 44 }}>+ Cadastrar outra dívida</button>}
      {form}
    </Section>
  );
}

/* ----- 3. CASCATA ----- */
function CaixaCascata({ m, onSetup }) {
  if (!(m.income > 0)) return null;
  const rows = [
    { k: "Fixas", cap: m.fixed, used: m.fixedPaid, col: T.accentSoft, sub: m.fixed > 0 ? "contas do mês" : "cadastre em Arquivo → Contas fixas" },
    { k: "Provisões", cap: m.prov, used: null, col: T.accentSoft, sub: m.prov > 0 ? "gastos do ano ÷ 12" : "IPVA, manutenção, presentes: em Ajustes" },
    { k: "Inimigo", cap: m.debtBudget, used: m.debtPaid, col: T.danger, sub: m.debtTotal > 0 ? "pagamento da dívida" : "sem dívida" },
    { k: "Reserva", cap: m.reserveMonthly, used: m.reserveIn, col: T.accent, sub: m.phase === 2 ? "no mínimo na fase de quitação" : "para a Caixinha" },
    { k: "Variáveis", cap: m.varCap, used: m.varUsed, col: T.gold, sub: m.varCap > 0 ? `${brl(Math.max(0, m.varCap - m.varUsed))} restam${m.cmd.varGoal > 0 ? "" : " · meta automática: típico − 20%"}` : "defina a meta em Ajustes" },
    { k: "Livre sem culpa", cap: m.freeCap, used: m.freeUsed, col: T.good, sub: m.freeCap > 0 ? `${brl(Math.max(0, m.freeCap - m.freeUsed))} restam` : "valor pequeno e fixo: em Ajustes" },
  ];
  return (
    <Section title="Cascata do mês">
      <div data-pn="" className="cx-rise" style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, padding: "11px 13px", border: `1px solid ${T.border}`, background: T.panel }}>
        <div style={{ minWidth: 0 }}><div style={cxLbl}>{m.cmd.incomeMode === "fixa" ? "Renda do mês" : "Renda de segurança"}</div><div style={{ ...cxMono, fontSize: 22, fontWeight: 700 }}>{brl(m.income)}</div><div style={{ fontSize: 12, color: T.muted }}>{m.incomeSrc ? `${m.incomeSrc} · ` : ""}entrou: {brl(m.inMonth)}{m.extra > 0 ? ` · extra ${brl(m.extra)}` : ""}</div></div>
        <div style={{ textAlign: "right" }}><div style={cxLbl}>Sem destino</div><div style={{ ...cxMono, fontSize: 22, fontWeight: 700, color: Math.abs(m.unassigned) < 0.01 ? T.good : m.unassigned < 0 ? T.danger : T.gold }}>{Math.abs(m.unassigned) < 0.01 ? "R$ 0 ✓" : brl(m.unassigned)}</div></div>
      </div>
      <div style={{ position: "relative", marginTop: 12, paddingLeft: 30 }}>
        <div aria-hidden="true" style={{ position: "absolute", left: 10, top: 4, bottom: 4, width: 2, background: `linear-gradient(${T.accent}, ${T.accent}22)` }} />
        <span aria-hidden="true" className="cx-flow" style={{ position: "absolute", left: 8, top: 0, width: 6, height: 6, borderRadius: "50%", background: "#E6FBFF", boxShadow: `0 0 10px ${T.accent}` }} />
        <span aria-hidden="true" className="cx-flow" style={{ animationDelay: "1.6s", position: "absolute", left: 8, top: 0, width: 6, height: 6, borderRadius: "50%", background: "#E6FBFF", boxShadow: `0 0 10px ${T.accent}` }} />
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {rows.map((r, i) => (
            <div key={r.k} className="cx-rise" style={{ animationDelay: `${0.05 * i}s`, position: "relative" }}>
              <span aria-hidden="true" style={{ position: "absolute", left: -25, top: 3, width: 11, height: 11, transform: "rotate(45deg)", border: `1px solid ${r.col}`, background: `${r.col}33` }} />
              <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "baseline" }}>
                <span style={{ fontSize: 13, fontWeight: 700, letterSpacing: "0.14em", textTransform: "uppercase", color: r.col }}>{r.k}</span>
                <span style={{ ...cxMono, fontSize: 12.5, color: T.text }}>{r.used != null ? `${brl(r.used)} / ` : ""}{brl(r.cap)}</span>
              </div>
              {r.cap > 0 && <div style={{ marginTop: 5 }}><CxBar pct={r.used != null ? (r.used / r.cap) * 100 : 100} color={r.used != null && r.used > r.cap ? T.danger : r.col} /></div>}
              <div style={{ fontSize: 12, color: T.muted, marginTop: 3 }}>{r.sub}</div>
            </div>
          ))}
        </div>
      </div>
      <p style={{ margin: "12px 0 0", fontSize: 12.5, color: T.muted, lineHeight: 1.4 }}>A ordem é fixa: a renda desce pela cascata até não sobrar nada sem destino.</p>
      <button onClick={onSetup} style={{ ...cxGhost, marginTop: 10, minHeight: 44 }}>Ajustar envelopes</button>
    </Section>
  );
}

/* ----- 4. PREVISÃO E CONTROLE ----- */
function CaixaPrevisao({ m, finance, today }) {
  const cap = m.varCap + m.freeCap;
  const fc = cxForecast(finance, today, cap);
  const ctl = cxControl(finance, today);
  const par = cxPareto(finance, today);
  const W = 330, H = 130;
  let chart = null;
  if (fc.ready) {
    const top = Math.max(cap || 0, fc.p90) * 1.1 || 1;
    const X = (d) => (d / fc.last) * W, Y = (v) => H - 10 - (v / top) * (H - 20);
    const real = fc.curve.map((v, i) => `${i === 0 ? "M" : "L"} ${X(i + 1).toFixed(1)} ${Y(v).toFixed(1)}`).join(" ");
    const x0 = X(fc.day), y0 = Y(fc.spent);
    chart = (
      <svg viewBox={`0 0 ${W} ${H}`} width="100%" role="img" aria-label={`Fechamento provável entre ${brl(fc.p10)} e ${brl(fc.p90)}`}>
        {cap > 0 && <><line x1="0" x2={W} y1={Y(cap)} y2={Y(cap)} stroke={T.gold} strokeDasharray="4 4" /><text x="4" y={Y(cap) - 5} fill={T.gold} fontSize="10" fontWeight="700" fontFamily="Rajdhani, sans-serif">ENVELOPE {brl(cap)}</text></>}
        <path className="cx-band" d={`M ${x0} ${y0} L ${W} ${Y(fc.p90)} L ${W} ${Y(fc.p10)} Z`} fill={T.accent} />
        <path d={`M ${x0} ${y0} L ${W} ${Y(fc.p50)}`} stroke={T.accentSoft} strokeDasharray="3 4" fill="none" />
        <path className="cx-draw" d={real} stroke={T.accent} strokeWidth="2.5" fill="none" strokeLinecap="round" pathLength="1000" />
        <circle cx={x0} cy={y0} r="4.5" fill="#E6FBFF" className="cx-pulse" />
      </svg>
    );
  }
  return (
    <Section title="Previsão e controle">
      {fc.ready ? (
        <>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", gap: 8 }}><span style={{ fontSize: 14, color: T.muted }}>Variáveis devem fechar entre</span><span style={{ ...cxMono, fontSize: 18, fontWeight: 700, whiteSpace: "nowrap" }}>{cxBrl0(fc.p10)} – {cxBrl0(fc.p90)}</span></div>
          {chart}
          {fc.over != null && (
            <div data-pn="" style={{ marginTop: 6, padding: "8px 10px", display: "flex", justifyContent: "space-between", alignItems: "center", border: `1px solid ${fc.over >= 0.5 ? T.danger : fc.over >= 0.2 ? T.gold : T.good}88`, background: T.panel }}>
              <span style={{ fontSize: 13.5 }}>Chance de estourar o envelope</span><b style={{ ...cxMono, fontSize: 17, color: fc.over >= 0.5 ? T.danger : fc.over >= 0.2 ? T.gold : T.good }}>{Math.round(fc.over * 100)}%</b>
            </div>
          )}
        </>
      ) : <div style={{ fontSize: 14, color: T.muted }}>A previsão liga com 10 dias de registros de gasto ({fc.have || 0} até agora). Registrar todo dia, mesmo dia sem gasto, é o que alimenta os cálculos.</div>}

      <div style={{ ...cxLbl, marginTop: 16, marginBottom: 6 }}>Controle por categoria</div>
      {ctl.ready ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {ctl.rows.map((r) => {
            const w = 140, h = 30, mx = Math.max(r.ucl, ...r.series, r.curWeek) * 1.1 || 1;
            const p = r.series.map((v, i) => `${i === 0 ? "M" : "L"} ${((i / (r.series.length - 1)) * w).toFixed(1)} ${(h - (v / mx) * h).toFixed(1)}`).join(" ");
            return (
              <div key={r.cat} style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 140px 64px", gap: 8, alignItems: "center", minHeight: 40 }}>
                <span style={{ fontSize: 14, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.cat}</span>
                <svg viewBox={`-2 -3 ${w + 6} ${h + 6}`} width="140" height="34" aria-hidden="true">
                  <line x1="0" x2={w} y1={h - (r.ucl / mx) * h} y2={h - (r.ucl / mx) * h} stroke={T.gold} strokeDasharray="3 3" opacity=".7" />
                  <path className="cx-draw" d={p} fill="none" stroke={T.accent} strokeWidth="1.6" pathLength="1000" />
                  <circle cx={w} cy={h - (r.series[r.series.length - 1] / mx) * h} r="3.5" fill={r.signal ? T.danger : T.accent} className={r.signal ? "cx-pulse" : ""} />
                </svg>
                <span title={r.signal ? `Fora do padrão: ${r.why}` : "Variação normal"} style={{ fontSize: 11.5, fontWeight: 700, letterSpacing: "0.1em", color: r.signal ? T.danger : T.good }}>{r.signal ? "SINAL" : "RUÍDO"}</span>
              </div>
            );
          })}
          {ctl.rows.filter((r) => r.signal).map((r) => (
            <div key={r.cat} style={{ fontSize: 13.5, color: T.bad || T.danger }}>{r.cat}: {r.why} acima do limite natural de {brl(r.ucl)} por semana. Mudou o padrão, não é ruído.</div>
          ))}
          <div style={{ fontSize: 12, color: T.muted }}>Limite natural = média + 2,66 × variação média entre semanas. Só acende quando sai do padrão.</div>
        </div>
      ) : <div style={{ fontSize: 14, color: T.muted }}>Liga com 6 semanas de registros ({ctl.weeksHave || 0} até agora).</div>}

      {par && par.n > 3 && (
        <div data-pn="" style={{ marginTop: 14, padding: "10px 12px", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 10, border: `1px solid ${T.border}`, background: T.panel }}>
          <div style={{ minWidth: 0 }}><div style={cxLbl}>Onde cortar primeiro · 30 dias</div><div style={{ fontSize: 14, marginTop: 2 }}>{par.top.join(" + ")}</div></div>
          <b style={{ ...cxMono, fontSize: 22, color: T.gold }}>{par.pct}%</b>
        </div>
      )}
    </Section>
  );
}

/* ----- 5. DERROTADOS ----- */
function CaixaDerrotados({ m, fin, today }) {
  const [f, setF] = useState(null);
  const list = m.cmd.settled || [];
  const totOrig = round2(list.reduce((a, d) => a + (Number(d.orig) || 0), 0));
  const totPaid = round2(list.reduce((a, d) => a + (Number(d.paid) || 0), 0));
  const save = () => {
    const orig = parseMoney(f.orig), paid = parseMoney(f.paid);
    if (!f.name.trim() || !(orig > 0)) return;
    fin.setCmd({ settled: [...list, { id: cxId(), name: f.name.trim(), orig, paid: paid > 0 ? paid : orig, date: f.date || today, how: f.how.trim() }] });
    setF(null);
  };
  return (
    <Section title="Inimigos derrotados">
      {list.length ? (
        <>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <CxTile label="Dívida eliminada" value={brl(totOrig)} sub={`${list.length} ${list.length === 1 ? "inimigo" : "inimigos"}`} color={T.good} />
            <CxTile label="Juros pagos" value={brl(Math.max(0, totPaid - totOrig))} sub="o preço da lição" color={T.bad || T.danger} />
          </div>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 10 }}>
            {[...list].sort((a, b) => (a.date < b.date ? 1 : -1)).map((d) => (
              <div key={d.id} style={{ display: "grid", gridTemplateColumns: "auto 1fr auto auto", gap: 10, alignItems: "center", padding: "8px 0", borderBottom: `1px solid ${T.border}` }}>
                <span aria-hidden="true" style={{ width: 10, height: 10, transform: "rotate(45deg)", background: T.good }} />
                <div style={{ minWidth: 0 }}><div style={{ fontSize: 15, fontWeight: 600, textDecoration: "line-through", textDecorationColor: `${T.good}aa` }}>{d.name}</div><div style={{ fontSize: 12.5, color: T.muted }}>{d.date.slice(8)}/{d.date.slice(5, 7)}/{d.date.slice(2, 4)}{d.how ? ` · ${d.how}` : ""}</div></div>
                <span style={{ ...cxMono, fontSize: 13 }}>{brl(d.orig)}</span>
                <button aria-label={`Apagar ${d.name}`} onClick={() => fin.setCmd({ settled: list.filter((x) => x.id !== d.id) })} style={{ background: "none", border: "none", color: T.muted, fontSize: 18, minWidth: 40, minHeight: 40, cursor: "pointer" }}>×</button>
              </div>
            ))}
          </div>
        </>
      ) : <div style={{ fontSize: 14, color: T.muted }}>Registre as dívidas que você já quitou. É o histórico da guerra: quanto devia, quanto pagou e quanto de juros custou.</div>}
      {f ? (
        <div style={{ marginTop: 12, display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          {[["Nome", "name", "Cheque especial BB", "text"], ["Quanto devia", "orig", "R$ 0,00", "decimal"], ["Quanto pagou no total", "paid", "R$ 0,00", "decimal"], ["Como quitou", "how", "dinheiro, acordo, bem", "text"]].map(([t, k, ph, im]) => (
            <label key={k} style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13, color: T.muted, minWidth: 0 }}>{t}
              <input value={f[k]} onChange={(e) => setF({ ...f, [k]: e.target.value })} inputMode={im} placeholder={ph} style={{ ...inputStyle, width: "100%" }} />
            </label>
          ))}
          <label style={{ gridColumn: "1 / -1", display: "flex", flexDirection: "column", gap: 4, fontSize: 13, color: T.muted }}>Quando quitou
            <input type="date" value={f.date} max={today} onChange={(e) => setF({ ...f, date: e.target.value })} style={{ ...inputStyle, width: "100%", colorScheme: T.light ? "light" : "dark" }} />
          </label>
          <button onClick={save} style={{ ...btnPrimary, minHeight: 46 }}>Salvar</button>
          <button onClick={() => setF(null)} style={{ ...cxGhost, minHeight: 46 }}>Cancelar</button>
        </div>
      ) : <button onClick={() => setF({ name: "", orig: "", paid: "", how: "", date: today })} style={{ ...cxGhost, marginTop: 10, minHeight: 44 }}>+ Registrar dívida quitada</button>}
    </Section>
  );
}

/* ----- AJUSTES DO COMANDO ----- */
function CaixaAjustes({ m, finance, fin, onClose }) {
  const plan = finance.plan || {};
  const fmt = (v) => (v > 0 ? String(v).replace(".", ",") : "");
  const [mode, setMode] = useState(m.cmd.incomeMode || "variavel");
  const [v, setV] = useState({ floor: fmt(m.cmd.incomeFloor), income: fmt(plan.income), debt: fmt(plan.debtBudget), reserve: fmt(plan.reserveMonthly), varGoal: fmt(m.cmd.varGoal), free: fmt(m.cmd.free), hours: String(m.cmd.hours || 180), big: String(m.cmd.bigLimit || 100) });
  const [prov, setProv] = useState(m.cmd.provisions || []);
  const [np, setNp] = useState({ name: "", amount: "" });
  const [done, setDone] = useState("");
  const save = () => {
    const n = (k) => { const x = parseMoney(v[k]); return x > 0 ? x : 0; };
    fin.setPlan({ income: n("income"), debtBudget: n("debt"), reserveMonthly: n("reserve") });
    fin.setCmd({ incomeMode: mode, incomeFloor: n("floor"), varGoal: n("varGoal"), free: n("free"), hours: Math.max(1, Math.round(Number(v.hours) || 180)), bigLimit: Math.max(1, n("big") || 100), provisions: prov });
    setDone("Salvo."); if (onClose) setTimeout(onClose, 400);
  };
  const field = (t, k, hint) => (
    <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13.5, color: T.text, minWidth: 0 }}>{t}
      <input value={v[k]} onChange={(e) => setV({ ...v, [k]: e.target.value })} inputMode="decimal" placeholder="R$ 0,00" style={{ ...inputStyle, width: "100%" }} />
      {hint && <span style={{ fontSize: 12, color: T.muted }}>{hint}</span>}
    </label>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 12, marginTop: 12 }}>
      <div role="group" aria-label="Tipo de renda" style={{ display: "flex", gap: 6 }}>
        {[["variavel", "Renda variável"], ["fixa", "Renda fixa"]].map(([k, t]) => (
          <button key={k} onClick={() => setMode(k)} aria-pressed={mode === k} style={{ flex: 1, minHeight: 44, background: mode === k ? T.panel2 : "transparent", border: `1px solid ${mode === k ? T.accent : T.border}`, color: mode === k ? T.text : T.muted, fontFamily: F.ui, fontSize: 14, fontWeight: 700, cursor: "pointer" }}>{t}</button>
        ))}
      </div>
      {mode === "fixa"
        ? field("Renda líquida do mês", "income", "Italac + escritório + outras, já descontado")
        : <>
            {field("Mínimo que entra com certeza (opcional)", "floor", m.incomeHist.length ? `Últimos meses: ${m.incomeHist.map((x) => `${monthShort(x.k)} ${brl(x.v)}`).join(" · ")}. Vazio = usa o menor deles.` : "Ex.: salário-base. Vazio = usa o menor mês registrado.")}
            {!m.incomeHist.length && field("Estimativa enquanto não há histórico", "income", "Seja conservador: use um mês fraco.")}
          </>}
      {field("Pagamento da dívida por mês", "debt", m.interestMonth > 0 ? `Juros hoje: ${brl(m.interestMonth)}/mês. Abaixo disso a dívida não cai.` : "Quanto vai para a dívida todo mês")}
      {field("Reserva por mês", "reserve", m.debtTotal > 0 ? "Com dívida de cartão: pouco, só até 1 mês de custo." : "Para a Caixinha")}
      {field("Meta de variáveis no mês", "varGoal", m.typical > 0 ? `Seu típico: ${brl(m.typical)}. Meta ambiciosa: ${brl(round2(m.typical * 0.8))} (−20%). Vazio = automático.` : "Mercado, combustível, comida fora. Vazio = automático quando houver histórico.")}
      {field("Livre sem culpa no mês", "free", "Pequeno e fixo. Cortar tudo gera rebote.")}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13.5, minWidth: 0 }}>Horas de trabalho no mês
          <input value={v.hours} onChange={(e) => setV({ ...v, hours: e.target.value })} inputMode="numeric" style={{ ...inputStyle, width: "100%" }} />
        </label>
        <label style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 13.5, minWidth: 0 }}>Gasto grande a partir de
          <input value={v.big} onChange={(e) => setV({ ...v, big: e.target.value })} inputMode="decimal" style={{ ...inputStyle, width: "100%" }} />
        </label>
      </div>
      <div>
        <div style={{ fontSize: 13.5, marginBottom: 6 }}>Provisões: gastos do ano que não são todo mês</div>
        {prov.map((p) => (
          <div key={p.id} style={{ display: "flex", alignItems: "center", gap: 8, padding: "6px 0", borderBottom: `1px solid ${T.border}` }}>
            <span style={{ flex: 1, fontSize: 14 }}>{p.name}</span><span style={{ ...cxMono, fontSize: 13 }}>{brl(p.amount)}/ano</span>
            <button aria-label={`Remover ${p.name}`} onClick={() => setProv(prov.filter((x) => x.id !== p.id))} style={{ background: "none", border: "none", color: T.muted, fontSize: 18, minWidth: 40, minHeight: 40, cursor: "pointer" }}>×</button>
          </div>
        ))}
        <div style={{ display: "flex", gap: 6, marginTop: 6 }}>
          <input value={np.name} onChange={(e) => setNp({ ...np, name: e.target.value })} placeholder="IPVA da moto" aria-label="Nome da provisão" style={{ ...inputStyle }} />
          <input value={np.amount} onChange={(e) => setNp({ ...np, amount: e.target.value })} inputMode="decimal" placeholder="R$/ano" aria-label="Valor por ano" style={{ ...inputStyle, flex: "0 0 96px" }} />
          <button onClick={() => { const a = parseMoney(np.amount); if (np.name.trim() && a > 0) { setProv([...prov, { id: cxId(), name: np.name.trim(), amount: a }]); setNp({ name: "", amount: "" }); } }} style={{ ...cxGhost, flex: "0 0 52px", minHeight: 44 }} aria-label="Adicionar provisão">+</button>
        </div>
      </div>
      <button onClick={save} style={{ ...btnPrimary, minHeight: 48 }}>Salvar ajustes</button>
      {done && <div role="status" style={{ color: T.good, fontSize: 14 }}>{done}</div>}
    </div>
  );
}

/* custo de oportunidade dentro do lançamento */
function CxOppLine({ finance, today, value }) {
  const o = cxOpportunity(finance, today, value);
  if (!o || (o.hours == null && o.delay == null && o.after == null)) return null;
  const row = (t, val, col) => <div style={{ display: "flex", justifyContent: "space-between", gap: 8, fontSize: 14 }}><span>{t}</span><b style={{ ...cxMono, color: col || T.text }}>{val}</b></div>;
  return (
    <div data-pn="" style={{ marginTop: 10, padding: "9px 11px", border: `1px solid ${T.danger}66`, background: "rgba(255,83,104,.06)" }}>
      <div style={{ ...cxLbl, color: T.bad || T.danger, marginBottom: 4 }}>Custo de oportunidade</div>
      {o.hours != null && row("Em horas de trabalho", `${o.hours.toFixed(1).replace(".", ",")} h`)}
      {o.delay != null && row(`Atrasa ${o.delayWhat} em`, `${o.delay < 1 ? "menos de 1" : Math.round(o.delay)} ${Math.round(o.delay) === 1 || o.delay < 1 ? "dia" : "dias"}`, T.bad || T.danger)}
      {o.after != null && row("Livre hoje passa para", o.after[0] === o.after[1] ? `R$ ${o.after[1]}` : `R$ ${o.after[0]} – ${o.after[1]}`)}
      {o.big && <div style={{ fontSize: 12.5, color: T.gold, marginTop: 4 }}>Gasto grande: se não for necessidade, espere 24 h antes de comprar.</div>}
    </div>
  );
}

/* tela inteira do Caixa: comando em cima, arquivo fechado embaixo */
function CaixaCommand({ finance, fin, today }) {
  const m = caixaModel(finance, today);
  const [setup, setSetup] = useState(false);
  const setupRef = useRef(null);
  const openSetup = () => { setSetup(true); setTimeout(() => { try { setupRef.current && setupRef.current.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) { /* ok */ } }, 50); };
  return (
    <>
      <CaixaComando m={m} onSetup={openSetup} />
      <DueStrip finance={finance} today={today} fin={fin} />
      <CaixaInimigo m={m} finance={finance} today={today} fin={fin} />
      <CaixaCascata m={m} onSetup={openSetup} />
      <CaixaPrevisao m={m} finance={finance} today={today} />
      <CaixaDerrotados m={m} fin={fin} today={today} />
      <div ref={setupRef}>
        {setup ? (
          <Section title="Ajustes do comando" accent={T.accent}>
            <CaixaAjustes m={m} finance={finance} fin={fin} onClose={() => setSetup(false)} />
          </Section>
        ) : (
          <button onClick={openSetup} data-pn="" style={{ marginTop: 18, width: "100%", minHeight: 48, background: T.panel, border: `1px solid ${T.border}`, color: T.accentSoft, fontFamily: F.ui, fontSize: 13.5, fontWeight: 700, letterSpacing: "0.18em", textTransform: "uppercase", cursor: "pointer" }}>Ajustes do comando</button>
        )}
      </div>
    </>
  );
}
