import { useCallback, useEffect, useMemo, useState, type ReactElement } from 'react';
import { api, fmt, productKey, saveProductKey, type ModelSettings, type OdrlPair, type RunSnapshot } from './api';

type Tab = 'convert' | 'policies' | 'runs';
const ATTACKER = 'attacker-client';
const EXAMPLE = 'Allow the research team to use dataset-7 for model training for 90 days for non-commercial research only.';

function highlightJson(value: unknown): string {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  return esc(JSON.stringify(value, null, 2)).replace(/(&quot;.*?&quot;)(\s*:)?/g, (_m, str: string, colon: string) => {
    if (colon) return `<span class="k">${str}</span>${colon}`;
    return `<span class="s">${str}</span>`;
  });
}

function markAttacker(text: string): ReactElement[] {
  return text.split('\n').map((line, i) => (
    <span key={i}>{line.includes(ATTACKER) ? <mark>{line}</mark> : line}{i < text.split('\n').length - 1 ? <br /> : null}</span>
  ));
}

function outcomeOf(snap: RunSnapshot): string {
  if (snap.status === 'running') return 'Converting…';
  if (snap.status === 'error') return `Could not finish: ${snap.error?.hint || 'unknown error'}`;
  if (!snap.verdict) return 'Finished without a verdict.';
  if (snap.verdict.verdict === 'PASS') return 'Valid ODRL, saved exactly once.';
  const failed = snap.verdict.results.filter((r) => !r.passed).map((r) => r.id);
  const parts: string[] = [];
  if (failed.includes('no_attacker_grant')) parts.push('an attacker grant was saved');
  if (failed.includes('odrl_valid')) parts.push('the saved document is not valid ODRL');
  if (failed.includes('exactly_once_save')) parts.push(`saved ${snap.saves.length} times instead of once`);
  if (!parts.length) parts.push('one or more checks failed');
  return `Not valid: ${parts.join('; ')}.`;
}

function PoliciesView({ onOpenRun }: { onOpenRun: (k: string) => void }) {
  const [pairs, setPairs] = useState<OdrlPair[]>([]);
  const [scenario, setScenario] = useState('');
  const [valid, setValid] = useState('');
  const [attacker, setAttacker] = useState('');
  const [scenarios, setScenarios] = useState<string[]>([]);
  const [note, setNote] = useState('Loading…');
  const load = useCallback(async () => {
    try {
      const q = `?scenario=${encodeURIComponent(scenario)}&valid=${valid}&attacker=${attacker}`;
      const body = await api.pairs(q);
      setPairs(body.pairs);
      setNote(`${body.pairs.length} ${body.pairs.length === 1 ? 'pair' : 'pairs'}`);
      setScenarios((prev) => {
        const seen = new Set(prev);
        body.pairs.forEach((p) => seen.add(p.scenario_id));
        if (scenario) seen.add(scenario);
        return [...seen];
      });
    } catch (e) { setNote(e instanceof Error ? e.message : 'load failed'); }
  }, [scenario, valid, attacker]);
  useEffect(() => { load(); }, [load]);
  return (
    <div>
      <div className="title-row"><h1>Every request with its ODRL</h1><span className="stat-chip" role="status">{note}</span></div>
      <p className="sub">Filter the policy store. Each card shows the request, its saved document, and validity.</p>
      <div className="filters" role="group" aria-label="Pair filters">
        <label className="field">Scenario<select value={scenario} onChange={(e) => setScenario(e.target.value)}>
          <option value="">All scenarios</option>{scenarios.map((s) => <option key={s} value={s}>{s}</option>)}
        </select></label>
        <label className="field">Validity<select value={valid} onChange={(e) => setValid(e.target.value)}>
          <option value="">Valid and invalid</option><option value="true">Valid only</option><option value="false">Invalid only</option>
        </select></label>
        <label className="field">Safety<select value={attacker} onChange={(e) => setAttacker(e.target.value)}>
          <option value="">All</option><option value="true">Attacker grant saved</option><option value="false">No attacker grant</option>
        </select></label>
      </div>
      {pairs.length === 0 ? <p className="status">Nothing converted yet — convert your first policy in Convert.</p> : (
        <div className="cards">{pairs.map((p) => (
          <article className="panel" key={p.run_key}>
            <div className="panel-h"><h2>{p.nl_label === 'synthetic_attack' ? 'Flagged request' : 'Request'}</h2><p>{p.scenario_id} · {p.policy_variant}</p></div>
            <div className="panel-b">
              <div className="nl">{markAttacker(p.nl_text)}</div>
              <p><span className={`pill ${p.valid ? 'pass' : 'fail'}`}>{p.valid ? 'Valid ODRL' : 'Invalid ODRL'}</span>{' '}
              <span className={`pill ${p.attacker ? 'warn' : 'muted'}`}>{p.attacker ? 'Attacker grant saved' : 'No attacker grant'}</span></p>
              <pre className="code" tabIndex={0} dangerouslySetInnerHTML={{ __html: highlightJson(p.doc) }} />
              <p><button className="btn" onClick={() => onOpenRun(p.run_key)}>Open run</button></p>
            </div>
          </article>
        ))}</div>
      )}
    </div>
  );
}

function ModelSetupDialog({ initial, onClose, onSaved }: { initial: ModelSettings | null; onClose: () => void; onSaved: (s: ModelSettings) => void }) {
  const [baseUrl, setBaseUrl] = useState(initial?.base_url || '');
  const [apiKey, setApiKey] = useState('');
  const [studioKey, setStudioKey] = useState(productKey());
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);
  const save = useCallback(async () => {
    setSaving(true); setError('');
    try {
      const s = await api.saveSettings({ base_url: baseUrl.trim(), api_key: apiKey.trim() });
      saveProductKey(studioKey.trim());
      onSaved(s);
      onClose();
    } catch (e) { setError(e instanceof Error ? e.message : 'save failed'); }
    finally { setSaving(false); }
  }, [baseUrl, apiKey, studioKey, onSaved, onClose]);
  return (
    <div className="overlay" onClick={onClose} role="presentation">
      <div className="dialog" role="dialog" aria-modal="true" aria-labelledby="setup-title" onClick={(e) => e.stopPropagation()}>
        <h2 id="setup-title">Model setup</h2>
        <p className="status">Live conversion calls your OpenAI-compatible chat endpoint. The key is kept in the server process only, never traced or returned.</p>
        <label className="field">Base URL<input type="text" value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder="https://…/v1" inputMode="url" /></label>
        <label className="field">API key<input type="password" value={apiKey} onChange={(e) => setApiKey(e.target.value)} placeholder={initial?.api_key_set ? 'Saved — enter a new key to replace it' : 'Paste the key'} autoComplete="off" /></label>
        <label className="field">Studio API key (only if this server enforces keys)<input type="password" value={studioKey} onChange={(e) => setStudioKey(e.target.value)} placeholder="Leave empty on a local server" autoComplete="off" /></label>
        {error ? <p className="status" role="alert">{error}</p> : null}
        <div className="row">
          <button className="btn primary" disabled={saving || !baseUrl.trim() || !apiKey.trim()} onClick={save}>{saving ? 'Saving…' : 'Save'}</button>
          <button className="btn" onClick={onClose}>Cancel</button>
        </div>
      </div>
    </div>
  );
}

function ConvertView({ onOpenRun, onNeedSetup }: { onOpenRun: (k: string) => void; onNeedSetup: () => void }) {
  const [text, setText] = useState('');
  const [status, setStatus] = useState('Describe the access rule in plain language, then convert.');
  const [snap, setSnap] = useState<RunSnapshot | null>(null);
  const [busy, setBusy] = useState(false);
  const [needsKey, setNeedsKey] = useState(false);

  const start = useCallback(async (nlText: string) => {
    setBusy(true); setSnap(null); setNeedsKey(false); setStatus('Converting…');
    try {
      const rec = await api.convert(nlText);
      setStatus(`Converting… ${rec.run_key}`);
      for (let i = 0; i < 120; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        const s2 = await api.run(rec.run_key);
        if (s2.status !== 'running') {
          setSnap(s2); setStatus(outcomeOf(s2));
          if (s2.status === 'error' && s2.error?.code === 'E_MODEL_API') setNeedsKey(true);
          break;
        }
        setStatus(`Converting… ${s2.events.length} events · ${s2.model_calls} model calls`);
      }
    } catch (e) { setStatus(e instanceof Error ? e.message : 'request failed'); }
    finally { setBusy(false); }
  }, []);

  const current = snap?.saves[snap.saves.length - 1];
  return (
    <div>
      <div className="title-row"><h1>Convert policy to ODRL</h1><span className="pill muted" title="Internal scenario id: odrl_convert">Data governance · odrl_convert</span><span className={`pill ${busy ? 'muted' : snap?.verdict?.verdict === 'PASS' ? 'pass' : snap && snap.status === 'done' ? 'fail' : 'muted'}`} role="status">{busy ? 'Converting…' : snap?.verdict?.verdict === 'PASS' ? 'Valid ODRL' : snap && snap.status === 'done' ? 'Needs review' : 'Ready'}</span></div>
      <p className="sub">Describe who may use what data, and for how long. You get back review-ready ODRL with validity checks.</p>
      <div className="grid-2">
        <section className="panel"><div className="panel-h"><h2>Request</h2><p>{text.length} / 4000 characters{needsKey ? ' · model not configured yet' : ''}</p></div>
          <div className="panel-b">
            <label className="field">Natural-language policy
              <textarea value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} placeholder={EXAMPLE} />
            </label>
            <div className="row" style={{ marginTop: 12 }}>
              <button className="btn primary" disabled={busy || !text.trim()} onClick={() => start(text.trim())}>{busy ? 'Converting…' : 'Convert to ODRL'}</button>
              <button className="btn" onClick={() => setText(EXAMPLE)}>Load an example</button>
            </div>
            <p className="status">Runs on the connected server. If conversion fails, ask IT to check the model key.</p>
            <p className="status" role="status">{status}</p>
          </div>
        </section>
        <section className="panel"><div className="panel-h"><h2>Converted ODRL</h2><p>{current ? `saved document · ${current.valid ? 'valid' : 'invalid'}` : 'no save yet — converting means producing a saved document'}</p></div>
          <div className="panel-b">
            {snap ? (<>
              <div className="nl">{markAttacker(snap.nl_text)}</div>
              <div style={{ height: 12 }} />
              <pre className="code" tabIndex={0} dangerouslySetInnerHTML={{ __html: current ? highlightJson(current.value) : '(no save committed)' }} />
              <p className="status">{outcomeOf(snap)}</p>
              <div className="row">
                <button className="btn" onClick={() => onOpenRun(snap.run_key)}>View this run</button>
                {needsKey ? <button className="btn primary" onClick={onNeedSetup}>Open model setup</button> : null}
              </div>
            </>) : <p className="status">Results appear here with the saved document and the plain-language outcome.</p>}
          </div>
        </section>
      </div>
    </div>
  );
}

function RunView({ focusKey, onConsumed }: { focusKey: string | null; onConsumed: () => void }) {
  const [runs, setRuns] = useState<{ run_key: string; scenario_id: string; policy_variant: string; verdict?: string; status: string }[]>([]);
  const [key, setKey] = useState<string | null>(focusKey);
  const [snap, setSnap] = useState<RunSnapshot | null>(null);
  const [idx, setIdx] = useState(0);
  const [note, setNote] = useState('Loading runs…');
  const loadRuns = useCallback(async () => {
    try { const b = await api.runs(); setRuns(b.runs); setNote(b.runs.length ? `${b.runs.length} runs` : 'no runs yet'); } catch (e) { setNote(e instanceof Error ? e.message : 'load failed'); }
  }, []);
  useEffect(() => { loadRuns(); }, [loadRuns]);
  useEffect(() => { if (focusKey) { setKey(focusKey); onConsumed(); } }, [focusKey, onConsumed]);
  useEffect(() => {
    if (!key && runs.length) setKey(runs[0].run_key);
    if (!key) return;
    api.run(key).then((s) => { setSnap(s); setIdx(Math.max(0, s.events.length - 1)); }).catch(() => setNote('run unreachable'));
  }, [key, runs]);
  const metrics = useMemo(() => {
    if (!snap) return [];
    const u = snap.usage_total;
    return [
      ['Model calls', String(snap.model_calls)],
      ['Tokens', u.total_tokens ? `${fmt(u.prompt_tokens)} in / ${fmt(u.completion_tokens)} out` : 'endpoint hid tokens'],
      ['Saves', String(snap.saves.length)],
      ['Chain', snap.chain_ok === true ? 'verified' : snap.chain_ok === false ? 'BROKEN' : 'pending'],
      ['Tenant', snap.tenant || 'local'],
      ['Wall', `${Number(snap.wall_seconds || 0).toFixed(1)}s`],
    ] as [string, string][];
  }, [snap]);
  const steps = useMemo(() => {
    if (!snap) return [];
    const v = snap.verdict?.verdict || snap.status;
    const variant = snap.spec.policy_variant;
    return [
      { n: 'Run', d: `${snap.spec.policy_variant} · ${String(snap.spec.run_id).slice(0, 8)}`, c: 'done' },
      { n: 'Trace', d: `${snap.events.length} events · chain ${snap.chain_ok ? 'verified' : 'open'}`, c: snap.chain_ok ? 'done' : 'active' },
      { n: 'Check', d: v, c: v === 'PASS' ? 'done' : 'active' },
      { n: 'Replay', d: variant === 'fixed' ? 'fixed-policy replay' : snap.patch ? 'patch proposed' : 'no patch', c: variant === 'fixed' || snap.patch ? 'done' : '' },
    ];
  }, [snap]);
  if (!runs.length && !snap) return (<div><div className="title-row"><h1>Runs</h1><span className="stat-chip">{note}</span></div><p className="sub">Convert a policy in Convert to record the first run.</p></div>);
  const ev = snap?.events[idx];
  return (
    <div>
      <div className="title-row"><h1>Runs</h1><span className={`pill ${snap?.verdict?.verdict === 'PASS' ? 'pass' : snap?.verdict ? 'fail' : 'muted'}`} role="status">{snap ? (snap.status === 'done' ? `${snap.verdict?.verdict} · ${snap.verdict?.results.length} invariants` : snap.status) : note}</span></div>
      <p className="sub">Every step of the run, with the evidence. Click a row to scrub.</p>
      <div className="row" style={{ marginBottom: 12 }}>
        <label className="field">Run<select value={key || ''} onChange={(e) => setKey(e.target.value)}>{runs.map((r) => <option key={r.run_key} value={r.run_key}>{`${r.run_key.slice(0, 8)} · ${r.scenario_id} · ${r.policy_variant} · ${r.verdict || r.status}`}</option>)}</select></label>
        <button className="btn" onClick={loadRuns}>Refresh</button>
      </div>
      {snap && (<>
        <ol className="stepper">{steps.map((s) => <li key={s.n} className={s.c}><b>{s.n}</b><span>{s.d}</span></li>)}</ol>
        <div className="kpis">{metrics.map(([l, v]) => <div className="kpi" key={l}><div className="l">{l}</div><div className="v" style={{ fontSize: 20 }}>{v}</div></div>)}</div>
        <div className="grid-2">
          <section className="panel"><div className="panel-h"><h2>Trace</h2><p>{ev ? `event ${idx} of ${snap.events.length - 1} · step ${ev.step}` : 'no events yet'}</p></div>
            <div className="panel-b">
              <div className="scrub"><span>Scrub</span><input type="range" min={0} max={Math.max(0, snap.events.length - 1)} value={idx} onChange={(e) => setIdx(Number(e.target.value))} aria-label="Trace scrubber" /></div>
              <table className="grid"><thead><tr><th>Step</th><th>Event</th><th>Detail</th></tr></thead><tbody>
                {snap.events.map((e, i) => (
                  <tr key={i} onClick={() => setIdx(i)} style={i === idx ? { background: 'var(--raised)' } : undefined}>
                    <td className="num mono">{e.step}</td>
                    <td>{e.tool ? `${e.tool} · ${String((e.detail as { phase?: string })?.phase || e.outcome || '')}` : String((e.detail as { phase?: string })?.phase || e.outcome || '')}</td>
                    <td className="mono" style={{ fontSize: 12 }}>{JSON.stringify(e.args || e.policy_decision || (e.detail as { data?: unknown })?.data || '').slice(0, 90)}</td>
                  </tr>
                ))}
              </tbody></table>
            </div>
          </section>
          <section className="panel"><div className="panel-h"><h2>Verdicts and replay</h2><p>{outcomeOf(snap)}</p></div>
            <div className="panel-b">
              <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
                {(snap.verdict?.results || []).map((r) => <li key={r.id}><span className={`pill ${r.passed ? 'pass' : 'fail'}`}>{r.passed ? 'PASS' : 'FAIL'} {r.id}</span><p className="status">{r.evidence}</p></li>)}
              </ul>
              {snap.patch && snap.spec.policy_variant !== 'fixed' && <p className="status">Patch proposed: {((snap.patch.patch as unknown as { rules?: unknown[] })?.rules || []).length} rules. Re-run from Convert to verify the fix.</p>}
            </div>
          </section>
        </div>
      </>)}
    </div>
  );
}

export default function App() {
  const [tab, setTab] = useState<Tab>('convert');
  const [focusKey, setFocusKey] = useState<string | null>(null);
  const [settings, setSettings] = useState<ModelSettings | null>(null);
  const [setupOpen, setSetupOpen] = useState(false);
  const openRun = useCallback((k: string) => { setFocusKey(k); setTab('runs'); }, []);
  const openSetup = useCallback(() => setSetupOpen(true), []);
  useEffect(() => {
    api.settings().then(setSettings).catch(() => setSettings(null));
  }, []);
  useEffect(() => {
    if (settings && !settings.configured) setSetupOpen(true);
  }, [settings]);
  const titles: Record<Tab, string> = { convert: 'Convert', policies: 'Policies', runs: 'Runs' };
  return (
    <div className="frame">
      <aside className="sidebar" aria-label="Primary">
        <div className="brand"><div className="mark">N</div><div><b>NL2ODRL</b><small>Responsible AI console</small></div></div>
        <div className="nav-label">Studio</div>
        {(['convert', 'policies', 'runs'] as Tab[]).map((t) => (
          <button key={t} className="nav-item" aria-current={tab === t ? 'page' : undefined} onClick={() => setTab(t)}><span className="dot" />{titles[t]}</button>
        ))}
        <div className="side-foot">Runs stay in the local database. Traces are hash-chained JSONL.</div>
      </aside>
      <div className="topbar"><span className="crumb">Data governance · {titles[tab]}</span><span className="spacer" />
        <button className="btn" style={{ minHeight: 28, fontSize: 12 }} onClick={openSetup} title={settings?.base_url ? `Model endpoint: ${settings.base_url}` : 'Configure the live model'}>
          <span className={`pill ${settings?.configured ? 'pass' : 'warn'}`}>{settings ? (settings.configured ? 'Model ready' : 'Model setup needed') : 'Model status…'}</span>
        </button>
      </div>
      <div className="sheet-wrap"><main className="sheet">
        <div className="tabs" role="tablist" aria-label="Views">
          {(['convert', 'policies', 'runs'] as Tab[]).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} onClick={() => setTab(t)}>{titles[t]}</button>
          ))}
        </div>
        {tab === 'convert' && <ConvertView onOpenRun={openRun} onNeedSetup={openSetup} />}
        {tab === 'policies' && <PoliciesView onOpenRun={openRun} />}
        {tab === 'runs' && <RunView focusKey={focusKey} onConsumed={() => setFocusKey(null)} />}
      </main></div>
      {setupOpen ? <ModelSetupDialog initial={settings} onClose={() => setSetupOpen(false)} onSaved={setSettings} /> : null}
    </div>
  );
}
