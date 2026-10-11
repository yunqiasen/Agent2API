import { useEffect, useState, type ReactNode } from "react";
import { toast } from "sonner";
import { Settings as SettingsIcon, SlidersHorizontal, Coins, Users, Save, Route, Plus, Trash2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { PageHeader, SectionCard } from "@/lib/design";
import { PLATFORMS, apiGet, apiPost, type Settings as SettingsData } from "@/lib/api";

const STRATEGIES = [
  { v: "least_used", l: "最少使用 least_used" },
  { v: "round_robin", l: "轮询 round_robin" },
  { v: "random", l: "随机 random" },
];

function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: string }) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {children}
      {hint && <p className="text-xs text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Sel({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { v: string; l: string }[];
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger className="h-9 bg-background">
        <SelectValue />
      </SelectTrigger>
      <SelectContent>
        {options.map((o) => (
          <SelectItem key={o.v} value={o.v}>
            {o.l}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

const PLAT_ORDER = PLATFORMS.map((p) => ({ v: p.key, l: p.name }));

type PriorityCfg = { default: string[]; overrides: Record<string, string[]> };

function normalizeOrder(raw: unknown): string[] {
  const values = Array.isArray(raw) ? raw : [];
  const out: string[] = [];
  for (const value of values) {
    const key = String(value);
    if (PLAT_ORDER.some((p) => p.v === key) && !out.includes(key)) out.push(key);
  }
  for (const p of PLAT_ORDER) if (!out.includes(p.v)) out.push(p.v);
  return out;
}

function parsePriority(raw?: string): PriorityCfg {
  try {
    const d = JSON.parse(raw || "");
    if (d && typeof d === "object") {
      const source = d as { default?: unknown; overrides?: Record<string, unknown> };
      const overrides: Record<string, string[]> = {};
      if (source.overrides && typeof source.overrides === "object") {
        for (const [model, order] of Object.entries(source.overrides)) {
          overrides[model] = normalizeOrder(order);
        }
      }
      return { default: normalizeOrder(source.default), overrides };
    }
  } catch { /* ignore */ }
  return { default: PLAT_ORDER.map((p) => p.v), overrides: {} };
}

function OrderSels({ value, onChange }: { value: string[]; onChange: (v: string[]) => void }) {
  // 渠道下拉必须构成全排列：改一个位置 = 与被选中平台交换位置。
  // 旧实现允许重复值（如 [trae,trae,codebuddy]），导致平台"消失"且路由坏数据入库。
  const arr = PLAT_ORDER.map((_, i) => value[i] || PLAT_ORDER[i].v);
  return (
    <div className="grid grid-cols-2 xl:grid-cols-4 gap-2">
      {PLAT_ORDER.map((_, i) => (
        <Select
          key={i}
          value={arr[i]}
          onValueChange={(v) => {
            if (v === arr[i]) return;
            const next = [...arr];
            const j = next.indexOf(v);
            // 目标平台已在其它位置：交换，保证始终是渠道全排列
            if (j >= 0) {
              next[j] = next[i];
            }
            next[i] = v;
            onChange(next);
          }}
        >
          <SelectTrigger className="h-9 bg-background">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PLAT_ORDER.map((o) => (
              <SelectItem key={o.v} value={o.v}>
                {i + 1}. {o.l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ))}
    </div>
  );
}

export function Settings() {
  const [s, setS] = useState<SettingsData>({});
  const [saving, setSaving] = useState(false);
  const [prio, setPrio] = useState<PriorityCfg>({ default: PLAT_ORDER.map((p) => p.v), overrides: {} });
  const [ovModel, setOvModel] = useState("");

  function load() {
    apiGet<SettingsData>("/api/settings")
      .then((d) => {
        setS(d);
        setPrio(parsePriority(d.model_route_priority));
      })
      .catch((e) => toast.error(e instanceof Error ? e.message : "加载失败"));
  }
  useEffect(load, []);

  function set(k: keyof SettingsData, v: string) {
    setS((prev) => ({ ...prev, [k]: v }));
  }

  async function save() {
    setSaving(true);
    try {
      await apiPost("/api/settings", {
        default_model: s.default_model || "",
        strategy: s.strategy || "least_used",
        auto_refresh: s.auto_refresh || "1",
        refresh_ahead: s.refresh_ahead || "900",
        allow_model_passthrough: s.allow_model_passthrough || "1",
        model_base_override: s.model_base_override || "",
        log_retention: s.log_retention || "2000",
        system_prompt: s.system_prompt || "",
        auto_checkin: s.auto_checkin || "1",
        checkin_time: s.checkin_time || "00:00",
        credit_threshold: s.credit_threshold || "0",
        free_models: s.free_models || "",
        public_join_enabled: s.public_join_enabled || "1",
        model_route_priority: JSON.stringify(prio),
      });
      // 以后端规范化后的持久化值为准，保存后立刻回显，避免页面切换后顺序看似丢失。
      const persisted = await apiGet<SettingsData>("/api/settings");
      setS(persisted);
      setPrio(parsePriority(persisted.model_route_priority));
      toast.success("设置已保存");
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "保存失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mx-auto w-full max-w-[1100px] space-y-5 p-6">
      <PageHeader
        icon={SettingsIcon}
        title="设置"
        subtitle="运行参数与号池扩充开关"
        right={
          <Button
            onClick={save}
            disabled={saving}
          >
            <Save className="size-4" />
            {saving ? "保存中…" : "保存设置"}
          </Button>
        }
      />

      <SectionCard
        title="运行设置"
        desc="调度策略与令牌刷新"
        icon={SlidersHorizontal}
        tone="indigo"
        delay={60}
        bodyClassName="grid gap-4 p-5 sm:grid-cols-2"
      >
        <Field label="默认模型" hint="仅已探测的对话模型有效；留空自动选择注册表中的首个可用模型">
          <Input className="h-9 bg-background" value={s.default_model || ""} onChange={(e) => set("default_model", e.target.value)} />
        </Field>
        <Field label="调度策略">
          <Sel value={s.strategy || "least_used"} onChange={(v) => set("strategy", v)} options={STRATEGIES} />
        </Field>
        <Field label="自动刷新令牌">
          <Sel value={s.auto_refresh || "1"} onChange={(v) => set("auto_refresh", v)} options={[{ v: "1", l: "开启" }, { v: "0", l: "关闭" }]} />
        </Field>
        <Field label="提前刷新秒数">
          <Input type="number" className="h-9 bg-background" value={s.refresh_ahead || ""} onChange={(e) => set("refresh_ahead", e.target.value)} />
        </Field>
        <Field label="允许客户端指定模型">
          <Sel value={s.allow_model_passthrough || "1"} onChange={(v) => set("allow_model_passthrough", v)} options={[{ v: "1", l: "允许" }, { v: "0", l: "强制默认模型" }]} />
        </Field>
        <Field label="模型网关覆盖（留空用平台默认）">
          <Input className="h-9 bg-background font-mono text-xs" placeholder="https://copilot.tencent.com" value={s.model_base_override || ""} onChange={(e) => set("model_base_override", e.target.value)} />
        </Field>
        <Field label="日志保留条数">
          <Input type="number" className="h-9 bg-background" value={s.log_retention || ""} onChange={(e) => set("log_retention", e.target.value)} />
        </Field>
      </SectionCard>

      <SectionCard
        title="计费与积分"
        desc="统计直接读取上游 usage.credit，不按 token 人工换算"
        icon={Coins}
        tone="amber"
        delay={120}
        bodyClassName="grid gap-4 p-5 sm:grid-cols-2"
      >
        <Field label="积分阈值（低于此值禁止调用计费模型）">
          <Input type="number" min={0} className="h-9 bg-background" value={s.credit_threshold || ""} onChange={(e) => set("credit_threshold", e.target.value)} />
        </Field>
        <Field label="免费模型（逗号分隔，永不门控）" hint="只影响额度门控；实际积分仍以腾讯返回为准">
          <Input className="h-9 bg-background" placeholder="例如 hy3" value={s.free_models || ""} onChange={(e) => set("free_models", e.target.value)} />
        </Field>
      </SectionCard>

      <SectionCard
        title="号池与签到"
        desc="令牌续期、签到与公共接入"
        icon={Users}
        tone="emerald"
        delay={180}
        bodyClassName="grid gap-4 p-5 sm:grid-cols-2"
      >
        <Field label="每日自动签到">
          <Sel value={s.auto_checkin || "1"} onChange={(v) => set("auto_checkin", v)} options={[{ v: "1", l: "开启" }, { v: "0", l: "关闭" }]} />
        </Field>
        <Field label="自动签到时间">
          <Input type="time" className="h-9 bg-background" value={s.checkin_time || "00:00"} onChange={(e) => set("checkin_time", e.target.value)} />
        </Field>
        <Field label="兜底 system 提示词">
          <Input className="h-9 bg-background" placeholder="You are a helpful assistant." value={s.system_prompt || ""} onChange={(e) => set("system_prompt", e.target.value)} />
        </Field>
        <Field label="开放公共号池扩充（用户可扫码接入）">
          <Sel value={s.public_join_enabled || "1"} onChange={(v) => set("public_join_enabled", v)} options={[{ v: "1", l: "开启" }, { v: "0", l: "关闭" }]} />
        </Field>

      </SectionCard>

      <SectionCard
        title="模型调用优先级"
        desc="同名模型在多个渠道都存在时，按此顺序选择渠道；单模型可单独覆盖"
        icon={Route}
        tone="violet"
        delay={240}
        bodyClassName="space-y-5 p-5"
      >
        <Field label="全局默认顺序" hint={`外部请求不指定渠道时，按 ${PLAT_ORDER.map((_, i) => i + 1).join(" → ")} 顺序尝试`}>
          <OrderSels value={prio.default} onChange={(v) => setPrio({ ...prio, default: v })} />
        </Field>

        <div className="space-y-3">
          <Label>单模型覆盖</Label>
          {Object.entries(prio.overrides).map(([m, order]) => (
            <div key={m} className="flex items-center gap-2">
              <span className="w-44 truncate font-mono text-xs">{m}</span>
              <div className="flex-1">
                <OrderSels
                  value={order}
                  onChange={(v) => setPrio({ ...prio, overrides: { ...prio.overrides, [m]: v } })}
                />
              </div>
              <Button
                variant="ghost"
                size="icon"
                className="text-rose-600 dark:text-rose-400"
                onClick={() => {
                  const next = { ...prio.overrides };
                  delete next[m];
                  setPrio({ ...prio, overrides: next });
                }}
              >
                <Trash2 className="size-4" />
              </Button>
            </div>
          ))}
          <div className="flex items-center gap-2">
            <Input
              className="h-9 w-44 bg-background font-mono text-xs"
              placeholder="模型 ID，如 glm-5"
              value={ovModel}
              onChange={(e) => setOvModel(e.target.value)}
            />
            <Button
              variant="secondary"
              size="sm"
              className="gap-1"
              disabled={!ovModel.trim() || !!prio.overrides[ovModel.trim()]}
              onClick={() => {
                const m = ovModel.trim();
                setPrio({ ...prio, overrides: { ...prio.overrides, [m]: [...prio.default] } });
                setOvModel("");
              }}
            >
              <Plus className="size-4" /> 添加覆盖
            </Button>
          </div>
          <p className="text-xs text-muted-foreground">
            保存后立即生效，无需重启。未覆盖的模型走全局默认顺序。
          </p>
        </div>
      </SectionCard>

      <p className="text-xs text-muted-foreground">
        模型列表与发现已移至 <a href="/models" className="text-primary underline hover:opacity-80">模型</a> 页面（手动拉取 + 各平台可用模型展示）。
      </p>
    </div>
  );
}
