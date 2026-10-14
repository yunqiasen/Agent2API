import { type DailyReward, num } from "@/lib/api";
import { Chip } from "@/lib/design";

export function rewardMessage(state?: DailyReward): string {
  if (!state) return "尚未核验活动";
  if (state.error) return `${state.error_stage || "查询"}失败：${state.error}`;
  if (state.reward_status === "credited") return `奖励已到账 +${num(state.amount ?? undefined)} 积分（${state.activity_date}）`;
  if (state.activity_status === "confirmed") return "活动已确认，奖励待到账";
  if (state.activity_status === "submitted") return "已提交待核验";
  if (state.activity_status === "unavailable") return state.reason || "活动不适用";
  if (state.activity_status === "not_completed") return "今日活动未完成";
  return "尚未核验活动";
}

function timestamp(value?: number | null) {
  return value ? new Date(value * 1000).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false }) : "未知";
}

export function DailyRewardCard({ state }: { state?: DailyReward }) {
  if (!state) return <Chip tone="slate">尚未核验活动</Chip>;
  return <div className="space-y-1.5 rounded-md border p-2 text-[11px]" aria-label="国际每日奖励">
    <div className="flex flex-wrap items-center gap-1.5">
      <span className="text-muted-foreground">每日活动</span>
      <Chip tone={state.error ? "rose" : state.activity_status === "confirmed" ? "emerald" : "amber"}>
        {state.activity_status === "confirmed" ? "活动已确认" : state.activity_status === "submitted" ? "已提交待核验" : state.activity_status === "unavailable" ? "不适用" : state.activity_status === "not_completed" ? "未完成" : "未核验"}
      </Chip>
    </div>
    <p>{rewardMessage(state)}</p>
    <p className="text-muted-foreground">活动日期：{state.activity_date} · Asia/Shanghai</p>
    {!!state.last_success_at && <p className="text-muted-foreground">活动查询：{timestamp(state.last_success_at)}{state.error ? "（保留上次记录）" : ""}</p>}
    {!!state.reward_checked_at && <p className="text-muted-foreground">奖励查询：{timestamp(state.reward_checked_at)}</p>}
    {!!state.rewards?.length && <details>
      <summary className="cursor-pointer">奖励记录（{state.rewards.length}，共享账本只计一次）</summary>
      <ul className="mt-1 space-y-2 break-all">
        {state.rewards.map(record => <li key={record.record_id}>
          <p>{record.kind === "daily_activity" ? "每日奖励" : "平台奖励（活动归属待核验）"}：{record.amount == null ? "金额未公布" : `+${num(record.amount)} 积分`}</p>
          <p>所属活动：{record.activity_date || "待核验"} · 发放：{timestamp(record.credited_at)}</p>
          <p className="text-muted-foreground">记录 {record.record_id}</p>
        </li>)}
      </ul>
    </details>}
  </div>;
}
