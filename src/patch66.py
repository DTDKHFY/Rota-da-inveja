#!/usr/bin/env python3
"""v66: árvore viva — vento nos galhos, folhas caindo, vaga-lumes, névoa, runas e chuva por cima da pintura do estágio."""
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()

def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:100], s.count(a))
    s = s.replace(a, b)

LIVE = r'''/* ---------- árvore viva (v66) ----------
   Onde fica cada coisa em cada pintura (0–1 da largura e da altura): base do tronco, topo da copa,
   largura que balança, runas do chão ou do tronco, copa (de onde caem as folhas) e lua.
   Pintura nova = uma linha nova aqui e a letra dela em TREE_FX_PLAN. */
const TREE_FX = {
  A: { moon: [0.24, 0.13], rune: [0.42, 0.665, 0.25, 0.05] },
  B: { moon: [0.24, 0.15], rune: [0.51, 0.75, 0.23, 0.05], tree: { base: [0.51, 0.725], top: 0.645, cx: 0.51, hw: 0.08, amp: 0.014 } },
  D: { moon: [0.24, 0.15], rune: [0.51, 0.755, 0.23, 0.05], tree: { base: [0.51, 0.74], top: 0.565, cx: 0.52, hw: 0.14, amp: 0.0154 } },
  E: { moon: [0.22, 0.11], trunk: [0.525, 0.56, 0.64], tree: { base: [0.52, 0.70], top: 0.19, cx: 0.50, hw: 0.30, amp: 0.014, canopy: [0.50, 0.31, 0.26, 0.11] } },
  F: { moon: [0.22, 0.10], trunk: [0.52, 0.52, 0.64], tree: { base: [0.50, 0.66], top: 0.20, cx: 0.42, hw: 0.40, amp: 0.0126, canopy: [0.42, 0.32, 0.34, 0.12] } },
  G: { moon: [0.21, 0.09], trunk: [0.57, 0.48, 0.64], tree: { base: [0.56, 0.66], top: 0.03, cx: 0.55, hw: 0.55, amp: 0.0112, canopy: [0.55, 0.22, 0.46, 0.16] } },
  H: { moon: [0.18, 0.08], trunk: [0.62, 0.45, 0.70], gold: true, tree: { base: [0.60, 0.72], top: 0.0, cx: 0.62, hw: 0.55, amp: 0.0098, canopy: [0.62, 0.22, 0.44, 0.17] } },
};
const TREE_FX_PLAN = "A B D D E E E E F F F F G G G G H H H H".split(" ");
/* clima do app → clima da pintura */
const TREE_WX = {
  calmo: { wind: 0.75, gust: 0.15, fog: 0.10, rain: 0, flies: 1 },
  vento: { wind: 1.35, gust: 0.55, fog: 0.06, rain: 0, flies: 1.4 },
  tempestade: { wind: 2.0, gust: 0.9, fog: 0.08, rain: 1, flies: 0.4 },
  nevoa: { wind: 0.4, gust: 0.1, fog: 0.32, rain: 0, flies: 0.8 },
};
function treeMood(state) {
  const wx = treeWeather(state);
  const w = wx.weather === "storm" ? "tempestade" : wx.weather === "fog" ? "nevoa" : wx.weather === "sun" ? "vento" : "calmo";
  const p = treeParamsOf(state);
  const today = dayKey(new Date());
  const dry = p.route ? (state.finals || []).slice(0, 5).some((f) => routeBranch(state, f, today).dry) : (p.dry || []).some(Boolean);
  return { w, dry };
}
const TREE_VS = "attribute vec2 p;varying vec2 v;void main(){v=vec2((p.x+1.)*.5,1.-(p.y+1.)*.5);gl_Position=vec4(p,0.,1.);}";
/* a pintura entorta mais no alto da árvore e nada na base do tronco; as folhas tremulam por cima */
const TREE_FS = `precision mediump float;varying vec2 v;uniform sampler2D tex;uniform vec4 crop;uniform vec2 base;uniform float t,amp,flut,top,cx,hw,gust,on;
void main(){vec2 q=crop.xy+v*crop.zw;float h=clamp((base.y-q.y)/max(.001,base.y-top),0.,1.);
float w=pow(h,1.5)*exp(-pow((q.x-cx)/hw,2.)*1.6)*on;float s=sin(t*.9)*.55+sin(t*1.9+1.3)*.25+sin(t*.37+2.)*.2+gust;
float dx=w*amp*s+w*flut*sin(t*5.3+q.y*70.+q.x*35.);float dy=w*flut*.6*cos(t*4.1+q.x*60.+q.y*20.)-abs(w*amp*s)*.12;
gl_FragColor=texture2D(tex,q-vec2(dx,dy));}`;
function LiveArt({ i, mode, posY, mood }) {
  const wrap = useRef(null), glRef = useRef(null), fxRef = useRef(null);
  const moodRef = useRef(mood);
  moodRef.current = mood;
  const [ok, setOk] = useState(false);
  const [reduce] = useState(() => { try { return window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { return false; } });
  useEffect(() => {
    if (reduce) return undefined;
    const box = wrap.current, glc = glRef.current, fxc = fxRef.current;
    if (!box || !glc || !fxc) return undefined;
    const fx = TREE_FX[TREE_FX_PLAN[i]] || TREE_FX.A;
    const lite = mode === "lite";
    const stage = i;
    let dead = false, raf = 0, visible = true, last = 0, ready = false, shown = false;
    const t0 = performance.now();
    let W = 0, H = 0, crop = [0, 0, 1, 1];
    /* vento: WebGL; sem ele a pintura fica parada e só os efeitos se mexem */
    let gl = null, U = {};
    try {
      gl = glc.getContext("webgl", { premultipliedAlpha: false, antialias: false, alpha: false });
      if (gl) {
        const sh = (type, src) => { const o = gl.createShader(type); gl.shaderSource(o, src); gl.compileShader(o); return o; };
        const prog = gl.createProgram();
        gl.attachShader(prog, sh(gl.VERTEX_SHADER, TREE_VS)); gl.attachShader(prog, sh(gl.FRAGMENT_SHADER, TREE_FS)); gl.linkProgram(prog);
        if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) gl = null;
        else {
          gl.useProgram(prog);
          gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
          gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
          const loc = gl.getAttribLocation(prog, "p"); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
          ["crop", "base", "t", "amp", "flut", "top", "cx", "hw", "gust", "on"].forEach((k) => { U[k] = gl.getUniformLocation(prog, k); });
          gl.bindTexture(gl.TEXTURE_2D, gl.createTexture());
          [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]].forEach(([k, v]) => gl.texParameteri(gl.TEXTURE_2D, k, v));
        }
      }
    } catch (e) { gl = null; }
    const onLost = (e) => { e.preventDefault(); gl = null; setOk(false); };
    glc.addEventListener("webglcontextlost", onLost);
    const img = new Image();
    img.decoding = "async";
    img.onload = () => {
      if (dead) return;
      if (gl) { try { gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, img); } catch (e) { gl = null; } }
      ready = true; kick();
    };
    img.src = treeArt(i);
    const layout = () => {
      /* tamanho de layout (clientWidth), não o da animação de entrada da aba */
      const rw = box.clientWidth, rh = box.clientHeight;
      if (!rw || !rh) return;
      const dpr = Math.min(lite ? 1.5 : 2, window.devicePixelRatio || 1);
      W = Math.round(rw * dpr); H = Math.round(rh * dpr);
      glc.width = W; glc.height = H; fxc.width = W; fxc.height = H;
      const ai = 720 / 1280, ac = rw / rh;
      crop = ac > ai ? [0, (1 - ai / ac) * posY, 1, ai / ac] : [(1 - ac / ai) * 0.5, 0, ac / ai, 1];
      if (gl) gl.viewport(0, 0, W, H);
    };
    layout();
    const X = (u) => ((u - crop[0]) / crop[2]) * W;
    const Y = (q) => ((q - crop[1]) / crop[3]) * H;
    const rnd = (a, b) => a + Math.random() * (b - a);
    const cp = fx.tree && fx.tree.canopy;
    let flies = Array.from({ length: Math.round((lite ? 4 : 6) + stage * (lite ? 0.6 : 1.3)) }, (_, k) => {
      const hi = !lite && cp && k % 4 === 0;
      return { u: hi ? cp[0] + rnd(-cp[2], cp[2]) : rnd(0.04, 0.96), v: hi ? cp[1] + rnd(-cp[3], cp[3] * 1.4) : rnd(0.5, 0.95), a: rnd(0, 6.28), sp: rnd(0.0004, 0.0011), ph: rnd(0, 6.28), hi };
    });
    let leaves = [], drops = [], flash = 0;
    const c = fxc.getContext("2d");
    function draw(now) {
      const t = (now - t0) / 1000;
      const dt = Math.min(0.1, (now - (last || now)) / 1000);
      last = now;
      const md = moodRef.current || { w: "calmo", dry: false };
      const wx = TREE_WX[md.w] || TREE_WX.calmo;
      const gust = wx.gust * Math.max(0, Math.sin(t * 0.23) * Math.sin(t * 0.71 + 1));
      if (gl && ready) {
        const tr = fx.tree;
        gl.uniform4f(U.crop, crop[0], crop[1], crop[2], crop[3]);
        gl.uniform1f(U.t, t); gl.uniform1f(U.on, tr ? 1 : 0);
        if (tr) {
          gl.uniform2f(U.base, tr.base[0], tr.base[1]); gl.uniform1f(U.top, tr.top); gl.uniform1f(U.cx, tr.cx); gl.uniform1f(U.hw, tr.hw);
          gl.uniform1f(U.amp, tr.amp * wx.wind); gl.uniform1f(U.flut, 0.0032 * wx.wind); gl.uniform1f(U.gust, gust * 1.6);
        }
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
        if (!shown) { shown = true; setOk(true); }
      }
      c.clearRect(0, 0, W, H);
      const sc = W / 360;
      let g;
      if (!lite) {
        /* luar e névoa que atravessa o vale */
        const m = fx.moon, mp = 0.55 + 0.15 * Math.sin(t * 0.6);
        g = c.createRadialGradient(X(m[0]), Y(m[1]), 0, X(m[0]), Y(m[1]), 70 * sc);
        g.addColorStop(0, `rgba(255,245,210,${0.22 * mp})`); g.addColorStop(1, "rgba(255,245,210,0)");
        c.fillStyle = g; c.fillRect(0, 0, W, H);
        for (let k = 0; k < 3; k++) {
          const fu = ((t * (0.012 + k * 0.006) + k * 0.37) % 1.6) - 0.3, fv = 0.5 + k * 0.06;
          g = c.createRadialGradient(X(fu), Y(fv), 0, X(fu), Y(fv), 220 * sc);
          g.addColorStop(0, `rgba(205,215,230,${wx.fog})`); g.addColorStop(1, "rgba(205,215,230,0)");
          c.save(); c.translate(X(fu), Y(fv)); c.scale(1.9, 0.45); c.translate(-X(fu), -Y(fv)); c.fillStyle = g; c.fillRect(0, 0, W, H); c.restore();
        }
      }
      c.globalCompositeOperation = "lighter";
      /* runas pulsando: no chão nos primeiros estágios, no tronco depois */
      const pulse = 0.5 + 0.5 * Math.sin(t * 1.4);
      if (fx.rune) {
        const r = fx.rune, rx = (r[2] / crop[2]) * W * 1.15, ry = (r[3] / crop[3]) * H * 1.6;
        c.save(); c.translate(X(r[0]), Y(r[1])); c.scale(1, ry / rx);
        g = c.createRadialGradient(0, 0, 0, 0, 0, rx);
        g.addColorStop(0, `rgba(60,255,210,${0.04 + 0.08 * pulse})`); g.addColorStop(0.75, `rgba(60,255,210,${0.07 + 0.16 * pulse})`); g.addColorStop(1, "rgba(60,255,210,0)");
        c.fillStyle = g; c.beginPath(); c.arc(0, 0, rx, 0, 6.29); c.fill(); c.restore();
      }
      if (fx.trunk) {
        const [tu, v0, v1] = fx.trunk;
        g = c.createLinearGradient(0, Y(v0), 0, Y(v1));
        const a = 0.08 + 0.18 * pulse;
        g.addColorStop(0, "rgba(60,255,210,0)"); g.addColorStop(0.5, `rgba(60,255,210,${a})`); g.addColorStop(1, "rgba(60,255,210,0)");
        c.fillStyle = g; c.beginPath(); c.ellipse(X(tu), (Y(v0) + Y(v1)) / 2, 26 * sc, Math.max(1, (Y(v1) - Y(v0)) / 2), 0, 0, 6.29); c.fill();
      }
      /* vaga-lumes: mais a cada estágio */
      const nf = Math.max(2, Math.round(flies.length * Math.min(1, wx.flies)));
      flies.forEach((f, k) => {
        f.a += Math.sin(t * 0.7 + f.ph) * 0.8 * dt * 2;
        f.u += Math.cos(f.a) * f.sp * (1 + wx.wind * 0.4); f.v += Math.sin(f.a) * f.sp * 0.6;
        if (f.u < 0.02 || f.u > 0.98) f.a = Math.PI - f.a;
        if (f.v < (f.hi ? 0.08 : 0.45) || f.v > 0.97) f.a = -f.a;
        if (k >= nf) return;
        const b = Math.max(0, Math.sin(t * 2.1 + f.ph * 3)) ** 2 * (wx.flies > 1 ? 1 : 0.85);
        const x = X(f.u), y = Y(f.v), rr = (3 + 5 * b) * sc;
        g = c.createRadialGradient(x, y, 0, x, y, rr * 3);
        g.addColorStop(0, `rgba(240,255,150,${0.25 + 0.7 * b})`); g.addColorStop(0.25, `rgba(200,255,90,${0.18 * b + 0.05})`); g.addColorStop(1, "rgba(200,255,90,0)");
        c.fillStyle = g; c.fillRect(x - rr * 3, y - rr * 3, rr * 6, rr * 6);
      });
      if (!lite && fx.gold && cp) {
        for (let k = 0; k < 14; k++) {
          const u = cp[0] + Math.sin(k * 12.9898) * cp[2] * 0.9, q = cp[1] + Math.cos(k * 78.233) * cp[3];
          const b = Math.max(0, Math.sin(t * 1.7 + k * 1.3)) ** 3;
          g = c.createRadialGradient(X(u), Y(q), 0, X(u), Y(q), 9 * sc);
          g.addColorStop(0, `rgba(255,210,110,${0.6 * b})`); g.addColorStop(1, "rgba(255,210,110,0)");
          c.fillStyle = g; c.fillRect(X(u) - 9 * sc, Y(q) - 9 * sc, 18 * sc, 18 * sc);
        }
      }
      c.globalCompositeOperation = "source-over";
      if (lite) return;
      /* folhas: mais com vento; secas quando um objetivo final está parado */
      if (cp) {
        const rate = (0.25 + wx.wind * 0.5 + (md.dry ? 0.9 : 0)) * (0.5 + stage / 20);
        if (Math.random() < rate * dt * 2 && leaves.length < 40) {
          const isDry = md.dry && Math.random() < 0.7;
          leaves.push({ u: cp[0] + rnd(-cp[2], cp[2]) * 0.85, v: cp[1] + rnd(-cp[3], cp[3]) * 0.6, vu: rnd(-0.01, 0.01), vv: rnd(0.03, 0.06), r: rnd(0, 6.28), vr: rnd(-3, 3), ph: rnd(0, 6.28), life: 1,
            col: isDry ? `hsl(${rnd(28, 42)},60%,${rnd(38, 50)}%)` : fx.gold && Math.random() < 0.5 ? `hsl(${rnd(40, 50)},80%,58%)` : `hsl(${rnd(95, 125)},40%,${rnd(30, 42)}%)` });
        }
      }
      leaves = leaves.filter((l) => l.v < 0.92 && l.life > 0);
      leaves.forEach((l) => {
        const push = wx.wind * (0.04 + gust * 0.08);
        l.u += (l.vu + push * 0.5 + Math.sin(t * 2 + l.ph) * 0.02) * dt; l.v += l.vv * dt; l.r += l.vr * dt;
        if (l.v > 0.8) l.life -= dt * 0.8;
        c.save(); c.globalAlpha = Math.min(1, l.life * 1.2) * 0.95; c.translate(X(l.u), Y(l.v)); c.rotate(l.r); c.scale(1, 0.55 + 0.45 * Math.sin(t * 4 + l.ph));
        c.fillStyle = l.col; c.beginPath(); c.ellipse(0, 0, 4.2 * sc, 2.2 * sc, 0, 0, 6.29); c.fill(); c.restore();
      });
      /* chuva e relâmpago */
      if (wx.rain) {
        if (drops.length < 140) for (let k = 0; k < 8; k++) drops.push({ x: rnd(-0.1, 1.1), y: rnd(-0.2, 0), sp: rnd(1.1, 1.6) });
        c.strokeStyle = "rgba(180,200,230,.35)"; c.lineWidth = sc; c.beginPath();
        drops.forEach((d) => { d.y += d.sp * dt; d.x += 0.25 * dt; c.moveTo(d.x * W, d.y * H); c.lineTo((d.x - 0.012) * W, (d.y - 0.045) * H); });
        c.stroke(); drops = drops.filter((d) => d.y < 1.05);
        c.fillStyle = "rgba(10,15,30,.28)"; c.fillRect(0, 0, W, H);
        if (flash <= 0 && Math.random() < dt * 0.12) flash = 0.35;
        if (flash > 0) { c.fillStyle = `rgba(220,230,255,${flash * 0.6})`; c.fillRect(0, 0, W, H); flash -= dt * 1.6; }
      } else drops = [];
    }
    /* 30 quadros por segundo; para fora da tela e com o app em segundo plano */
    function frame(now) {
      raf = 0;
      if (dead || !visible || document.hidden) return;
      raf = requestAnimationFrame(frame);
      if (now - last < 33 || !W) return;
      draw(now);
    }
    function kick() { if (!raf && !dead && visible && !document.hidden) raf = requestAnimationFrame(frame); }
    const io = typeof IntersectionObserver !== "undefined" ? new IntersectionObserver((es) => { visible = es[es.length - 1].isIntersecting; if (visible) { last = 0; kick(); } }, { threshold: 0.02 }) : null;
    if (io) io.observe(box);
    const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(layout) : null;
    if (ro) ro.observe(box); else window.addEventListener("resize", layout);
    const onVis = () => { if (!document.hidden) { last = 0; kick(); } };
    document.addEventListener("visibilitychange", onVis);
    kick();
    return () => {
      dead = true;
      if (raf) cancelAnimationFrame(raf);
      if (io) io.disconnect();
      if (ro) ro.disconnect(); else window.removeEventListener("resize", layout);
      document.removeEventListener("visibilitychange", onVis);
      glc.removeEventListener("webglcontextlost", onLost);
      try { const ext = gl && gl.getExtension("WEBGL_lose_context"); if (ext) ext.loseContext(); } catch (e) { /* nada */ }
      setOk(false);
    };
  }, [i, mode, posY, reduce]);
  if (reduce) return null;
  const cv = { position: "absolute", inset: 0, width: "100%", height: "100%", pointerEvents: "none" };
  return (
    <div ref={wrap} aria-hidden="true" style={{ position: "absolute", inset: 0, pointerEvents: "none" }}>
      <canvas key={`g${i}-${mode}-${posY}`} ref={glRef} style={{ ...cv, opacity: ok ? 1 : 0, transition: "opacity .6s ease-out" }} />
      <canvas key={`f${i}-${mode}-${posY}`} ref={fxRef} style={cv} />
    </div>
  );
}
/* a pintura do estágio; quando o estágio muda, a nova entra por cima da antiga */
function StageArt({ i, ratio, posY = 0.62, alt, live, mood, children }) {'''
rep('''/* a pintura do estágio; quando o estágio muda, a nova entra por cima da antiga */
function StageArt({ i, ratio, pos, alt, children }) {''', LIVE)
rep('''  const im = { position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", objectPosition: pos || "50% 62%" };''',
    '''  const im = { position: "absolute", inset: 0, width: "100%", height: "100%", objectFit: "cover", objectPosition: `50% ${posY * 100}%` };''')
rep('''      <img key={`c${cur}`} src={treeArt(cur)} alt={alt || ""} decoding="async" className={prev != null ? "ts-in" : undefined} style={im} />
      {children}''', '''      <img key={`c${cur}`} src={treeArt(cur)} alt={alt || ""} decoding="async" className={prev != null ? "ts-in" : undefined} style={im} />
      {live && <LiveArt i={cur} mode={live} posY={posY} mood={mood} />}
      {children}''')
# Status: versão leve (vento e vaga-lumes)
rep('''            <StageArt i={st.i} ratio="3 / 4" pos="50% 62%">''', '''            <StageArt i={st.i} ratio="3 / 4" posY={0.62} live="lite" mood={mood}>''')
rep('''  const wx = treeWeather(state);
  const p = treeParamsOf(state);
  const rb = p.route ?''', '''  const wx = treeWeather(state);
  const mood = treeMood(state);
  const p = treeParamsOf(state);
  const rb = p.route ?''')
# Perfil: versão completa
rep('''      <StageArt i={i} ratio="3 / 4" pos="50% 48%" alt=''', '''      <StageArt i={i} ratio="3 / 4" posY={0.48} live="full" mood={mood} alt=''')
rep('''function TreeStages({ state }) {
  const st = treeStageInfo(state);''', '''function TreeStages({ state }) {
  const st = treeStageInfo(state);
  const mood = treeMood(state);''')

open(p, 'w', encoding='utf-8').write(s)
print('ok', len(s))
