import { useEffect, useMemo, useRef, useState } from "react";
import {
  Coins,
  Gauge,
  TrendingUp,
  ArrowDownToLine,
  Users,
  KeyRound,
  Zap,
  CalendarRange,
  Receipt,
  ChevronRight,
  RefreshCcw,
  Sparkles,
  Activity,
  Wallet,
  Send,
  Server,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  apiGet,
  apiPost,
  type OverviewStats,
  type OverviewUsage,
  type ApiKey,
  PLATFORMS,
  platName,
  fmtTs,
  num,
} from "@/lib/api";
import {
  useCountUp,
  Tile,
  SectionCard,
  Progress,
  Copyable,
  Chip,
} from "@/lib/design";

/* ---------- hero pool card (solid primary accent, shadcn default) ---------- */
function HeroPool({
  remain,
  accounts,
  active,
  delay = 0,
}: {
  remain: number;
  accounts: number;
  active: number;
  delay?: number;
}) {
  const v = useCountUp(remain);
  return (
    <div
      className="relative col-span-2 overflow-hidden rounded-xl bg-primary p-5 text-primary-foreground animate-in fade-in slide-in-from-bottom-3 duration-700"
      style={{ animationDelay: `${delay}ms` }}
    >
      <div className="relative flex items-start justify-between">
        <div>
          <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-primary-foreground/80">
            <Sparkles className="size-3.5" /> 积分池剩余
          </div>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-[40px] font-semibold tabular-nums tracking-tight">
              {num(v)}
            </span>
            <span className="text-sm text-primary-foreground/70">credits</span>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2 text-xs text-primary-foreground/90">
            <span className="inline-flex items-center gap-1 rounded-full bg-primary-foreground/15 px-2.5 py-1 backdrop-blur">
              <Users className="size-3" /> {accounts} 个账号
            </span>
            <span className="inline-flex items-center gap-1 rounded-full bg-primary-foreground/15 px-2.5 py-1 backdrop-blur">
              <Activity className="size-3" /> 启用 {active}
            </span>
            <span className="inline-flex items-center gap-1 rounded-full bg-primary-foreground/15 px-2.5 py-1 backdrop-blur">
              <Wallet className="size-3" /> 实时聚合
            </span>
          </div>
        </div>
        <div className="flex size-14 items-center justify-center rounded-2xl bg-primary-foreground/15 backdrop-blur">
          <Wallet className="size-7 text-primary-foreground/90" />
        </div>
      </div>
    </div>
  );
}

/* ---------- smooth area chart (uses theme primary color) ---------- */
function UsageAreaChart({ series }: { series: OverviewUsage["series"] }) {
  const [hover, setHover] = useState<number | null>(null);
  const w = 720;
  const h = 240;
  const padL = 44;
  const padR = 16;
  const padT = 20;
  const padB = 30;
  const max = Math.max(1, ...series.map((s) => s.tokens));
  const n = Math.max(2, series.length);
  const step = (w - padL - padR) / (n - 1);
  const y = (v: number) => padT + (h - padT - padB) * (1 - v / max);
  const pts = series.map((s, i) => ({ x: padL + i * step, y: y(s.tokens), s }));

  const linePath = pts
    .map((p, i) => {
      if (i === 0) return `M ${p.x} ${p.y}`;
      const prev = pts[i - 1];
      const cx = (prev.x + p.x) / 2;
      return `C ${cx} ${prev.y}, ${cx} ${p.y}, ${p.x} ${p.y}`;
    })
    .join(" ");
  const areaPath =
    pts.length > 0
      ? `${linePath} L ${pts[pts.length - 1].x} ${h - padB} L ${pts[0].x} ${h - padB} Z`
      : "";

  const ticks = 4;
  const yTicks = Array.from({ length: ticks + 1 }, (_, i) =>
    Math.round(max * (1 - i / ticks))
  );
  const empty = series.every((s) => s.tokens === 0);

  return (
    <div className="relative text-primary">
      <svg viewBox={`0 0 ${w} ${h}`} className="h-[240px] w-full">
        {yTicks.map((t, i) => {
          const yy = y(t);
          return (
            <g key={i}>
              <line
                x1={padL}
                y1={yy}
                x2={w - padR}
                y2={yy}
                stroke="currentColor"
                strokeOpacity="0.08"
              />
              <text
                x={padL - 8}
                y={yy + 3}
                textAnchor="end"
                fontSize="10"
                fill="currentColor"
                opacity="0.45"
              >
                {num(t)}
              </text>
            </g>
          );
        })}
        {!empty && (
          <>
            <path d={areaPath} fill="currentColor" fillOpacity={0.12} className="transition-[d] duration-700" />
            <path
              d={linePath}
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              className="transition-[d] duration-700"
            />
          </>
        )}
        {pts.map((p, i) => (
          <g key={i}>
            {hover === i && (
              <line
                x1={p.x}
                y1={padT}
                x2={p.x}
                y2={h - padB}
                stroke="currentColor"
                strokeOpacity="0.4"
                strokeDasharray="3 3"
              />
            )}
            <circle
              cx={p.x}
              cy={p.y}
              r={hover === i ? 6 : 3.5}
              fill="#fff"
              stroke="currentColor"
              strokeWidth={hover === i ? 2.5 : 2}
              className="transition-all"
            />
            <rect
              x={p.x - step / 2}
              y={padT}
              width={step}
              height={h - padT - padB}
              fill="transparent"
              onMouseEnter={() => setHover(i)}
              onMouseLeave={() => setHover(null)}
              style={{ cursor: "pointer" }}
            />
          </g>
        ))}
        {pts.map((p, i) => {
          const d = new Date(parseInt(series[i].bucket, 10) * 1000);
          const lbl = `${d.getMonth() + 1}/${d.getDate()}`;
          return (
            <text
              key={i}
              x={p.x}
              y={h - 10}
              textAnchor="middle"
              fontSize="10"
              fill="currentColor"
              opacity="0.55"
            >
              {lbl}
            </text>
          );
        })}
        {empty && (
          <text
            x={w / 2}
            y={h / 2}
            textAnchor="middle"
            fontSize="12"
            fill="currentColor"
            opacity="0.4"
          >
            暂无用量数据
          </text>
        )}
      </svg>
      {hover != null && series[hover] && (
        <div
          className="pointer-events-none absolute z-10 rounded-md border bg-popover/95 px-3 py-2 text-xs shadow-lg backdrop-blur animate-in fade-in zoom-in-95 duration-150"
          style={{
            left: `clamp(8px, ${(pts[hover].x / w) * 100}%, calc(100% - 160px))`,
            top: 8,
          }}
        >
          <div className="font-medium tabular-nums">
            {num(series[hover].tokens)} tokens
          </div>
          <div className="text-muted-foreground">
            {num(series[hover].cost)} 积分
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- main ---------- */
export function Overview() {
  const [s, setS] = useState<OverviewStats | null>(null);
  const [u, setU] = useState<OverviewUsage | null>(null);
  const [keyFirst, setKeyFirst] = useState<string>("hy3api");
  const [testPrompt, setTestPrompt] = useState("用一句话介绍你自己");
  const [testPlatform, setTestPlatform] = useState("workbuddy");
  const [testOut, setTestOut] = useState<string>("");
  const [testing, setTesting] = useState(false);

  function refresh() {
    apiGet<OverviewStats>("/api/overview")
      .then((r) => {
        setS(r);
        if (r.platforms?.[0]?.key) setTestPlatform(r.platforms[0].key);
      })
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
    apiGet<OverviewUsage>("/api/overview/usage")
      .then(setU)
      .catch((e) => console.warn("usage load failed", e));
    apiGet<ApiKey[]>("/api/keys")
      .then((ks) => {
        const on = ks.find((k) => k.enabled) || ks[0];
        if (on) setKeyFirst(on.api_key);
      })
      .catch(() => {});
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 30_000);
    return () => clearInterval(t);
  }, []);

  const maxByModel = useMemo(
    () => (u ? Math.max(1, ...u.by_model.map((x) => x.tokens)) : 0),
    [u]
  );
  const maxByPlatform = useMemo(
    () => (u ? Math.max(1, ...u.by_platform.map((x) => x.tokens)) : 0),
    [u]
  );

  async function runTest() {
    setTesting(true);
    setTestOut("");
    try {
      const r = await apiPost<{ ok: boolean; error?: string; content?: string; ms?: number }>(
        "/api/test",
        { platform: testPlatform, prompt: testPrompt }
      );
      if (r.ok) {
        setTestOut(`✅ 成功 (${r.ms}ms)\n${r.content || ""}`);
        toast.success("连通性测试成功");
        refresh();
      } else {
        setTestOut(`❌ 失败：${r.error || "未知错误"}`);
        toast.error(r.error || "测试失败");
      }
    } catch (e) {
      setTestOut(`❌ 失败：${e instanceof Error ? e.message : String(e)}`);
      toast.error(e instanceof Error ? e.message : "测试异常");
    } finally {
      setTesting(false);
    }
  }

  const apiOrigin = typeof window !== "undefined" ? window.location.origin : "";
  const totalTokens7d = (u?.series || []).reduce((s, x) => s + x.tokens, 0);
  const totalCost7d = (u?.series || []).reduce((s, x) => s + x.cost, 0);

  return (
    <div className="mx-auto w-full max-w-[1280px] space-y-5 p-5 md:p-7">
      {/* header */}
      <div className="flex flex-wrap items-end justify-between gap-3 animate-in fade-in slide-in-from-top-2 duration-500">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Gauge className="size-5 text-primary" />
            概览
          </h1>
          <p className="mt-1 text-xs text-muted-foreground">
            号池状态 · Token 用量 · 积分扣费 · 实时连通性
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="outline" className="font-mono text-[10px]">
            累计 {num(totalTokens7d)} tokens · {num(totalCost7d)} 积分
          </Badge>
          <Button variant="outline" size="sm" onClick={refresh} className="gap-1">
            <RefreshCcw className="size-3.5" />刷新
          </Button>
        </div>
      </div>

      {/* hero + tiles row */}
      <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-6">
        <HeroPool
          remain={s?.quota_remain ?? 0}
          accounts={s?.accounts ?? 0}
          active={s?.accounts_active ?? 0}
          delay={0}
        />
        <Tile
          icon={TrendingUp}
          label="今日 Tokens"
          value={num(u?.today.tokens)}
          hint={
            u
              ? `prompt ${num(u.today.prompt_tokens)} · out ${num(u.today.completion_tokens)}`
              : ""
          }
          delay={80}
        />
        <Tile
          icon={ArrowDownToLine}
          label="今日积分消耗"
          value={num(u?.today.cost)}
          hint="credits"
          delay={160}
        />
        <Tile
          icon={Zap}
          label="今日调用"
          value={u?.today.calls ?? 0}
          hint="calls"
          delay={240}
        />
        <Tile
          icon={KeyRound}
          label="API Key"
          value={s?.keys ?? 0}
          hint="keys"
          delay={320}
        />
      </div>

      {/* access info + test */}
      <div className="grid gap-4 lg:grid-cols-3">
        <SectionCard
          title="接入信息"
          desc="客户端只需指向此 URL + Bearer Token 即可使用号池内任一模型"
          icon={Coins}
          className="lg:col-span-2"
          delay={120}
        >
          <div className="grid gap-4 px-5 py-4 sm:grid-cols-2">
            <div className="space-y-1.5">
              <Label>API 地址</Label>
              <div className="flex h-9 items-center gap-2 rounded-md border bg-muted/40 px-3 font-mono text-xs">
                <span className="truncate">{apiOrigin}/v1</span>
                <Copyable text={`${apiOrigin}/v1`} />
              </div>
            </div>
            <div className="space-y-1.5">
              <Label>可用模型</Label>
              <div className="flex h-9 items-center rounded-md border bg-muted/40 px-3 font-mono text-xs text-muted-foreground">
                {u ? `${u.by_model.length} 个模型 · ${(s?.platforms || []).length} 个平台` : "加载中…"}
              </div>
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <div className="flex items-center justify-between">
                <Label>curl 示例</Label>
                <Copyable
                  text={`curl -X POST ${apiOrigin}/v1/chat/completions \\\n  -H "Authorization: Bearer ${keyFirst}" \\\n  -H "Content-Type: application/json" \\\n  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}]}'`}
                />
              </div>
              <pre className="overflow-x-auto rounded-md border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed text-foreground">
{`curl -X POST ${apiOrigin}/v1/chat/completions \\
  -H "Authorization: Bearer ${keyFirst}" \\
  -H "Content-Type: application/json" \\
  -d '{"model":"hy3","messages":[{"role":"user","content":"你好"}]}'`}
              </pre>
            </div>
          </div>
        </SectionCard>

        <SectionCard
          title="连通性测试"
          desc="走完整反代链路，验证所选平台下号池调度"
          icon={Send}
          delay={200}
        >
          <div className="space-y-3 px-5 py-4">
            <div className="space-y-1.5">
              <Label>平台</Label>
              <Select value={testPlatform} onValueChange={setTestPlatform}>
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
            <div className="space-y-1.5">
              <Label>提示词</Label>
              <Input
                className="h-9 bg-background"
                value={testPrompt}
                onChange={(e) => setTestPrompt(e.target.value)}
              />
            </div>
            <Button onClick={runTest} disabled={testing} className="w-full gap-1.5">
              <Send className="size-3.5" />
              {testing ? "测试中…" : "发起连通性测试"}
            </Button>
            {testOut && (
              <pre className="max-h-40 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-3 font-mono text-[11px] leading-relaxed animate-in fade-in">
                {testOut}
              </pre>
            )}
          </div>
        </SectionCard>
      </div>

      {/* 7-day area chart */}
      <SectionCard
        title="近 7 天 Token 用量"
        desc="每日聚合 · 平滑曲线 · hover 查看明细"
        icon={CalendarRange}
        delay={160}
      >
        <div className="px-3 pb-4 pt-2">
          <UsageAreaChart series={u?.series || []} />
        </div>
      </SectionCard>

      {/* models + platforms */}
      <div className="grid gap-4 lg:grid-cols-2">
        <SectionCard
          title="各模型用量"
          desc="按上游返回的 token 总量降序 · 积分取 usage.credit"
          icon={TrendingUp}
          delay={200}
        >
          <div className="space-y-3 px-5 py-4">
            {(u?.by_model || []).length === 0 && (
              <p className="text-xs text-muted-foreground">暂无数据</p>
            )}
            {(u?.by_model || []).map((m) => (
              <div key={m.model} className="space-y-1.5">
                <div className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2">
                    <Chip>{m.model}</Chip>
                    <span className="text-muted-foreground">{m.calls} 次</span>
                  </span>
                  <span className="tabular-nums">
                    {num(m.tokens)} tokens ·{" "}
                    <span className="text-amber-600 dark:text-amber-400">{num(m.cost)}</span> 积分
                  </span>
                </div>
                <Progress value={m.tokens} max={maxByModel} />
              </div>
            ))}
          </div>
        </SectionCard>

        <SectionCard
          title="各平台用量"
          desc="按真实 token 用量展示 · 积分取上游 usage.credit"
          icon={Activity}
          delay={260}
        >
          <div className="space-y-3 px-5 py-4">
            {(u?.by_platform || []).length === 0 && (
              <p className="text-xs text-muted-foreground">暂无数据</p>
            )}
            {(u?.by_platform || []).map((p) => (
              <div key={p.platform} className="space-y-1.5">
                <div className="flex items-center justify-between text-xs">
                  <span className="flex items-center gap-2">
                    <Chip>{platName(p.platform)}</Chip>
                    <span className="text-muted-foreground">{p.calls} 次</span>
                  </span>
                  <span className="tabular-nums">
                    {num(p.tokens)} tokens ·{" "}
                    <span className="text-amber-600 dark:text-amber-400">{num(p.cost)}</span> 积分
                  </span>
                </div>
                <Progress value={p.tokens} max={maxByPlatform} />
              </div>
            ))}
          </div>
        </SectionCard>
      </div>

      {/* deductions */}
      <SectionCard
        title="调用扣费明细"
        desc="今日真实积分扣除 · 直接读取上游 usage.credit（不做人工换算）"
        icon={Receipt}
        right={
          <a
            href="/logs"
            className="flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-foreground"
          >
            查看完整日志 <ChevronRight className="size-3" />
          </a>
        }
        delay={220}
      >
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-left text-[11px] uppercase tracking-wider text-muted-foreground">
                <th className="px-5 py-2 font-medium">时间</th>
                <th className="px-3 py-2 font-medium">平台</th>
                <th className="px-3 py-2 font-medium">模型</th>
                <th className="px-3 py-2 text-right font-medium">tokens</th>
                <th className="px-5 py-2 text-right font-medium">扣除积分</th>
              </tr>
            </thead>
            <tbody>
              {(u?.deductions || []).length === 0 && (
                <tr>
                  <td
                    colSpan={5}
                    className="px-5 py-10 text-center text-xs text-muted-foreground"
                  >
                    今日暂无真实扣费记录（免费模型的上游 usage.credit 为 0）
                  </td>
                </tr>
              )}
              {(u?.deductions || []).map((d) => (
                <tr key={d.id} className="border-t transition-colors hover:bg-muted">
                  <td className="px-5 py-2 font-mono text-[11px] text-muted-foreground">
                    {fmtTs(d.ts)}
                  </td>
                  <td className="px-3 py-2">
                    <Chip>{platName(d.platform)}</Chip>
                  </td>
                  <td className="px-3 py-2">
                    <Chip>{d.model}</Chip>
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {num(d.tokens)}
                  </td>
                  <td className="px-5 py-2 text-right tabular-nums font-semibold text-rose-600 dark:text-rose-400">
                    −{num(d.cost)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </SectionCard>

      {/* platforms */}
      <SectionCard
        title="平台支持"
        desc="号池账号所在平台与上游网关"
        icon={Server}
        delay={260}
      >
        <div className="grid gap-3 px-5 py-4 sm:grid-cols-2 lg:grid-cols-3">
          {(s?.platforms || []).map((p) => (
            <div
              key={p.key}
              className="group rounded-lg border bg-background/60 p-3 transition-all hover:-translate-y-0.5 hover:shadow-sm"
            >
              <div className="flex items-center gap-2">
                <span className="inline-flex size-7 items-center justify-center rounded-md bg-muted text-muted-foreground">
                  <Server className="size-3.5" />
                </span>
                <span className="text-sm font-medium">{p.name}</span>
              </div>
              <div className="mt-2 space-y-1 text-[11px] text-muted-foreground">
                <div className="flex justify-between gap-2">
                  <span>登录域</span>
                  <span className="truncate font-mono text-foreground/70">{p.api_base}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span>模型网关</span>
                  <span className="truncate font-mono text-foreground/70">{p.model_base}</span>
                </div>
                {p.note && (
                  <div className="pt-1 text-foreground/70">{p.note}</div>
                )}
              </div>
            </div>
          ))}
        </div>
      </SectionCard>
    </div>
  );
}
