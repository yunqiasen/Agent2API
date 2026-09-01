// 与 Agent2API 后端的通信层（基于 Flask 会话 cookie，same-origin）

export const PLATFORMS = [
  { key: "workbuddy", name: "WorkBuddy" },
  { key: "trae", name: "TraeWork" },
  { key: "codebuddy", name: "CodeBuddy 国际" },
] as const;

export type PlatformKey = (typeof PLATFORMS)[number]["key"];

export function platName(key: string): string {
  return PLATFORMS.find((p) => p.key === key)?.name ?? key;
}

export class ApiError extends Error {
  status: number;
  constructor(message: string, status = 0) {
    super(message);
    this.status = status;
  }
}

async function request<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const res = await fetch(path, {
    ...opts,
    credentials: "include",
    headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
  });
  let data: any = null;
  try {
    data = await res.json();
  } catch {
    /* ignore */
  }
  // 会话失效：除登录/公共接入外，统一抛未登录
  if (
    res.status === 401 &&
    !path.startsWith("/api/login") &&
    !path.startsWith("/api/join")
  ) {
    throw new ApiError("未登录", 401);
  }
  if (!res.ok) {
    throw new ApiError(data?.error || data?.msg || `HTTP ${res.status}`, res.status);
  }
  return data as T;
}

export function apiGet<T>(path: string): Promise<T> {
  return request<T>(path);
}
export function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
}
export function apiPatch<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, { method: "PATCH", body: body ? JSON.stringify(body) : undefined });
}
export function apiDelete<T>(path: string): Promise<T> {
  return request<T>(path, { method: "DELETE" });
}

// ---------- 格式化工具 ----------
export function fmtTs(t?: number): string {
  if (!t) return "-";
  return new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
}
export function ago(t?: number): string {
  if (!t) return "从未";
  const d = Math.floor(Date.now() / 1000) - t;
  if (d < 60) return d + "秒前";
  if (d < 3600) return Math.floor(d / 60) + "分钟前";
  if (d < 86400) return Math.floor(d / 3600) + "小时前";
  return Math.floor(d / 86400) + "天前";
}
export function num(v?: number): string {
  return (Math.round((v || 0) * 100) / 100).toLocaleString("zh-CN");
}

// ---------- 类型 ----------
export interface ModelEntry {
  id: string; // 实际请求时发往上游网关的模型 id
  name: string; // 面板展示名
  kind?: "chat" | "image" | "audio"; // chat=对话, image=图像生成, audio=语音/合成
}

export interface PlatformInfo {
  key: string;
  name: string;
  api_base: string;
  model_base: string;
  note?: string;
  models?: ModelEntry[];
}

export interface ModelDiscoverResponse {
  ok: boolean;
  platforms: {
    platform: string;
    label: string;
    models: ModelEntry[]; // 该平台可用的真实模型（探测确认）
    note?: string;
  }[];
  exposed: ModelEntry[]; // 对外 /v1/models 暴露的清单
  total?: number;
  discovered_at?: number;
  disabled?: Record<string, string[]>; // 渠道×模型开关：被关闭的模型 id 列表
  image_discovering?: boolean; // 后台图像探测是否仍在进行
}
export interface OverviewStats {
  accounts: number;
  accounts_active: number;
  quota_remain: number;
  req_24h: number;
  ok_24h: number;
  keys: number;
  platforms: PlatformInfo[];
  settings: Record<string, any>;
}
export interface Account {
  id: number;
  platform: string;
  email?: string;
  nickname?: string;
  label?: string;
  uid?: string;
  quota_total?: number;
  quota_used?: number;
  quota_remain?: number;
  quota_unit?: string;
  quota_packages?: any[];
  quota_updated_at?: number;
  expires_at_sec?: number;
  has_refresh?: boolean;
  supports_checkin?: boolean;
  checkin_today?: number;
  checkin_streak?: number;
  checkin_streak_known?: boolean;
  checkin_last_date?: string;
  checkin_credit?: number;
  checkin_updated_at?: number;
  status?: string;
  status_reason?: string;
  last_error?: string;
  req_count?: number;
  fail_count?: number;
  last_used?: number;
  source?: string;
  enabled?: boolean;
}
export interface ApiKey {
  id: number;
  name: string;
  api_key: string;
  platform: string;
  req_count?: number;
  last_used?: number;
  enabled?: boolean;
}
export interface CallLog {
  ts?: number;
  platform?: string;
  account_id?: number;
  model?: string;
  stream?: boolean;
  status?: number;
  duration_ms?: number;
  prompt_chars?: number;
  error?: string;
}
export interface Settings {
  default_model?: string;
  strategy?: string;
  auto_refresh?: string;
  refresh_ahead?: string;
  model_base_override?: string;
  allow_model_passthrough?: string;
  log_retention?: string;
  system_prompt?: string;
  auto_checkin?: string;
  checkin_time?: string;
  credit_threshold?: string;
  free_models?: string;
  public_join_enabled?: string;
  model_route_priority?: string;
  model_platform_disabled?: string;
}

/* ---------- 公共号池接入（用户主动提交账号） ---------- */
export interface JoinQuota {
  total?: number;
  remain?: number;
  unit?: string;
  packages?: { name?: string; remain?: number; total?: number; unit?: string }[];
}
export interface JoinStartResp {
  ok: boolean;
  state: string;
  auth_url: string;
  login_id?: string;
  error?: string;
}
export interface JoinPollResp {
  ok: boolean;
  account_id?: number;
  email?: string;
  nickname?: string;
  quota?: JoinQuota | null;
  pending?: boolean;
  done?: boolean;
  msg?: string;
  error?: string;
  quota_error?: string;
}

export interface OverviewUsage {
  now: number;
  pool: { credits: number; accounts: number; enabled: number };
  today: {
    calls: number;
    tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
    cost: number;
  };
  series: { bucket: string; calls: number; tokens: number; cost: number }[];
  by_model: { model: string; calls: number; tokens: number; cost: number }[];
  by_platform: { platform: string; calls: number; tokens: number; cost: number }[];
  deductions: {
    id: number;
    ts: number;
    platform: string;
    model: string;
    account_id: number;
    status: number;
    tokens: number;
    cost: number;
  }[];
  credit_source: "upstream_usage";
}
