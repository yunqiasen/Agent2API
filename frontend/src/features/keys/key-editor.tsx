import { useEffect, useId, useMemo, useState, type FormEvent } from "react";
import { Loader2, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { apiGet, apiPatch, apiPost, PLATFORMS, type ApiKey, type ModelDiscoverResponse } from "@/lib/api";

// 全池使用独立的 UI 值；Radix Select 不接受空字符串选项。
const ALL_CHANNELS = "all-channels";
type Scope = "all" | "selected";
type Choice = { id: string; name: string; enabled: boolean };

function modelChoices(catalog: ModelDiscoverResponse | null, platform: string): Choice[] {
  const choices = new Map<string, Choice>();
  for (const channel of catalog?.platforms ?? []) {
    if (platform && channel.platform !== platform) continue;
    const disabled = new Set(catalog?.disabled?.[channel.platform] ?? []);
    for (const model of channel.models) {
      const enabled = !disabled.has(model.id);
      const existing = choices.get(model.id);
      if (existing) existing.enabled ||= enabled;
      else choices.set(model.id, { id: model.id, name: model.name || model.id, enabled });
    }
  }
  return [...choices.values()].sort((a, b) => a.name.localeCompare(b.name) || a.id.localeCompare(b.id));
}

export function KeyEditor({ initial, onSaved }: { initial?: ApiKey; onSaved: () => void }) {
  const formId = useId();
  const [name, setName] = useState("");
  const [customKey, setCustomKey] = useState("");
  const [platform, setPlatform] = useState(initial?.platform ?? "");
  const [scope, setScope] = useState<Scope>(initial && initial.model_scope !== "all" ? "selected" : "all");
  const [selected, setSelected] = useState<string[]>(initial?.allowed_models ?? []);
  const [search, setSearch] = useState("");
  const [catalog, setCatalog] = useState<ModelDiscoverResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [revision, setRevision] = useState(0);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setLoadError("");
    apiGet<ModelDiscoverResponse>("/api/models/discover/progress")
      .then((data) => {
        if (!data?.ok || !Array.isArray(data.platforms) || data.platforms.some((p) => !Array.isArray(p.models))) {
          throw new Error("模型目录响应异常");
        }
        if (!cancelled) setCatalog(data);
      })
      .catch((error) => { if (!cancelled) setLoadError(error instanceof Error ? error.message : "加载模型目录失败"); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [platform, revision]);

  const choices = useMemo(() => modelChoices(catalog, platform), [catalog, platform]);
  const byId = useMemo(() => new Map(choices.map((choice) => [choice.id, choice])), [choices]);
  const current = Boolean(catalog) && !loading && !loadError;
  function invalidReason(id: string) {
    if (!current) return "";
    const choice = byId.get(id);
    return !choice ? "当前渠道未收录" : !choice.enabled ? "模型页已关闭" : "";
  }
  const invalid = selected.filter((id) => invalidReason(id));
  const query = search.trim().toLocaleLowerCase();
  const filtered = choices.filter((choice) => `${choice.name} ${choice.id}`.toLocaleLowerCase().includes(query));
  const blocked = saving || (scope === "selected" && (!current || selected.length === 0 || invalid.length > 0));

  function reload() { setLoading(true); setRevision((value) => value + 1); }
  function updateSelection(ids: string[]) { setSelected(ids); setSaveError(""); }
  function changeScope(value: Scope) {
    if (value !== scope) updateSelection([]);
    setScope(value);
    setSaveError("");
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (blocked) return;
    setSaving(true);
    setSaveError("");
    const permissions = { platform, model_scope: scope, allowed_models: scope === "selected" ? selected : [] };
    try {
      if (initial) {
        await apiPatch(`/api/keys/${initial.id}`, permissions);
        toast.success("密钥权限已保存，原 Key 保持不变");
      } else {
        await apiPost("/api/keys", { name: name.trim() || "未命名", api_key: customKey.trim(), ...permissions });
        toast.success("调用密钥已创建");
        setName(""); setCustomKey(""); updateSelection([]);
      }
      onSaved();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : "保存失败");
      // 保存失败保留选择，并回读最新全局开关；不访问上游模型。
      reload();
    } finally { setSaving(false); }
  }

  return (
    <form aria-label={initial ? "编辑调用密钥" : "新建调用密钥"} onSubmit={save} className="space-y-4">
      <div className={`grid gap-3 ${initial ? "" : "md:grid-cols-3"}`}>
        {!initial && <>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-name`}>名称</Label>
            <Input id={`${formId}-name`} placeholder="例如 我的客户端" value={name} disabled={saving} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label htmlFor={`${formId}-key`}>自定义 Key（留空自动生成）</Label>
            <Input id={`${formId}-key`} className="font-mono text-xs" placeholder="sk-xxxx" value={customKey} disabled={saving} onChange={(e) => setCustomKey(e.target.value)} />
          </div>
        </>}
        <div className="space-y-2">
          <Label htmlFor={`${formId}-platform`}>绑定渠道</Label>
          <Select value={platform || ALL_CHANNELS} disabled={saving} onValueChange={(value) => {
            const next = value === ALL_CHANNELS ? "" : value;
            if (next !== platform) { setLoading(true); setPlatform(next); setSaveError(""); }
          }}>
            <SelectTrigger id={`${formId}-platform`} className="w-full bg-background"><SelectValue /></SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL_CHANNELS}>全池</SelectItem>
              {PLATFORMS.map((p) => <SelectItem key={p.key} value={p.key}>{p.name}</SelectItem>)}
            </SelectContent>
          </Select>
        </div>
      </div>
      <fieldset disabled={saving} className="space-y-3">
        <legend className="mb-2 text-sm font-medium">模型范围</legend>
        <div className="flex flex-wrap gap-4 text-sm">
          <label className="flex cursor-pointer items-center gap-2"><input type="radio" name={`${formId}-scope`} value="all" checked={scope === "all"} onChange={() => changeScope("all")} className="accent-primary" />不限模型</label>
          <label className="flex cursor-pointer items-center gap-2"><input type="radio" name={`${formId}-scope`} value="selected" checked={scope === "selected"} onChange={() => changeScope("selected")} className="accent-primary" />指定模型</label>
        </div>
        {scope === "all" ? <p className="text-xs text-muted-foreground">使用所选渠道中已开启的模型；新模型也可用。</p> : <>
          <div className="flex items-center justify-between gap-3">
            <p className="text-xs text-muted-foreground">已选 {selected.length} 个 · 新模型需手动勾选</p>
            <Button type="button" variant="ghost" size="sm" onClick={reload} disabled={loading} className="gap-1">
              <RefreshCw className={`size-3.5 ${loading ? "animate-spin" : ""}`} />刷新可选模型
            </Button>
          </div>
          {selected.length > 0 && <div aria-label="已选模型" className="flex max-h-32 flex-wrap gap-2 overflow-y-auto">
            {selected.map((id) => {
              const reason = invalidReason(id);
              return <span key={id} className={`inline-flex items-center gap-1 rounded-md border px-2 py-1 text-xs ${reason ? "border-dashed bg-muted text-muted-foreground" : "bg-primary/5 text-foreground"}`}>
                <span className="break-all">{id}{reason && ` · 停用：${reason}`}</span>
                <button type="button" aria-label={`移除 ${id}`} onClick={() => updateSelection(selected.filter((mid) => mid !== id))} className="shrink-0 rounded p-0.5 hover:bg-secondary"><X className="size-3" /></button>
              </span>;
            })}
          </div>}
          <Input aria-label="搜索模型名称或 ID" placeholder="搜索模型名称或 ID" value={search} onChange={(e) => setSearch(e.target.value)} />
          {loading && <p role="status" className="text-xs text-muted-foreground">正在读取可选模型…</p>}
          {loadError && <p role="alert" className="text-xs text-rose-600">模型目录加载失败：{loadError}。保留当前选择，请刷新后保存。</p>}
          <div role="group" aria-label="可选模型" className="grid max-h-64 gap-1 overflow-y-auto rounded-lg border p-2 sm:grid-cols-2">
            {filtered.map((choice) => <label key={choice.id} className={`flex items-start gap-2 rounded-md p-2 ${choice.enabled && current ? "cursor-pointer hover:bg-muted" : "cursor-not-allowed opacity-45"}`}>
              <input type="checkbox" aria-label={`选择 ${choice.id}`} disabled={!choice.enabled || !current} checked={selected.includes(choice.id)} className="mt-0.5 accent-primary"
                onChange={(e) => updateSelection(e.target.checked ? [...selected, choice.id] : selected.filter((id) => id !== choice.id))} />
              <span className="min-w-0 text-xs"><span className="block font-medium">{choice.name}</span><span className="block break-all font-mono text-[10px] text-muted-foreground">{choice.id}</span>{!choice.enabled && <span className="text-[10px]">模型页已关闭</span>}</span>
            </label>)}
            {filtered.length === 0 && !loading && <p className="col-span-full p-2 text-xs text-muted-foreground">{search ? "没有匹配的模型" : "当前渠道暂无已收录模型"}</p>}
          </div>
          {selected.length === 0 && <p className="text-xs text-muted-foreground">至少选择 1 个已开启模型。</p>}
          {invalid.length > 0 && <p role="alert" className="text-xs text-amber-600">有 {invalid.length} 个选择已停用；请移除，或在模型页恢复开启后刷新。</p>}
        </>}
      </fieldset>
      {initial?.model_scope_error && <p role="alert" className="text-xs text-rose-600">原配置：{initial.model_scope_error}</p>}
      {saveError && <p role="alert" className="text-xs text-rose-600">{saveError}</p>}
      <div className="flex justify-end"><Button type="submit" disabled={blocked} className="min-w-24 gap-1.5">{saving && <Loader2 className="size-4 animate-spin" />}{initial ? "保存权限" : "创建"}</Button></div>
    </form>
  );
}
