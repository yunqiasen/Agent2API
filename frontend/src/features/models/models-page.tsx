import { useEffect, useState } from "react";
import {
  Boxes,
  RefreshCw,
  ChevronDown,
  ChevronRight,
  CircleCheck,
  CircleX,
  Loader2,
  Server,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  apiGet,
  type ModelDiscoverResponse,
  type PlatformInfo,
  PLATFORMS,
} from "@/lib/api";
import {
  PageHeader,
  SectionCard,
  Chip,
  toneFor,
  type Tone,
} from "@/lib/design";

function platformLabel(k: string): string {
  return PLATFORMS.find((p) => p.key === k)?.label ?? k;
}

function kindBadge(kind?: string) {
  if (kind === "image") {
    return (
      <span className="rounded bg-violet-600/15 px-1 font-mono text-[9px] font-medium text-violet-600 dark:text-violet-400">
        图
      </span>
    );
  }
  if (kind === "audio") {
    return (
      <span className="rounded bg-emerald-600/15 px-1 font-mono text-[9px] font-medium text-emerald-600 dark:text-emerald-400">
        音
      </span>
    );
  }
  return null;
}

function ModelChip({ id, name, kind }: { id: string; name: string; kind?: string }) {
  return (
    <div className="relative">
      <span
        className="inline-flex flex-col items-start gap-0 rounded-lg border border-border bg-secondary px-2.5 py-1 text-xs transition-colors text-secondary-foreground"
      >
        <span className="flex items-center gap-1 leading-tight font-medium">
          {name}
          {kindBadge(kind)}
        </span>
        <span className="font-mono text-[10px] leading-tight opacity-70">↳ {id}</span>
      </span>
    </div>
  );
}

function PlatformCard({
  keyId,
  label,
  models,
  loading,
  note,
  delay = 0,
}: {
  keyId: string;
  label: string;
  models: { id: string; name: string; kind?: string }[];
  loading: boolean;
  note?: string;
  delay?: number;
}) {
  const [open, setOpen] = useState(true);
  const tone = toneFor(keyId);

  return (
    <SectionCard
      title={label}
      desc={`${keyId}`}
      icon={Server}
      tone={tone}
      delay={delay}
      right={
        <div className="flex items-center gap-3 text-xs text-muted-foreground">
          <span className="flex items-center gap-1">
            {models.length > 0 ? (
              <CircleCheck className="size-3.5 text-emerald-600 dark:text-emerald-400" />
            ) : (
              <CircleX className="size-3.5 text-muted-foreground" />
            )}
            {models.length} 个模型
          </span>
        </div>
      }
    >
      <div className="flex flex-col">
        <button
          type="button"
          onClick={() => setOpen((v) => !v)}
          className="flex items-center gap-1 px-5 py-2 text-xs text-muted-foreground transition-colors hover:text-foreground"
        >
          {open ? <ChevronDown className="size-3.5" /> : <ChevronRight className="size-3.5" />}
          {open ? "收起" : "展开"}模型列表
        </button>
        {open && (
          <div className="flex flex-wrap gap-1.5 px-5 pb-4 pt-1">
            {models.map((m) => (
              <ModelChip key={m.id} id={m.id} name={m.name} kind={m.kind} />
            ))}
            {models.length === 0 && !loading && (
              <p className="text-xs text-muted-foreground">
                没有可用模型（号池里无该平台账号时请先在「号池」添加）
              </p>
            )}
          </div>
        )}
        {note && <p className="px-5 pb-4 text-xs text-muted-foreground">{note}</p>}
      </div>
    </SectionCard>
  );
}

export function Models() {
  const [data, setData] = useState<ModelDiscoverResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [imageDiscovering, setImageDiscovering] = useState(false);
  const [lastFetched, setLastFetched] = useState<number | null>(null);

  // 后台图像探测轮询：discover 同步只返回文本/音频，生图模型由后台线程陆续写入
  // 注册表；轮询 progress 接口把新出现的图像模型实时展示出来（不阻塞刷新响应）。
  function pollImageProgress(round: number) {
    if (round <= 0) {
      setImageDiscovering(false);
      return;
    }
    window.setTimeout(async () => {
      try {
        const d = await apiGet<ModelDiscoverResponse>("/api/models/discover/progress");
        setData(d);
        setLastFetched(Date.now());
        if (d.image_discovering && round > 1) {
          pollImageProgress(round - 1);
        } else {
          setImageDiscovering(false);
          if (!d.image_discovering) {
            toast.success(`模型目录已更新，共收录 ${d.total ?? 0} 个模型（含生图）`);
          }
        }
      } catch {
        // 轮询失败不阻断：保留已展示数据，停止轮询
        setImageDiscovering(false);
      }
    }, 4000);
  }

  async function discover() {
    setLoading(true);
    try {
      const d = await apiGet<ModelDiscoverResponse>("/api/models/discover");
      setData(d);
      setLastFetched(Date.now());
      const total = d.platforms.reduce((s, p) => s + (p.models?.length || 0), 0);
      if (d.image_discovering) {
        toast.success(`文本/音频目录已刷新（${total} 条）；生图模型后台探测中，将自动出现…`);
        setImageDiscovering(true);
        pollImageProgress(60); // 每 4s 一次，约 240s 上限（后台图像探测预算 180s）
      } else {
        toast.success(`目录已刷新，共收录 ${total} 个平台模型条目`);
      }
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "拉取失败");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    discover();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const skeletons = [
    { k: "workbuddy", l: "WorkBuddy" },
    { k: "codebuddy_cn", l: "CodeBuddy CN" },
    { k: "codebuddy", l: "CodeBuddy 国际" },
  ];

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      <PageHeader
        icon={Boxes}
        title="模型"
        subtitle="各平台可用模型由真实账号探测得到（不内置目录）：探测到什么就提供什么，文本 / 图像 / 音频全部收录"
        right={
          <>
            <Button onClick={discover} disabled={loading} className="min-w-[150px] gap-1.5">
              {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
              刷新模型目录
            </Button>
          </>
        }
      />
      {lastFetched && (
        <p className="-mt-3 text-xs text-muted-foreground">
          上次拉取：{new Date(lastFetched).toLocaleString("zh-CN", { hour12: false })}
          {imageDiscovering && (
            <span className="ml-2 inline-flex items-center gap-1 text-amber-600">
              <Loader2 className="size-3 animate-spin" />
              生图模型后台探测中，即将自动出现…
            </span>
          )}
        </p>
      )}

      <SectionCard
        title="对外暴露（/v1/models）"
        desc="客户端 GET /v1/models 看到的清单，来自对外模型配置与内置目录合并"
        icon={Boxes}
        tone="indigo"
        right={<Badge variant="outline" className="font-mono text-[10px]">共 {data?.exposed?.length ?? 0} 个</Badge>}
      >
        <div className="flex flex-wrap gap-1.5 px-5 py-4">
          {(data?.exposed ?? []).map((m) => (
            <ModelChip key={m.id} id={m.id} name={m.name} kind={m.kind} />
          ))}
          {!data && <p className="text-xs text-muted-foreground">点击「刷新模型目录」获取</p>}
        </div>
      </SectionCard>

      <div className="space-y-4">
        {data?.platforms?.map((p, i) => (
          <PlatformCard
            key={p.platform}
            keyId={p.platform}
            label={p.label}
            models={p.models ?? []}
            loading={loading && data === null}
            note={p.note}
            delay={i * 60}
          />
        ))}
        {!data &&
          skeletons.map((s, i) => (
            <SectionCard
              key={s.k}
              title={s.l}
              desc={s.k}
              icon={Server}
              tone={toneFor(s.k)}
              delay={i * 60}
              right={<Loader2 className="size-4 animate-spin text-muted-foreground" />}
            >
              <div className="px-5 py-4 text-xs text-muted-foreground">探测中…</div>
            </SectionCard>
          ))}
      </div>
    </div>
  );
}

// re-export for type forwarding (avoid unused-imports)
export type _Platform = PlatformInfo;
