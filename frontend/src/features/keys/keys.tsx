import { useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw, Trash2, Clock, Activity, ShieldCheck } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ApiKey, apiDelete, apiGet, apiPatch, apiPost, ago, platName, PLATFORMS } from "@/lib/api";
import { PageHeader, SectionCard, Chip, Copyable, type Tone } from "@/lib/design";

export function Keys() {
  const [list, setList] = useState<ApiKey[]>([]);
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [plat, setPlat] = useState("");

  function load() {
    apiGet<ApiKey[]>("/api/keys")
      .then(setList)
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
  }
  useEffect(load, []);

  async function add() {
    try {
      const d = await apiPost<{ api_key: string }>("/api/keys", {
        name: name || "未命名",
        api_key: key.trim(),
        platform: plat,
      });
      toast.success("已创建：" + d.api_key);
      setName("");
      setKey("");
      load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "创建失败");
    }
  }
  async function del(id: number) {
    if (!confirm("删除后使用该 Key 的客户端将立即失效，确认？")) return;
    await apiDelete(`/api/keys/${id}`);
    toast.success("已删除");
    load();
  }
  async function toggle(id: number, on: boolean) {
    await apiPatch(`/api/keys/${id}`, { enabled: on ? 1 : 0 });
    toast.success(on ? "已启用" : "已停用");
  }

  const tones: Tone[] = ["indigo", "violet", "emerald", "amber", "cyan", "sky", "fuchsia", "orange"];

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      <PageHeader
        icon={KeyRound}
        title="API Key"
        subtitle="创建调用密钥，绑定平台可限定调度范围"
        right={
          <Button variant="ghost" size="icon" onClick={load} aria-label="刷新">
            <RefreshCw className="size-4" />
          </Button>
        }
      />

      <SectionCard
        title="新建调用密钥"
        desc="绑定平台后，使用该 Key 的请求只会命中对应平台的账号；留空表示全池调度"
        icon={Plus}
        tone="indigo"
      >
        <div className="grid gap-3 px-5 py-4 md:grid-cols-4">
          <div className="space-y-2">
            <Label>名称</Label>
            <Input className="h-9 bg-background" placeholder="例如 我的客户端" value={name} onChange={(e) => setName(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>自定义 Key（留空自动生成）</Label>
            <Input className="h-9 bg-background font-mono text-xs" placeholder="sk-xxxx" value={key} onChange={(e) => setKey(e.target.value)} />
          </div>
          <div className="space-y-2">
            <Label>绑定平台（可选）</Label>
            <Select value={plat} onValueChange={setPlat}>
              <SelectTrigger className="h-9 bg-background">
                <SelectValue placeholder="全池" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="">全池</SelectItem>
                {PLATFORMS.map((p) => (
                  <SelectItem key={p.key} value={p.key}>
                    {p.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-end">
            <Button onClick={add} className="w-full gap-1.5">
              <Plus className="size-4" /> 创建
            </Button>
          </div>
        </div>
      </SectionCard>

      <SectionCard
        title="密钥列表"
        desc={`共 ${list.length} 个密钥`}
        icon={ShieldCheck}
        tone="violet"
        right={
          <Button variant="ghost" size="icon" onClick={load} aria-label="刷新">
            <RefreshCw className="size-4" />
          </Button>
        }
      >
        <div className="grid gap-3 p-5 sm:grid-cols-2 xl:grid-cols-3">
          {list.length === 0 && (
            <p className="col-span-full py-6 text-center text-xs text-muted-foreground">
              暂无密钥，使用上方表单创建
            </p>
          )}
          {list.map((k, i) => {
            const t = tones[i % tones.length];
            return (
              <div
                key={k.id}
                className="group flex flex-col rounded-xl border bg-card p-4 transition-all duration-300 hover:-translate-y-0.5 hover:shadow-sm animate-in fade-in slide-in-from-bottom-2"
                style={{ animationDelay: `${Math.min(i, 12) * 45}ms` }}
              >
                <div className="flex items-start justify-between gap-2">
                  <div className="flex min-w-0 items-center gap-2">
                    <span className="inline-flex size-8 shrink-0 items-center justify-center rounded-lg bg-secondary text-secondary-foreground">
                      <KeyRound className="size-4" />
                    </span>
                    <div className="min-w-0">
                      <div className="truncate text-sm font-medium">{k.name}</div>
                      <div className="font-mono text-[11px] text-muted-foreground">#{k.id}</div>
                    </div>
                  </div>
                  {k.platform ? (
                    <Chip tone={t}>{platName(k.platform)}</Chip>
                  ) : (
                    <Chip tone="slate">全池</Chip>
                  )}
                </div>

                <div className="mt-3 flex items-center gap-2 rounded-md border bg-muted/40 px-2.5 py-2">
                  <code className="min-w-0 flex-1 truncate font-mono text-[11px] text-foreground/80">{k.api_key}</code>
                  <Copyable text={k.api_key} className="shrink-0" />
                </div>

                <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
                  <span className="flex items-center gap-1">
                    <Activity className="size-3.5" /> {k.req_count || 0} 次调用
                  </span>
                  <span className="flex items-center gap-1">
                    <Clock className="size-3.5" /> {ago(k.last_used)}
                  </span>
                </div>

                <div className="mt-3 flex items-center justify-between border-t pt-3">
                  <div className="flex items-center gap-2">
                    <Switch checked={!!k.enabled} onCheckedChange={(v) => toggle(k.id, v)} />
                    <span className="text-[11px] text-muted-foreground">{k.enabled ? "已启用" : "已停用"}</span>
                  </div>
                  <Button variant="ghost" size="sm" className="gap-1 text-rose-600 dark:text-rose-400" onClick={() => del(k.id)}>
                    <Trash2 className="size-3.5" /> 删除
                  </Button>
                </div>
              </div>
            );
          })}
        </div>
      </SectionCard>
    </div>
  );
}
