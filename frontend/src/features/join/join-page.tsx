import { useEffect, useRef, useState } from "react";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import {
  Check,
  ExternalLink,
  Lock,
  Moon,
  Sun,
  Coins,
  Heart,
  ShieldCheck,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { QRCodeSVG } from "qrcode.react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { PLATFORMS, apiPost, type JoinQuota, type JoinPollResp, type JoinStartResp } from "@/lib/api";

type Step = "form" | "auth" | "done" | "closed";

const TRANSFER_URL = "https://www.akucb.com/";

export function JoinPage() {
  const { resolvedTheme, setTheme } = useTheme();
  const [step, setStep] = useState<Step>("form");
  const [platform, setPlatform] = useState<string>(PLATFORMS[0].key);
  const [note, setNote] = useState("");
  const [authUrl, setAuthUrl] = useState("");
  const [state, setState] = useState("");
  const [log, setLog] = useState("等待授权…");
  const [done, setDone] = useState<{
    id: number;
    nickname?: string;
    email?: string;
    quota?: JoinQuota | null;
  } | null>(null);
  const [busy, setBusy] = useState(false);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, []);

  function stopTimer() {
    if (timer.current) {
      clearInterval(timer.current);
      timer.current = null;
    }
  }

  async function start() {
    setBusy(true);
    setLog("等待授权…");
    try {
      const d = await apiPost<JoinStartResp>("/api/join/start", {
        platform,
        label: note || "用户贡献",
      });
      setAuthUrl(d.auth_url);
      setState(d.state);
      setStep("auth");
      poll(d.state);
    } catch (err) {
      const msg = err instanceof Error ? err.message : "获取授权链接失败";
      if (/公共接入已关闭/.test(msg)) {
        setStep("closed");
      } else {
        toast.error(msg);
      }
      setBusy(false);
    }
  }

  function poll(st: string) {
    let n = 0;
    stopTimer();
    timer.current = setInterval(async () => {
      n += 1;
      if (n > 150) {
        stopTimer();
        setLog("超时，请返回重试");
        return;
      }
      try {
        const r = await apiPost<JoinPollResp>("/api/join/poll", { state: st });
        if (r.ok) {
          stopTimer();
          setDone({ id: r.account_id!, nickname: r.nickname, email: r.email, quota: r.quota });
          setStep("done");
          toast.success("已加入共享号池");
        } else {
          setLog(`等待授权… (${n}s) ${r.msg || r.error || ""}`);
        }
      } catch (e) {
        setLog("轮询出错：" + (e instanceof Error ? e.message : ""));
      }
    }, 2000);
  }

  function reset() {
    stopTimer();
    setState("");
    setAuthUrl("");
    setDone(null);
    setLog("等待授权…");
    setStep("form");
    setBusy(false);
  }

  return (
    <div className="relative flex min-h-screen flex-col bg-background">
      <header className="mx-auto flex h-16 w-full max-w-[1040px] items-center justify-between px-5 sm:px-8 lg:px-0">
        <span className="flex items-center gap-2.5">
          <span className="inline-flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Sparkles className="size-4" />
          </span>
          <span className="text-[15px] font-semibold tracking-tight text-foreground">
            Agent2API
          </span>
        </span>
        <div className="flex items-center gap-2">
          <a
            href={TRANSFER_URL}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
          >
            前往中转站使用
            <ExternalLink className="size-3.5" />
          </a>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 text-muted-foreground hover:bg-secondary hover:text-foreground"
            onClick={() => setTheme(resolvedTheme === "dark" ? "light" : "dark")}
            aria-label="切换主题"
          >
            {resolvedTheme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
          </Button>
        </div>
      </header>

      <main className="mx-auto flex w-full max-w-[1040px] flex-1 items-center justify-center px-5 py-10 sm:px-8 lg:px-0">
        <div className="grid w-full max-w-[900px] -translate-y-4 items-center lg:-translate-y-8 lg:grid-cols-[minmax(0,1fr)_1px_380px] lg:gap-14">
          <section className="hidden min-h-72 flex-col justify-center lg:flex">
            <p className="text-xs font-medium text-primary">号池扩充计划</p>
            <h2 className="mt-3 max-w-sm text-3xl font-semibold leading-tight tracking-tight">
              扫码接入
              <br />
              把你的账号贡献进共享号池
            </h2>
            <p className="mt-4 max-w-xs text-xs leading-6 text-muted-foreground">
              用手机扫码或网页登录授权后，你的账号会被加入 Agent2API
              的共享号池，帮助更多人稳定调用多平台模型。账号由管理员统一管理，可随时停用。
            </p>
            <ul className="mt-6 space-y-3 text-xs text-muted-foreground">
              <li className="flex items-center gap-2.5">
                <span className="inline-flex size-5 items-center justify-center rounded-md bg-emerald-500/10 text-emerald-600 dark:text-emerald-400">
                  <Check className="size-3" />
                </span>
                仅用于模型调用，不上传无关数据
              </li>
              <li className="flex items-center gap-2.5">
                <span className="inline-flex size-5 items-center justify-center rounded-md bg-primary/10 text-primary">
                  <RefreshCw className="size-3" />
                </span>
                令牌自动续期，无需重复授权
              </li>
              <li className="flex items-center gap-2.5">
                <span className="inline-flex size-5 items-center justify-center rounded-md bg-secondary text-secondary-foreground">
                  <ShieldCheck className="size-3" />
                </span>
                共享号池越大，调用越稳定越快速
              </li>
            </ul>
          </section>

          <div className="hidden h-64 bg-border lg:block" aria-hidden="true" />

          <section className="w-full max-w-[380px] justify-self-center lg:justify-self-auto">
            <div className="overflow-hidden rounded-2xl border bg-card shadow-sm">
              <div className="flex items-center gap-2 border-b bg-muted px-5 py-3.5 text-sm font-semibold text-foreground">
                <Sparkles className="size-4" />
                贡献你的账号
              </div>

              <div className="p-5">
                {step === "form" && (
                  <div className="animate-in fade-in slide-in-from-bottom-2">
                    <p className="text-xs leading-5 text-muted-foreground lg:hidden">
                      授权后你的账号会加入共享号池，帮助更多人稳定调用多平台模型。
                    </p>
                    <div className="mt-4 space-y-4">
                      <div className="space-y-2">
                        <Label htmlFor="note">昵称（可选，便于管理员识别）</Label>
                        <Input
                          id="note"
                          className="h-9 bg-background"
                          maxLength={40}
                          placeholder="例如 小助手 / 热心用户"
                          value={note}
                          onChange={(e) => setNote(e.target.value)}
                        />
                      </div>
                      <div className="space-y-2">
                        <Label>账号类型</Label>
                        <Tabs value={platform} onValueChange={setPlatform}>
                          <TabsList className="grid w-full grid-cols-3">
                            {PLATFORMS.map((p) => (
                              <TabsTrigger key={p.key} value={p.key} className="text-[11px]">
                                {p.name}
                              </TabsTrigger>
                            ))}
                          </TabsList>
                        </Tabs>
                      </div>
                      <Button
                        className="w-full"
                        onClick={start}
                        disabled={busy}
                      >
                        {busy ? "获取中…" : "获取登陆链接"}
                      </Button>
                    </div>
                  </div>
                )}

                {step === "auth" && (
                  <div className="animate-in fade-in slide-in-from-bottom-2">
                    <h1 className="text-base font-medium">完成授权</h1>
                    <p className="mt-1.5 text-xs leading-5 text-muted-foreground">
                      选择下方任一方式完成登录，本页会自动检测授权结果。
                    </p>
                    <Tabs defaultValue="scan" className="mt-4">
                      <TabsList className="grid w-full grid-cols-2">
                        <TabsTrigger value="scan">扫码登录</TabsTrigger>
                        <TabsTrigger value="web">网页登录</TabsTrigger>
                      </TabsList>
                      <TabsContent value="scan" className="flex flex-col items-center gap-3 pt-4">
                        <div className="rounded-lg bg-white p-3">
                          {authUrl ? (
                            <QRCodeSVG value={authUrl} size={176} level="M" />
                          ) : null}
                        </div>
                        <p className="text-center text-xs text-muted-foreground">
                          用手机扫描上方二维码，在浏览器中完成授权后本页会自动检测。
                        </p>
                      </TabsContent>
                      <TabsContent value="web" className="space-y-3 pt-4">
                        <div className="space-y-2">
                          <Label htmlFor="authurl">授权链接</Label>
                          <textarea
                            id="authurl"
                            readOnly
                            rows={3}
                            value={authUrl}
                            className="w-full rounded-md border border-input bg-background px-2.5 py-2 font-mono text-xs"
                          />
                          <p className="text-xs text-muted-foreground">
                            点击下方按钮在新标签页打开并完成登录，本页会自动检测。
                          </p>
                        </div>
                        <Button
                          variant="secondary"
                          className="w-full"
                          onClick={() => window.open(authUrl, "_blank")}
                        >
                          <ExternalLink className="size-4" />
                          在浏览器打开
                        </Button>
                      </TabsContent>
                    </Tabs>
                    <pre className="mt-3 whitespace-pre-wrap break-all rounded-md bg-muted/40 p-3 text-xs text-muted-foreground">
                      {log}
                    </pre>
                    <Button
                      variant="ghost"
                      className="mt-3 w-full text-muted-foreground hover:text-foreground"
                      onClick={reset}
                    >
                      取消
                    </Button>
                  </div>
                )}

                {step === "done" && (
                  <div className="flex flex-col items-center py-1 text-center animate-in fade-in zoom-in-95">
                    <div className="flex size-14 items-center justify-center rounded-full bg-emerald-500/10">
                      <Check className="size-7 text-emerald-600 dark:text-emerald-400" />
                    </div>
                    <h1 className="mt-4 text-lg font-semibold tracking-tight">已加入共享号池</h1>
                    <p className="mt-2 flex items-center gap-1.5 text-xs leading-5 text-muted-foreground">
                      <Heart className="size-3.5 text-rose-500" />
                      感谢你的贡献！你的账号正在帮助更多人稳定调用模型。
                    </p>
                    {done && (
                      <div className="mt-4 w-full space-y-3 text-left">
                        <div className="rounded-xl border bg-muted p-4">
                          <div className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wider text-muted-foreground">
                            <Coins className="size-3.5 text-primary" /> 你的账号额度
                          </div>
                          <div className="mt-1.5 flex items-baseline gap-1.5">
                            <span className="text-2xl font-semibold tabular-nums text-foreground">
                              {(done.quota?.remain ?? 0).toLocaleString()}
                            </span>
                            <span className="text-xs text-muted-foreground">
                              / {(done.quota?.total ?? 0).toLocaleString()}{" "}
                              {done.quota?.unit ?? "credits"} 剩余
                            </span>
                          </div>
                          {done.quota?.packages && done.quota.packages.length > 0 && (
                            <div className="mt-3 space-y-1.5 border-t border-border pt-3">
                              {done.quota.packages.map((p, i) => (
                                <div key={i} className="flex items-center justify-between text-xs">
                                  <span className="text-muted-foreground">{p.name ?? "套餐"}</span>
                                  <span className="font-medium tabular-nums">
                                    {(p.remain ?? 0).toLocaleString()} /{" "}
                                    {(p.total ?? 0).toLocaleString()} {p.unit ?? done.quota?.unit}
                                  </span>
                                </div>
                              ))}
                            </div>
                          )}
                        </div>
                        <div className="rounded-lg border bg-background p-3 text-xs">
                          <div className="flex items-center justify-between">
                            <span className="text-muted-foreground">账号 ID</span>
                            <span className="font-mono">#{done.id}</span>
                          </div>
                          {(done.nickname || done.email) && (
                            <div className="mt-1.5 flex items-center justify-between">
                              <span className="text-muted-foreground">账号</span>
                              <span className="font-medium">
                                {done.nickname}{" "}
                                <span className="font-normal text-muted-foreground">{done.email}</span>
                              </span>
                            </div>
                          )}
                        </div>
                      </div>
                    )}
                    <a
                      href={TRANSFER_URL}
                      target="_blank"
                      rel="noreferrer"
                      className="mt-5 inline-flex w-full items-center justify-center gap-1.5 rounded-lg border bg-background px-4 py-2.5 text-sm font-medium text-foreground transition-colors hover:bg-secondary"
                    >
                      前往中转站使用
                      <ExternalLink className="size-4" />
                    </a>
                    <Button
                      variant="outline"
                      className="mt-2 w-full"
                      onClick={reset}
                    >
                      再添加一个账号
                    </Button>
                  </div>
                )}

                {step === "closed" && (
                  <div className="flex flex-col items-center py-6 text-center animate-in fade-in zoom-in-95">
                    <div className="flex size-12 items-center justify-center rounded-full bg-muted">
                      <Lock className="size-6 text-muted-foreground" />
                    </div>
                    <h1 className="mt-4 text-lg font-medium">公共接入已关闭</h1>
                    <p className="mt-2 text-xs leading-5 text-muted-foreground">
                      当前管理员未开放扫码扩充号池，如有需要请联系管理员开启。
                    </p>
                  </div>
                )}
              </div>
            </div>

            <p className="mt-4 text-center text-xs text-muted-foreground">
              已是管理员？
              <a href="/login" className="ml-1 text-primary underline hover:opacity-80">
                进入后台
              </a>
            </p>
          </section>
        </div>
      </main>

      <footer className="mx-auto w-full max-w-[1040px] px-5 py-6 text-center text-xs text-muted-foreground sm:px-8">
        Agent2API · 共享号池与中转站
      </footer>
    </div>
  );
}
