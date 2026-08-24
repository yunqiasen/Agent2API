import { useState, type ComponentType } from "react";
import { NavLink, Outlet, useLocation, useNavigate } from "react-router-dom";
import { useTheme } from "next-themes";
import { toast } from "sonner";
import {
  LayoutDashboard,
  Users,
  KeyRound,
  ScrollText,
  Settings,
  MoreHorizontal,
  LogOut,
  Sun,
  Moon,
  Monitor,
  Pencil,
  Boxes,
  Sparkles,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { apiPost } from "@/lib/api";

const NAV = [
  { to: "/dashboard", label: "概览", icon: LayoutDashboard },
  { to: "/accounts", label: "号池", icon: Users },
  { to: "/models", label: "模型", icon: Boxes },
  { to: "/keys", label: "API Key", icon: KeyRound },
  { to: "/logs", label: "调用日志", icon: ScrollText },
  { to: "/settings", label: "设置", icon: Settings },
];

function ThemeItem({
  icon: Icon,
  label,
  value,
}: {
  icon: ComponentType<{ className?: string }>;
  label: string;
  value: string;
}) {
  const { setTheme } = useTheme();
  return (
    <DropdownMenuItem onSelect={() => setTheme(value)}>
      <Icon className="size-4" />
      {label}
    </DropdownMenuItem>
  );
}

export function AppShell() {
  const location = useLocation();
  const navigate = useNavigate();
  const [pwOpen, setPwOpen] = useState(false);
  const [pw, setPw] = useState("");
  const [saving, setSaving] = useState(false);

  async function logout() {
    try {
      await apiPost("/api/logout");
    } finally {
      navigate("/", { replace: true });
    }
  }

  async function savePw() {
    if (pw.length < 4) {
      toast.error("密码至少 4 位");
      return;
    }
    setSaving(true);
    try {
      await apiPost("/api/password", { password: pw });
      toast.success("密码已修改");
      setPwOpen(false);
      setPw("");
    } catch (err) {
      toast.error(err instanceof Error ? err.message : "修改失败");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="flex min-h-screen bg-background">
      <aside className="fixed inset-y-0 left-0 z-30 hidden h-screen w-[276px] flex-col overflow-hidden border-r border-sidebar-border bg-sidebar/80 px-4 py-6 backdrop-blur lg:flex">
        <div className="flex h-8 shrink-0 items-center justify-between px-2">
          <span className="flex items-center gap-2.5">
            <span
              className="inline-flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground"
            >
              <Sparkles className="size-4" />
            </span>
            <span className="flex flex-col leading-tight">
              <span className="text-[15px] font-semibold tracking-tight text-foreground">
                hy3api
              </span>
              <span className="text-[10px] font-normal text-muted-foreground">
                v1 · 号池控制台
              </span>
            </span>
          </span>
          <Button
            variant="ghost"
            size="icon"
            className="size-8 text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground"
            onClick={() => navigate("/settings")}
            aria-label="设置"
          >
            <Settings className="size-4" />
          </Button>
        </div>

        <nav className="mt-7 min-h-0 flex-1 overflow-y-auto overscroll-contain pr-2 pb-2">
          <div className="space-y-0.5">
            {NAV.map((n) => {
              const active =
                location.pathname === n.to ||
                (n.to !== "/dashboard" && location.pathname.startsWith(n.to));
              const Icon = n.icon;
              return (
                <NavLink
                  key={n.to}
                  to={n.to}
                  className={`group relative flex items-center gap-2.5 rounded-lg px-3 py-2 text-[13px] transition-all duration-200 ${
                    active
                      ? "bg-primary font-medium text-primary-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-secondary hover:text-foreground"
                  }`}
                >
                  <Icon
                    className={`size-4 transition-transform duration-200 ${
                      active ? "scale-110" : "group-hover:scale-105"
                    }`}
                  />
                  {n.label}
                  {active && (
                    <span className="ml-auto size-1.5 rounded-full bg-white/80" />
                  )}
                </NavLink>
              );
            })}
          </div>
        </nav>

        <div className="relative z-10 mt-4 shrink-0 bg-sidebar pt-4">
          <div className="flex h-9 items-center gap-1 px-2.5">
            <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">管理员</span>
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="icon" className="size-8 text-muted-foreground" aria-label="更多">
                  <MoreHorizontal className="size-4" />
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent side="top" align="end" className="w-48">
                <ThemeItem icon={Sun} label="浅色" value="light" />
                <ThemeItem icon={Moon} label="深色" value="dark" />
                <ThemeItem icon={Monitor} label="跟随系统" value="system" />
                <DropdownMenuSeparator />
                <DropdownMenuItem onSelect={() => setPwOpen(true)}>
                  <Pencil className="size-4" />
                  修改密码
                </DropdownMenuItem>
                <DropdownMenuItem onSelect={logout}>
                  <LogOut className="size-4" />
                  退出登录
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          </div>
        </div>
      </aside>

      <div className="flex min-h-screen flex-1 flex-col lg:pl-[276px]">
        <Outlet />
      </div>

      <Dialog open={pwOpen} onOpenChange={setPwOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>修改管理员密码</DialogTitle>
            <DialogDescription>新密码至少 4 位。</DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Label htmlFor="newpw">新密码</Label>
            <Input
              id="newpw"
              type="password"
              value={pw}
              onChange={(e) => setPw(e.target.value)}
              className="h-9"
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setPwOpen(false)}>
              取消
            </Button>
            <Button onClick={savePw} disabled={saving}>
              {saving ? "保存中…" : "修改"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
