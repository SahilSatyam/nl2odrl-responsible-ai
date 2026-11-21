export interface RunSpec { scenario_id: string; policy_variant: string; seed: number; run_id: string }
export interface SpanRef { quote: string; start?: number | null; end?: number | null }
export interface RuleIR {
  modality: 'permission' | 'prohibition' | 'obligation'; action: string;
  target?: string | null; assignee?: string | null; assigner?: string | null;
  constraints?: unknown[]; duties?: unknown[]; source_spans?: SpanRef[];
}
export interface PolicyIR { policy_type?: string; conflict_strategy?: string | null; assigner?: string | null; assignee?: string | null; rules: RuleIR[]; ambiguities?: { kind: string; description: string }[]; assumptions?: { field: string; assumed_value: string; reason: string }[] }
export interface Draft {
  status: 'ready' | 'needs_review' | 'no_rules' | 'failed';
  odrl: unknown; ir: PolicyIR | null; valid: boolean; errors: string[];
  ambiguities: { kind: string; description: string }[];
  assumptions: { field: string; assumed_value: string; reason: string }[];
  dropped_rules: RuleIR[]; alerts: string[]; auto_publish: boolean;
  reasoning?: { ok: boolean; conflicts: { code: string; detail: string }[]; unsatisfiable: { code: string; detail: string }[] };
  grounding?: { grounded: boolean; issues: { code: string; detail: string; rule_index?: number | null }[] };
}
export interface Decision { decision: 'approve' | 'edit' | 'reject'; reason: string; reviewer_fp: string; edited: boolean; widening: string[]; alerts: string[]; created_at: string }
export interface RunSnapshot {
  run_key: string; status: 'queued' | 'running' | 'done' | 'error';
  tenant?: string; request_id?: string | null;
  spec: RunSpec; nl_text: string; nl_label?: string;
  events: TraceEvent[]; usage_total: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  model_calls: number; verdict: { verdict: string; results: VerdictRow[] } | null;
  patch: { patch?: { rules?: { rule: string; tool: string; action: string }[] } } | null;
  saves: SaveRow[]; chain_ok: boolean | null; wall_seconds: number; error: { code: string; hint: string } | null;
  draft?: Draft | null; decision?: Decision | null;
}
export interface TraceEvent { step: number; tool?: string; outcome?: string; args?: Record<string, unknown>; detail?: Record<string, unknown>; policy_decision?: { rule?: string; action?: string }; ts_virtual?: number }
export interface VerdictRow { id: string; passed: boolean; evidence: string }
export interface SaveRow { step: number; value: unknown; valid: boolean; errors: string[]; attacker: boolean }
export interface OdrlPair { run_key: string; nl_text: string; nl_label: string; scenario_id: string; policy_variant: string; doc: unknown; valid: boolean; attacker: boolean; outcome?: string }
export interface ModelSettings { model: string; base_url: string; api_key_set: boolean; configured: boolean; timeout_s: string; max_tokens: string }
export interface DraftRow { run_key: string; created_at: string; nl_text: string; decision: string | null; draft: Draft }
export interface Session { key_fp: string; via: 'local' | 'cookie' | 'bearer'; expires_in?: number | null }

/** Error carrying the server's code and optional structured detail. */
export class ApiError extends Error {
  status: number;
  code: string;
  detail?: unknown;
  constructor(message: string, status: number, code: string, detail?: unknown) {
    super(message);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

// The studio key is exchanged once for an HttpOnly session cookie; it is never
// written to localStorage. Every request sends the CSRF header the server
// requires on cookie-authenticated writes.
async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json', 'X-Requested-With': 'nl2odrl' };
  const r = await fetch(url, {
    ...init,
    credentials: 'same-origin',
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
  });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const b = body as { hint?: string; error?: string; detail?: unknown };
    throw new ApiError(b.hint || `request failed (${r.status})`, r.status, b.error || 'E_HTTP', b.detail);
  }
  return body as T;
}
const post = (body: unknown): RequestInit => ({ method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

export const api = {
  login(api_key: string) { return json<Session>('/api/session', post({ api_key })); },
  logout() { return json<{ ok: boolean }>('/api/session', { method: 'DELETE' }); },
  whoami() { return json<Session>('/api/session'); },
  convert(nl_text: string) {
    return json<{ run_key: string; status: string }>('/api/convert', post({ nl_text, policy_variant: 'fixed', seed: 42, max_steps: 12 }));
  },
  run(runKey: string) { return json<RunSnapshot>(`/api/runs/${encodeURIComponent(runKey)}`); },
  runs(limit = 100) { return json<{ runs: { run_key: string; scenario_id: string; policy_variant: string; verdict?: string; status: string }[] }>(`/api/runs?limit=${limit}`); },
  pairs(q: string) { return json<{ pairs: OdrlPair[] }>(`/api/odrl${q}`); },
  drafts(includeDecided = false) { return json<{ drafts: DraftRow[] }>(`/api/drafts${includeDecided ? '?include_decided=1' : ''}`); },
  publish(runKey: string, body: { decision: 'approve' | 'edit' | 'reject'; reason: string; ir?: unknown }) {
    return json<Decision & { published: boolean; odrl?: unknown }>(`/api/runs/${encodeURIComponent(runKey)}/publish`, post(body));
  },
  settings() { return json<ModelSettings>('/api/settings'); },
  saveSettings(payload: { base_url: string; api_key: string }) { return json<ModelSettings>('/api/settings', post(payload)); },
};

/**
 * Follow a run over Server-Sent Events until it ends. Falls back to polling
 * when EventSource is unavailable or the stream errors before the end event.
 */
export function followRun(runKey: string, onProgress: (events: number, status: string) => void): Promise<RunSnapshot> {
  const poll = async (): Promise<RunSnapshot> => {
    for (let i = 0; i < 600; i++) {
      const s = await api.run(runKey);
      if (s.status !== 'running' && s.status !== 'queued') return s;
      onProgress(s.events.length, s.status);
      await new Promise((r) => setTimeout(r, 1000));
    }
    return api.run(runKey);
  };
  if (typeof EventSource === 'undefined') return poll();
  return new Promise((resolve) => {
    let count = 0;
    let status = 'queued';
    let settled = false;
    const es = new EventSource(`/api/runs/${encodeURIComponent(runKey)}/events`, { withCredentials: true });
    const finish = (p: Promise<RunSnapshot>) => { if (!settled) { settled = true; es.close(); p.then(resolve); } };
    es.addEventListener('status', (e) => { status = JSON.parse((e as MessageEvent).data).status; onProgress(count, status); });
    es.addEventListener('trace', () => { count += 1; onProgress(count, status); });
    es.addEventListener('end', () => finish(api.run(runKey)));
    es.addEventListener('timeout', () => finish(poll()));
    es.onerror = () => finish(poll());
  });
}

export function fmt(n: number | undefined): string {
  return typeof n === 'number' ? n.toLocaleString('en-US') : '-';
}
