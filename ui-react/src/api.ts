export interface RunSpec { scenario_id: string; policy_variant: string; seed: number; run_id: string }
export interface RunSnapshot {
  run_key: string; status: 'running' | 'done' | 'error';
  tenant?: string;
  spec: RunSpec; nl_text: string; nl_label?: string;
  events: TraceEvent[]; usage_total: { prompt_tokens: number; completion_tokens: number; total_tokens: number };
  model_calls: number; verdict: { verdict: string; results: VerdictRow[] } | null;
  patch: { patch?: { rules?: { rule: string; tool: string; action: string }[] } } | null;
  saves: SaveRow[]; chain_ok: boolean | null; wall_seconds: number; error: { code: string; hint: string } | null;
}
export interface TraceEvent { step: number; tool?: string; outcome?: string; args?: Record<string, unknown>; detail?: Record<string, unknown>; policy_decision?: { rule?: string; action?: string }; ts_virtual?: number }
export interface VerdictRow { id: string; passed: boolean; evidence: string }
export interface SaveRow { step: number; value: unknown; valid: boolean; errors: string[]; attacker: boolean }
export interface OdrlPair { run_key: string; nl_text: string; nl_label: string; scenario_id: string; policy_variant: string; doc: unknown; valid: boolean; attacker: boolean }
export interface ModelSettings { model: string; base_url: string; api_key_set: boolean; configured: boolean; timeout_s: string; max_tokens: string }

const PRODUCT_KEY_STORAGE = 'nl2odrl_api_key';

export function productKey(): string {
  try {
    return localStorage.getItem(PRODUCT_KEY_STORAGE) || '';
  } catch {
    return '';
  }
}

export function saveProductKey(key: string): void {
  try {
    if (key) localStorage.setItem(PRODUCT_KEY_STORAGE, key);
    else localStorage.removeItem(PRODUCT_KEY_STORAGE);
  } catch {
    /* private mode: key simply does not persist */
  }
}

async function json<T>(url: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const key = productKey();
  if (key) headers.Authorization = `Bearer ${key}`;
  const r = await fetch(url, { ...init, headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) } });
  const body = await r.json();
  if (!r.ok) throw new Error((body as { hint?: string }).hint || 'request failed');
  return body as T;
}

export const api = {
  convert(nl_text: string) {
    return json<{ run_key: string }>('/api/convert', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ nl_text, policy_variant: 'fixed', seed: 42, max_steps: 12 }),
    });
  },
  run(runKey: string) { return json<RunSnapshot>(`/api/runs/${encodeURIComponent(runKey)}`); },
  runs(limit = 100) { return json<{ runs: { run_key: string; scenario_id: string; policy_variant: string; verdict?: string; status: string }[] }>(`/api/runs?limit=${limit}`); },
  pairs(q: string) { return json<{ pairs: OdrlPair[] }>(`/api/odrl${q}`); },
  settings() { return json<ModelSettings>('/api/settings'); },
  saveSettings(payload: { base_url: string; api_key: string }) {
    return json<ModelSettings>('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
  },
};

export function fmt(n: number | undefined): string {
  return typeof n === 'number' ? n.toLocaleString('en-US') : '-';
}
