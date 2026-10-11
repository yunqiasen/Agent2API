import { useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import { toast } from "sonner";
import { ExternalLink, KeyRound, Send, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { apiPost } from "@/lib/api";

const TRANSFER_URL = "https://www.akucb.com/";

export function LoginPage() {
  const navigate = useNavigate();
  const [pw, setPw] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (!pw) {
      toast.error("请输入管理员密码");
      return;
    }
    setSubmitting(true);
    try {
      await apiPost("/api/login", { password: pw });
      navigate("/dashboard", { replace: true });
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "登录失败");
    } finally {
      setSubmitting(false);
    }
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
        <a
          href={TRANSFER_URL}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-8 items-center gap-1.5 rounded-lg border bg-background px-3 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
        >
          前往中转站使用
          <ExternalLink className="size-3.5" />
        </a>
      </header>

      <main className="mx-auto flex w-full max-w-[1040px] flex-1 items-center justify-center px-5 py-10 sm:px-8 lg:px-0">
        <div className="grid w-full max-w-[900px] -translate-y-4 items-center lg:-translate-y-8 lg:grid-cols-[minmax(0,1fr)_1px_336px] lg:gap-14">
          <section className="hidden min-h-72 flex-col justify-center lg:flex">
            <p className="text-xs font-medium text-primary">Agent2API 控制台</p>
            <h2 className="mt-3 max-w-sm text-3xl font-semibold leading-tight tracking-tight">
              聚合多平台账号
              <br />
              OpenAI 兼容中转
            </h2>
            <p className="mt-4 max-w-xs text-xs leading-6 text-muted-foreground">
              统一管理 WorkBuddy / TraeWork / CodeBuddy 国际 / WorkBuddy 国际 的账号，
              对外提供 OpenAI 兼容接口（含 Responses API）与共享号池调度。
            </p>
            <a
              href="/join"
              className="mt-6 inline-flex w-fit items-center gap-1.5 rounded-lg border bg-background px-3 py-2 text-xs font-medium text-foreground transition-colors hover:bg-secondary"
            >
              <Send className="size-3.5" />
              我要贡献我的账号
            </a>
          </section>

          <div className="hidden h-64 bg-border lg:block" aria-hidden="true" />

          <section className="w-full max-w-[336px] justify-self-center lg:justify-self-auto">
            <div className="overflow-hidden rounded-2xl border bg-card shadow-sm">
              <div className="flex items-center gap-2 border-b bg-muted px-5 py-3.5 text-sm font-semibold text-foreground">
                <KeyRound className="size-4" />
                登录后台
              </div>
              <div className="p-5">
                <p className="text-xs leading-5 text-muted-foreground lg:hidden">
                  聚合多平台账号 · OpenAI 兼容中转
                </p>
                <form className="mt-4 space-y-4" onSubmit={submit}>
                  <div className="space-y-2">
                    <Label htmlFor="password">管理员密码</Label>
                    <Input
                      id="password"
                      type="password"
                      autoComplete="current-password"
                      autoFocus
                      value={pw}
                      onChange={(e) => setPw(e.target.value)}
                      className="h-9 bg-background"
                      placeholder="请输入管理员密码"
                    />
                  </div>
                  <Button
                    type="submit"
                    size="sm"
                    className="w-full"
                    disabled={submitting}
                  >
                    {submitting ? "登录中…" : "登录"}
                  </Button>
                </form>
                <a
                  href="/join"
                  className="mt-4 flex items-center justify-center gap-1.5 text-xs text-muted-foreground transition-colors hover:text-foreground lg:hidden"
                >
                  <Send className="size-3.5" />
                  我要贡献我的账号
                </a>
              </div>
            </div>
          </section>
        </div>
      </main>

      <footer className="mx-auto w-full max-w-[1040px] px-5 py-6 text-center text-xs text-muted-foreground sm:px-8">
        Agent2API · 共享号池与中转站
      </footer>
    </div>
  );
}
