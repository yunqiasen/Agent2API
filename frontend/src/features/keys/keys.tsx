import { useEffect, useState } from "react";
import { KeyRound, Plus, RefreshCw, Trash2, Clock, Activity, ShieldCheck, Pencil } from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { apiDelete, apiGet, apiPatch, ago, platName, type ApiKey } from "@/lib/api";
import { PageHeader, SectionCard, Chip, Copyable, type Tone } from "@/lib/design";
import { KeyEditor } from "./key-editor";

export function Keys() {
  const [list, setList] = useState<ApiKey[]>([]);
  const [editing, setEditing] = useState<ApiKey | null>(null);

  function load() {
    apiGet<ApiKey[]>("/api/keys")
      .then(setList)
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
  }
  useEffect(load, []);

  async function del(id: number) {
    if (!confirm("删除后使用该 Key 的客户端将立即失效，确认？")) return;
    try {
      await apiDelete(`/api/keys/${id}`);
      toast.success("已删除");
      load();
    } catch (error) { toast.error(error instanceof Error ? error.message : "删除失败"); }
  }
  async function toggle(id: number, on: boolean) {
    try {
      await apiPatch(`/api/keys/${id}`, { enabled: on ? 1 : 0 });
      toast.success(on ? "已启用" : "已停用");
      load();
    } catch (error) { toast.error(error instanceof Error ? error.message : "保存失败"); }
  }

  async function edit(id: number) {
    try {
      const latest = await apiGet<ApiKey[]>("/api/keys");
      setList(latest);
      const key = latest.find((entry) => entry.id === id);
      if (!key) { toast.error("该 Key 已被删除"); return; }
      setEditing(key);
    } catch (error) { toast.error(error instanceof Error ? error.message : "读取密钥失败"); }
  }

  const tones: Tone[] = ["indigo", "violet", "emerald", "amber", "cyan", "sky", "fuchsia", "orange"];

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      <PageHeader
        icon={KeyRound}
        title="API Key"
        subtitle="按渠道和模型分配调用权限"
        right={
          <Button variant="ghost" size="icon" onClick={load} aria-label="刷新">
            <RefreshCw className="size-4" />
          </Button>
        }
      />

      <SectionCard
        title="新建调用密钥"
        desc="渠道绑定与模型范围同时生效，关闭的模型在选择区灰显"
        icon={Plus}
        tone="indigo"
      >
        <div className="px-5 py-4"><KeyEditor onSaved={load} /></div>
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
                role="group"
                aria-label={`密钥 ${k.name}`}
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

                <div className="mt-3 space-y-1.5 text-xs">
                  <span className="font-medium">{k.model_scope_error ? "模型权限配置异常" : k.model_scope === "all" ? "不限模型" : `指定 ${k.allowed_models.length} 个模型`}</span>
                  {k.model_scope_error ? <p role="alert" className="text-rose-600">{k.model_scope_error}</p> : k.model_scope === "selected" && (
                    <div className="flex max-h-24 flex-wrap gap-1 overflow-y-auto">{k.allowed_models.map((id) => <span key={id} className="break-all rounded bg-secondary px-1.5 py-0.5 font-mono text-[10px]">{id}</span>)}</div>
                  )}
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
                    <Switch aria-label={`启停密钥 ${k.name}`} checked={!!k.enabled} onCheckedChange={(v) => toggle(k.id, v)} />
                    <span className="text-[11px] text-muted-foreground">{k.enabled ? "已启用" : "已停用"}</span>
                  </div>
                  <div className="flex items-center gap-1">
                    <Button variant="ghost" size="sm" className="gap-1" onClick={() => edit(k.id)}><Pencil className="size-3.5" /> 编辑</Button>
                    <Button variant="ghost" size="sm" className="gap-1 text-rose-600 dark:text-rose-400" onClick={() => del(k.id)}><Trash2 className="size-3.5" /> 删除</Button>
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      </SectionCard>
      <Dialog open={Boolean(editing)} onOpenChange={(open) => { if (!open) setEditing(null); }}>
        <DialogContent className="max-h-[90vh] max-w-[680px] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>编辑密钥 · {editing?.name}</DialogTitle>
            <DialogDescription>保存渠道和模型范围，原 Key 与调用统计保持不变。</DialogDescription>
          </DialogHeader>
          {editing && <KeyEditor key={editing.id} initial={editing} onSaved={() => { setEditing(null); load(); }} />}
        </DialogContent>
      </Dialog>
    </div>
  );
}
