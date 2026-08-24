import { useEffect, useMemo, useState } from "react";
import {
  RefreshCw,
  Trash2,
  ScrollText,
  Search,
  CheckCircle2,
  XCircle,
  Filter,
  Clock,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { CallLog, apiDelete, apiGet, fmtTs, platName, PLATFORMS } from "@/lib/api";
import { PageHeader, SectionCard, Chip, toneFor } from "@/lib/design";

type StatusFilter = "all" | "success" | "fail";

export function Logs() {
  const [list, setList] = useState<CallLog[]>([]);
  const [q, setQ] = useState("");
  const [plat, setPlat] = useState("all");
  const [status, setStatus] = useState<StatusFilter>("all");

  function load() {
    apiGet<CallLog[]>("/api/logs?limit=200")
      .then(setList)
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
  }
  useEffect(load, []);

  async function clear() {
    if (!confirm("清空全部日志？")) return;
    await apiDelete("/api/logs");
    toast.success("已清空");
    load();
  }

  const filtered = useMemo(() => {
    const kw = q.trim().toLowerCase();
    return list.filter((l) => {
      if (plat !== "all" && l.platform !== plat) return false;
      if (status === "success" && !(l.status && l.status < 400)) return false;
      if (status === "fail" && !(l.status && l.status >= 400)) return false;
      if (kw) {
        const hay = `${l.model || ""} ${l.account_id ?? ""} ${l.error || ""} ${l.platform || ""}`.toLowerCase();
        if (!hay.includes(kw)) return false;
      }
      return true;
    });
  }, [list, q, plat, status]);

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      <PageHeader
        icon={ScrollText}
        title="调用日志"
        subtitle={`最近 200 条请求记录 · 命中 ${filtered.length} 条`}
        right={
          <>
            <Button variant="ghost" size="icon" onClick={load} aria-label="刷新">
              <RefreshCw className="size-4" />
            </Button>
            <Button variant="ghost" size="icon" className="text-rose-600 dark:text-rose-400" onClick={clear} aria-label="清空">
              <Trash2 className="size-4" />
            </Button>
          </>
        }
      />

      <div className="flex flex-wrap items-center gap-3 rounded-xl border bg-card p-3 animate-in fade-in slide-in-from-bottom-2">
        <div className="relative min-w-[200px] flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            className="h-9 bg-background pl-9"
            placeholder="搜索模型 / 账号 / 错误"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
        </div>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <Filter className="size-3.5" /> 平台
        </div>
        <Select value={plat} onValueChange={setPlat}>
          <SelectTrigger className="h-9 w-[150px] bg-background">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部平台</SelectItem>
            {PLATFORMS.map((p) => (
              <SelectItem key={p.key} value={p.key}>
                {p.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
          状态
        </div>
        <Select value={status} onValueChange={(v) => setStatus(v as StatusFilter)}>
          <SelectTrigger className="h-9 w-[120px] bg-background">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">全部状态</SelectItem>
            <SelectItem value="success">成功</SelectItem>
            <SelectItem value="fail">失败</SelectItem>
          </SelectContent>
        </Select>
      </div>

      <SectionCard title="请求明细" desc="按时间倒序 · 悬停高亮" icon={ScrollText} tone="cyan">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-2.5 font-medium">时间</th>
                <th className="px-3 py-2.5 font-medium">平台</th>
                <th className="px-3 py-2.5 font-medium">账号</th>
                <th className="px-3 py-2.5 font-medium">模型</th>
                <th className="px-3 py-2.5 font-medium">流</th>
                <th className="px-3 py-2.5 font-medium">状态</th>
                <th className="px-3 py-2.5 text-right font-medium">耗时</th>
                <th className="px-3 py-2.5 text-right font-medium">输入字符</th>
                <th className="px-5 py-2.5 font-medium">错误</th>
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-5 py-10 text-center text-xs text-muted-foreground">
                    暂无匹配日志
                  </td>
                </tr>
              )}
              {filtered.map((l, i) => {
                const ok = l.status && l.status < 400;
                const t = toneFor(l.model || "x");
                return (
                  <tr
                    key={i}
                    className="border-t transition-colors hover:bg-muted"
                  >
                    <td className="whitespace-nowrap px-5 py-2.5 font-mono text-[11px] text-muted-foreground">
                      {fmtTs(l.ts)}
                    </td>
                    <td className="px-3 py-2.5">
                      {l.platform ? (
                        <Chip tone={toneFor(l.platform)}>{platName(l.platform)}</Chip>
                      ) : (
                        <span className="text-xs text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 font-mono text-xs">
                      {l.account_id ? `#${l.account_id}` : "-"}
                    </td>
                    <td className="px-3 py-2.5">
                      {l.model ? (
                        <span className="inline-flex h-5 items-center rounded-md bg-secondary px-1.5 font-mono text-[10px] text-secondary-foreground">
                          {l.model}
                        </span>
                      ) : (
                        <span className="text-xs text-muted-foreground">-</span>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      {l.stream ? (
                        <Chip tone="sky">流式</Chip>
                      ) : (
                        <Chip tone="slate">聚合</Chip>
                      )}
                    </td>
                    <td className="px-3 py-2.5">
                      {ok ? (
                        <span className="inline-flex items-center gap-1 text-emerald-600 dark:text-emerald-400">
                          <CheckCircle2 className="size-3.5" /> {l.status}
                        </span>
                      ) : (
                        <span className="inline-flex items-center gap-1 text-rose-600 dark:text-rose-400">
                          <XCircle className="size-3.5" /> {l.status}
                        </span>
                      )}
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-xs tabular-nums">
                      <span className="inline-flex items-center gap-1">
                        <Clock className="size-3 text-muted-foreground" /> {l.duration_ms || 0}ms
                      </span>
                    </td>
                    <td className="px-3 py-2.5 text-right font-mono text-xs tabular-nums text-muted-foreground">
                      {l.prompt_chars || 0}
                    </td>
                    <td className="max-w-[16rem] truncate px-5 py-2.5 text-xs text-muted-foreground" title={l.error || ""}>
                      {l.error ? l.error.slice(0, 40) : ""}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </SectionCard>
    </div>
  );
}
