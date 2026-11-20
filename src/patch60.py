p = 'work60s.html'
s = open(p, encoding='utf-8').read()
def rep(a, b, n=1):
    global s
    assert s.count(a) == n, (a[:80], s.count(a)); s = s.replace(a, b)
mod = open('sync60.jsx', encoding='utf-8').read()
# módulo antes do App
rep('function FinanceTab({ finance, fin }) {', mod + '\nfunction FinanceTab({ finance, fin }) {')
# hook logo depois do salvamento ao sair
rep('''      window.removeEventListener("pagehide", saveNow);
      window.removeEventListener("beforeunload", saveNow);
    };
  }, []);''', '''      window.removeEventListener("pagehide", saveNow);
      window.removeEventListener("beforeunload", saveNow);
    };
  }, []);
  const applyRemoteRef = useRef(null);
  useGithubSync(stateRef, loaded, state, applyRemoteRef);''')
# como aplicar o estado que veio do repositório
rep('''  function importBackup(p) {''', '''  applyRemoteRef.current = (p, by, device) => {
    const ns = normalizeState(p);
    applyTheme(ns.themeId);
    applyFont(ns.fontId);
    setState(processRollover(ns));
    if (by && by !== device) setFlash({ kind: "up", text: by === "claude" ? "ATUALIZADO PELO CLAUDE" : "DADOS SINCRONIZADOS" });
  };
  function importBackup(p) {''')
# Ajustes: troca o Google pela sincronização com GitHub
rep('''      {row("google", "Sincronizar com Google", "Em breve · falta o ID do cliente do Google")}''', '''      {row("google", "Sincronizar com GitHub", syncOn() ? `Ligado · ${syncMeta().repo}` : "Desligado", null, syncOn() ? T.good : undefined)}''')
a = s.index('      {open === "google" && panel(<div style={{ fontSize: 13, color: T.muted, lineHeight: 1.5, padding: "4px 2px" }}>Assim que o ID')
b = s.index('\n', a)
s = s[:a] + '      {open === "google" && panel(<SyncPanel />)}' + s[b:]
open(p, 'w', encoding='utf-8').write(s)
print('ok')
