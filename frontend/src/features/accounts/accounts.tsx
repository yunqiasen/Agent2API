import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Activity,
  CheckCircle2,
  FileUp,
  Gift,
  RefreshCw,
  Trash2,
  UserPlus,
  Users,
  KeyRound,
  Coins,
  Zap,
} from "lucide-react";
import { toast } from "sonner";
import { QRCodeSVG } from "qrcode.react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import {
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  ago,
  num,
  platName,
  PLATFORMS,
  type Account,
} from "@/lib/api";
import {
  Tile,
  Chip,
  PageHeader,
  Progress,
  type Tone,
} from "@/lib/design";

function QuotaCell({ a }: { a: Account }) {
  const t = a.quota_total || 0;
  const u = a.quota_used || 0;
  const r = a.quota_remain || 0;
  if (!t) return <span className="text-xs text-muted-foreground">额度尚未同步</span>;
  const pct = Math.max(0, Math.min(100, (r / t) * 100));
  const tone: Tone = pct < 10 ? "rose" : pct < 30 ? "amber" : "emerald";
  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between text-xs">
        <span className="text-muted-foreground">可用 / 总额</span>
        <span className="font-mono tabular-nums">
          {num(r)} / {num(t)} {a.quota_unit || ""}
        </span>
      </div>
      <Progress value={r} max={t} tone={tone} />
      <div className="flex items-center justify-between text-[11px] text-muted-foreground">
        <span>已消耗 {num(u)} · {a.quota_packages?.length || 0} 个上游套餐</span>
        <span>同步于 {ago(a.quota_updated_at)}</span>
      </div>
    </div>
  );
}

function StatusBadge({ a }: { a: Account }) {
  if (a.status === "active")
    return <Chip tone="emerald">已接入</Chip>;
  if (a.status === "banned")
    return <Chip tone="rose">封禁</Chip>;
  return <Chip tone="slate">{a.status}</Chip>;
}

function TokenBadge({ a }: { a: Account }) {
  const exp = a.expires_at_sec || 0;
  const now = Math.floor(Date.now() / 1000);
  if (!exp) return <Chip tone="slate">有效期未知</Chip>;
  if (exp <= now) return <Chip tone="rose">已过期</Chip>;
  if (exp - now < 900) return <Chip tone="amber">即将过期</Chip>;
  return <Chip tone="emerald">未过期</Chip>;
}

function CheckinBadge({ a }: { a: Account }) {
  if (!a.supports_checkin) return <Chip tone="slate">不适用</Chip>;
  if (a.checkin_today)
    return (
      <span className="inline-flex items-center gap-1">
        <Chip tone="emerald">已签到</Chip>
        {a.checkin_streak_known ? (
          <span className="text-[11px] text-muted-foreground">连续 {a.checkin_streak || 0} 天</span>
        ) : null}
      </span>
    );
  return (
    <span className="inline-flex items-center gap-1">
      <Chip tone="slate">未签到</Chip>
      {a.checkin_credit ? (
        <span className="text-[11px] text-amber-600 dark:text-amber-400">可领 {a.checkin_credit} 积分</span>
      ) : null}
    </span>
  );
}

type ProbeResult = { ok: boolean; ms?: number; error?: string; reply?: string; model?: string };

function ProbeCell({ a }: { a: Account }) {
  const [models, setModels] = useState<{ id: string; name: string }[]>([]);
  const [model, setModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [res, setRes] = useState<ProbeResult | null>(null);

  useEffect(() => {
    apiGet<{ ok: boolean; models: { id: string; name: string; kind?: string }[] }>(`/api/platforms/${a.platform}/models`)
      .then((d) => {
        // 测活走 chat 端点，只列对话模型
        const list = (d.models || []).filter((m) => m.id && (!m.kind || m.kind === "chat"));
        setModels(list);
        if (list.length && !model) setModel(list[0].id);
      })
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [a.platform]);

  async function probe() {
    if (!model) {
      toast.error("先选择模型");
      return;
    }
    setBusy(true);
    setRes(null);
    try {
      const d = await apiPost<ProbeResult>(`/api/accounts/${a.id}/probe`, { model });
      setRes(d);
      if (d.ok) toast.success(`测活通过 · ${d.ms}ms`);
      else toast.error("测活失败：" + (d.error || ""));
    } catch (e) {
      setRes({ ok: false, error: e instanceof Error ? e.message : "请求失败" });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-1.5">
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted-foreground">测活</span>
        <select
          value={model}
          onChange={(e) => setModel(e.target.value)}
          className="h-7 min-w-0 flex-1 rounded-md border border-input bg-background px-1.5 font-mono text-[11px]"
        >
          {models.length === 0 && <option value="">无已探测模型</option>}
          {models.map((m) => (
            <option key={m.id} value={m.id}>{m.id}</option>
          ))}
        </select>
        <Button variant="ghost" size="sm" className="h-7 gap-1 px-2" disabled={busy || !model} onClick={probe}>
          <Zap className="size-3.5" /> {busy ? "测中…" : "测活"}
        </Button>
        {res && (
          <span
            className={`inline-block size-2.5 shrink-0 rounded-full ${res.ok ? "bg-emerald-500" : "bg-rose-500"}`}
            title={res.ok ? `通过 · ${res.ms}ms` : res.error}
          />
        )}
      </div>
      {res && !res.ok && res.error && (
        <div className="rounded-md bg-rose-500/10 px-2 py-1 font-mono text-[11px] text-rose-600 dark:text-rose-400 break-all">
          {res.error}
        </div>
      )}
      {res && res.ok && (
        <div className="text-[11px] text-emerald-600 dark:text-emerald-400">
          通过 · {res.ms}ms{res.reply ? ` · ${res.reply.slice(0, 30)}` : ""}
        </div>
      )}
    </div>
  );
}

function AddAccountDialog({ open, onOpenChange, onAdded }: { open: boolean; onOpenChange: (v: boolean) => void; onAdded: () => void }) {
  const [platform, setPlatform] = useState(PLATFORMS[0].key);
  const [label, setLabel] = useState("");
  const [authUrl, setAuthUrl] = useState("");
  const [state, setState] = useState("");
  const [log, setLog] = useState("等待授权…");
  const [started, setStarted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [callbackUrl, setCallbackUrl] = useState("");
  const [callbackBusy, setCallbackBusy] = useState(false);
  const [traeMachineId, setTraeMachineId] = useState("");
  const [traeDeviceId, setTraeDeviceId] = useState("");
  const isTrae = platform === "trae";
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  function stop() {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }

  function reset() {
    stop();
    setAuthUrl("");
    setState("");
    setStarted(false);
    setLog("等待授权…");
    setBusy(false);
    setCallbackUrl("");
    setTraeMachineId("");
    setTraeDeviceId("");
  }

  async function start() {
    setBusy(true);
    setLog("等待授权…");
    try {
      const d = await apiPost<{ ok: boolean; state: string; auth_url: string; trae_machine_id?: string; trae_device_id?: string }>("/api/login/start", {
        platform,
        label: label || "后台添加",
      });
      setTraeMachineId(d.trae_machine_id || "");
      setTraeDeviceId(d.trae_device_id || "");
      setAuthUrl(d.auth_url);
      setState(d.state);
      setStarted(true);
      if (isTrae) {
        // Trae 没有 token 轮询端点：网页授权后复制回调 URL 到下方导入即可
        setLog("在浏览器完成授权后，页面会跳到 127.0.0.1（显示无法访问属正常），复制地址栏完整 URL 粘贴到下方导入。");
        return;
      }
      let n = 0;
      stop();
      timer.current = setInterval(async () => {
        n += 1;
        if (n > 150) {
          stop();
          setLog("超时，请重试");
          return;
        }
        try {
          const r = await apiPost<{ ok: boolean; account_id?: number; pending?: boolean; msg?: string; error?: string }>(
            "/api/login/poll",
            { state: d.state }
          );
          if (r.ok) {
            stop();
            toast.success("账号已加入号池");
            reset();
            onOpenChange(false);
            onAdded();
          } else {
            setLog(`等待授权… (${n}s) ${r.msg || r.error || ""}`);
          }
        } catch (e) {
          setLog("轮询出错：" + (e instanceof Error ? e.message : ""));
        }
      }, 2000);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "获取授权链接失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(v) => {
        if (!v) reset();
        onOpenChange(v);
      }}
    >
      <DialogContent>
        <DialogHeader>
          <DialogTitle>添加账号</DialogTitle>
          <DialogDescription>扫码或网页登录授权，账号将加入号池。</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>平台</Label>
            <Select value={platform} onValueChange={setPlatform}>
              <SelectTrigger className="h-9 bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PLATFORMS.map((p) => (
                  <SelectItem key={p.key} value={p.key}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>备注（可选）</Label>
            <Input className="h-9 bg-background" placeholder="例如 主号" value={label} onChange={(e) => setLabel(e.target.value)} />
          </div>
          {!started ? (
            <Button className="w-full" onClick={start} disabled={busy}>
              {busy ? "获取中…" : "获取授权链接"}
            </Button>
          ) : (
            isTrae ? (
              <div className="space-y-3 pt-1">
                <textarea readOnly rows={3} value={authUrl} className="w-full rounded-md border border-input bg-background px-2.5 py-2 font-mono text-xs" />
                <Button variant="secondary" className="w-full" onClick={() => window.open(authUrl, "_blank")}>
                  在浏览器打开
                </Button>
                <div className="space-y-2 border-t pt-3">
                  <p className="text-xs text-muted-foreground">
                    授权完成后浏览器会跳到 127.0.0.1（页面打不开属正常），复制地址栏完整回调 URL 粘贴到下面导入。
                  </p>
                  <textarea
                    rows={3}
                    value={callbackUrl}
                    onChange={(e) => setCallbackUrl(e.target.value)}
                    placeholder="http://127.0.0.1:18080/authorize?refreshToken=...&userJwt=..."
                    className="w-full rounded-md border border-input bg-background px-2.5 py-2 font-mono text-xs"
                  />
                  <Button
                    className="w-full"
                    disabled={callbackBusy || !callbackUrl.trim()}
                    onClick={async () => {
                      setCallbackBusy(true);
                      try {
                        await apiPost("/api/login/trae_callback", {
                          callback_url: callbackUrl.trim(),
                          machine_id: traeMachineId,
                          device_id: traeDeviceId,
                          label: label || "后台添加",
                        });
                        toast.success("Trae 账号已加入号池");
                        reset();
                        onOpenChange(false);
                        onAdded();
                      } catch (e) {
                        toast.error(e instanceof Error ? e.message : "Trae 回调导入失败");
                      } finally {
                        setCallbackBusy(false);
                      }
                    }}
                  >
                    {callbackBusy ? "导入中…" : "导入回调凭证"}
                  </Button>
                </div>
              </div>
            ) : (
            <Tabs defaultValue="scan">
              <TabsList className="grid w-full grid-cols-2">
                <TabsTrigger value="scan">扫码登录</TabsTrigger>
                <TabsTrigger value="web">网页登录</TabsTrigger>
              </TabsList>
              <TabsContent value="scan" className="flex flex-col items-center gap-3 pt-4">
                <div className="rounded-lg bg-white p-3">
                  {authUrl && <QRCodeSVG value={authUrl} size={176} level="M" />}
                </div>
                <p className="text-center text-xs text-muted-foreground">用手机扫码完成授权，本窗口会自动检测。</p>
              </TabsContent>
              <TabsContent value="web" className="space-y-3 pt-4">
                <textarea readOnly rows={3} value={authUrl} className="w-full rounded-md border border-input bg-background px-2.5 py-2 font-mono text-xs" />
                <Button variant="secondary" className="w-full" onClick={() => window.open(authUrl, "_blank")}>
                  在浏览器打开
                </Button>
              </TabsContent>
            </Tabs>
            )
          )}
          {started && (
            <pre className="whitespace-pre-wrap break-all rounded-md bg-muted/40 p-3 text-xs text-muted-foreground">
              {log}
            </pre>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

function ImportDialog({ open, onOpenChange, onAdded }: { open: boolean; onOpenChange: (v: boolean) => void; onAdded: () => void }) {
  const [platform, setPlatform] = useState(PLATFORMS[0].key);
  const [json, setJson] = useState("");
  const [at, setAt] = useState("");
  const [rt, setRt] = useState("");
  const [busy, setBusy] = useState(false);

  async function doImport() {
    setBusy(true);
    try {
      const body: any = { platform };
      if (json.trim()) body.json_text = json.trim();
      else {
        body.access_token = at.trim();
        body.refresh_token = rt.trim();
      }
      const d = await apiPost<{ added: number; updated: number }>("/api/accounts/import_token", body);
      toast.success(`导入完成：新增 ${d.added}，更新 ${d.updated}`);
      setJson("");
      setAt("");
      setRt("");
      onOpenChange(false);
      onAdded();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "导入失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>导入 Token / JSON</DialogTitle>
          <DialogDescription>粘贴 Cockpit Tools 导出的 JSON，或手填 access_token。</DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="space-y-2">
            <Label>平台</Label>
            <Select value={platform} onValueChange={setPlatform}>
              <SelectTrigger className="h-9 bg-background">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {PLATFORMS.map((p) => (
                  <SelectItem key={p.key} value={p.key}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label>JSON（支持数组，按 uid 去重）</Label>
            <textarea
              rows={5}
              value={json}
              onChange={(e) => setJson(e.target.value)}
              placeholder='[{"access_token":"...","refresh_token":"...","uid":"...","email":"..."}]'
              className="w-full rounded-md border border-input bg-background px-2.5 py-2 font-mono text-xs"
            />
          </div>
          <div className="grid grid-cols-2 gap-3">
            <div className="space-y-2">
              <Label>access_token</Label>
              <Input className="h-9 bg-background font-mono text-xs" value={at} onChange={(e) => setAt(e.target.value)} />
            </div>
            <div className="space-y-2">
              <Label>refresh_token</Label>
              <Input className="h-9 bg-background font-mono text-xs" value={rt} onChange={(e) => setRt(e.target.value)} />
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button onClick={doImport} disabled={busy}>
            {busy ? "导入中…" : "导入"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

export function Accounts() {
  const [list, setList] = useState<Account[]>([]);
  const [addOpen, setAddOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  // 渠道筛选：all = 全部渠道；其余只显示对应渠道号池
  const [platformFilter, setPlatformFilter] = useState("all");
  const filtered = platformFilter === "all"
    ? list
    : list.filter((a) => a.platform === platformFilter);

  function load() {
    apiGet<Account[]>("/api/accounts")
      .then(setList)
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
  }
  useEffect(load, []);

  const now = Math.floor(Date.now() / 1000);
  const total = filtered.length;
  const enabled = filtered.filter((a) => a.enabled).length;
  const claimable = filtered.reduce(
    (s, a) => s + (a.supports_checkin && !a.checkin_today ? a.checkin_credit || 0 : 0),
    0
  );
  const expiring = filtered.filter(
    (a) => a.expires_at_sec && a.expires_at_sec - now < 900 && a.expires_at_sec > now
  ).length;

  async function toggle(id: number, on: boolean) {
    await apiPatch(`/api/accounts/${id}`, { enabled: on ? 1 : 0 });
    toast.success(on ? "已启用" : "已停用");
    load();
  }
  async function del(id: number) {
    if (!confirm(`确认删除账号 #${id}？`)) return;
    await apiDelete(`/api/accounts/${id}`);
    toast.success("已删除");
    load();
  }
  async function refresh(id: number) {
    toast("刷新中…");
    try {
      const d = await apiPost<{ ok: boolean; error?: string }>(`/api/accounts/${id}/refresh`, {});
      toast.success(d.ok ? "已刷新令牌与额度" : "失败：" + (d.error || ""));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "失败");
    }
    load();
  }
  async function checkin(id: number) {
    toast("签到中…");
    try {
      const d = await apiPost<{
        ok: boolean; claimed_credit?: number; skipped?: boolean;
        reason?: string; error?: string;
      }>(`/api/accounts/${id}/checkin`, {});
      if (d.ok) {
        if (d.skipped) toast.success(d.reason || "上游确认今日已签到");
        else toast.success(d.claimed_credit ? `签到成功，获得 ${d.claimed_credit} 积分` : "签到成功");
      } else toast.error("签到失败：" + (d.error || ""));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "签到失败");
    }
    load();
  }
  async function quotaAll() {
    toast("正在刷新全部额度…");
    const d = await apiPost<{ results: { ok: boolean }[] }>("/api/accounts/quota_all", {});
    const ok = d.results.filter((x) => x.ok).length;
    toast.success(`完成：成功 ${ok} / ${d.results.length}`);
    load();
  }
  async function checkinAll() {
    const scope = platformFilter === "all" ? "全部渠道" : platName(platformFilter);
    toast(`正在签到${scope}账号…`);
    try {
      const d = await apiPost<{
        ok: boolean;
        results: { id: number; ok: boolean; skipped?: boolean; reason?: string; error?: string }[];
      }>("/api/accounts/checkin_all",
        platformFilter === "all" ? {} : { platform: platformFilter });
      const success = d.results.filter((x) => x.ok && !x.skipped).length;
      const skipped = d.results.filter((x) => x.skipped).length;
      const failed = d.results.filter((x) => !x.ok);
      if (failed.length > 0) {
        toast.error(
          `签到结束（${scope}）：成功 ${success}，已签 ${skipped}，失败 ${failed.length}。` +
          `首个错误：#${failed[0].id} ${failed[0].error || "未知错误"}`,
          { duration: 10000 },
        );
      } else {
        toast.success(`签到结束（${scope}）：成功 ${success}，已签跳过 ${skipped}`);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "批量签到失败");
    }
    load();
  }

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      <PageHeader
        icon={Users}
        title="号池"
        subtitle="管理接入的账号与额度 · 扫码添加或导入 Token"
        right={
          <>
            <Button onClick={() => setAddOpen(true)} className="gap-1.5">
              <UserPlus className="size-4" /> 添加账号
            </Button>
            <Button variant="secondary" onClick={() => setImportOpen(true)} className="gap-1.5">
              <FileUp className="size-4" /> 导入 Token
            </Button>
            <Button variant="secondary" onClick={quotaAll} className="gap-1.5">
              <Coins className="size-4" /> 刷新额度
            </Button>
            <Button variant="secondary" onClick={checkinAll} className="gap-1.5">
              <Gift className="size-4" /> {platformFilter === "all" ? "全部签到" : `签到${platName(platformFilter)}`}
            </Button>
            <Button variant="ghost" size="icon" onClick={load} aria-label="刷新">
              <RefreshCw className="size-4" />
            </Button>
          </>
        }
      />

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
        <Tile tone="indigo" icon={Users} label="账号总数" value={num(total)} hint="个" delay={0} />
        <Tile tone="emerald" icon={Activity} label="已启用" value={num(enabled)} hint="个" delay={80} />
        <Tile tone="amber" icon={Gift} label="今日可领积分" value={num(claimable)} hint="分" delay={160} />
        <Tile tone="rose" icon={AlertTriangle} label="即将过期" value={num(expiring)} hint="个" delay={240} />
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {[
          { key: "all", label: "全部渠道" },
          ...PLATFORMS.map((p) => ({ key: p.key, label: p.name })),
        ].map((opt) => {
          const active = platformFilter === opt.key;
          const count = opt.key === "all"
            ? list.length
            : list.filter((a) => a.platform === opt.key).length;
          return (
            <button
              key={opt.key}
              type="button"
              onClick={() => setPlatformFilter(opt.key)}
              className={
                "rounded-full border px-3 py-1.5 text-xs transition-colors " +
                (active
                  ? "border-primary bg-primary text-primary-foreground"
                  : "border-border bg-secondary text-secondary-foreground hover:bg-secondary/70")
              }
            >
              {opt.label}
              <span className={"ml-1.5 font-mono text-[10px] " + (active ? "opacity-80" : "opacity-60")}>
                {count}
              </span>
            </button>
          );
        })}
      </div>

      {filtered.length === 0 ? (
        <div className="rounded-xl border bg-card px-5 py-16 text-center text-sm text-muted-foreground animate-in fade-in">
          号池为空，点右上角「添加账号」开始接入
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {filtered.map((a, i) => {
            const pt = (["indigo", "violet", "emerald", "amber", "cyan", "sky", "fuchsia", "orange"] as Tone[])[
              i % 8
            ];
            return (
              <div
                key={a.id}
                className="group flex flex-col rounded-xl border bg-card p-4 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-sm animate-in fade-in slide-in-from-bottom-2"
                style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex items-center gap-2">
                    <span className="inline-flex size-8 items-center justify-center rounded-lg bg-secondary text-secondary-foreground">
                      <KeyRound className="size-4" />
                    </span>
                    <div className="flex flex-col">
                      <Chip>{platName(a.platform)}</Chip>
                      <span className="mt-1 font-mono text-[11px] text-muted-foreground">#{a.id}</span>
                    </div>
                  </div>
                  <div className="flex items-center gap-2">
                    <StatusBadge a={a} />
                    <Switch checked={!!a.enabled} onCheckedChange={(v) => toggle(a.id, v)} />
                  </div>
                </div>

                <div className="mt-3">
                  <div className="truncate text-sm font-medium" title="凭证导入时记录的账号名称">
                    {a.email || a.nickname || a.label || "-"}
                  </div>
                  <div className="font-mono text-[11px] text-muted-foreground" title="凭证中的账号 UID">
                    UID {(a.uid || "").slice(0, 22)}
                  </div>
                </div>

                <div className="my-3 h-px bg-border" />

                <div className="space-y-2.5">
                  <QuotaCell a={a} />
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-muted-foreground">令牌</span>
                    <span className="flex items-center gap-1.5">
                      <TokenBadge a={a} />
                      <span className="font-mono text-[11px] text-muted-foreground">{a.has_refresh ? "有刷新凭证" : "无刷新凭证"}</span>
                    </span>
                  </div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-muted-foreground">签到</span>
                    <CheckinBadge a={a} />
                  </div>
                  <div className="flex items-center justify-between text-xs">
                    <span className="text-muted-foreground">本站调用</span>
                    <span className="tabular-nums">
                      {a.req_count || 0} 次 · 失败 {a.fail_count || 0} · {ago(a.last_used)}
                    </span>
                  </div>
                  {a.last_error && (
                    <div className="truncate rounded-md bg-rose-500/10 px-2 py-1 text-[11px] text-rose-600 dark:text-rose-400" title={a.last_error}>
                      {a.last_error.slice(0, 40)}…
                    </div>
                  )}
                  <ProbeCell a={a} />
                </div>

                <div className="mt-3 flex items-center gap-1 border-t pt-3">
                  {a.supports_checkin && (
                    <Button variant="ghost" size="sm" className="gap-1 text-emerald-600 dark:text-emerald-400" onClick={() => checkin(a.id)}>
                      <CheckCircle2 className="size-3.5" /> 签到
                    </Button>
                  )}
                  <Button variant="ghost" size="sm" className="gap-1" onClick={() => refresh(a.id)}>
                    <RefreshCw className="size-3.5" /> 刷新
                  </Button>
                  <Button variant="ghost" size="sm" className="ml-auto gap-1 text-rose-600 dark:text-rose-400" onClick={() => del(a.id)}>
                    <Trash2 className="size-3.5" /> 删除
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      )}

      <AddAccountDialog open={addOpen} onOpenChange={setAddOpen} onAdded={load} />
      <ImportDialog open={importOpen} onOpenChange={setImportOpen} onAdded={load} />
    </div>
  );
}
