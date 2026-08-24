import { useEffect, useState, type ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { Spinner } from "@/components/ui/spinner";
import { apiGet } from "@/lib/api";
import { LoginPage } from "@/features/auth/login-page";
import { JoinPage } from "@/features/join/join-page";
import { AppShell } from "@/app/app-shell";
import { Overview } from "@/features/dashboard/overview";
import { Accounts } from "@/features/accounts/accounts";
import { Models } from "@/features/models/models-page";
import { Keys } from "@/features/keys/keys";
import { Logs } from "@/features/logs/logs";
import { Settings } from "@/features/settings/settings";

function RequireAuth({ children }: { children: ReactNode }) {
  const [state, setState] = useState<"loading" | "ok" | "no">("loading");
  useEffect(() => {
    apiGet<{ admin: boolean }>("/api/me")
      .then((r) => setState(r.admin ? "ok" : "no"))
      .catch(() => setState("no"));
  }, []);
  if (state === "loading")
    return (
      <div className="flex min-h-screen items-center justify-center bg-background text-muted-foreground">
        <Spinner className="size-6" />
      </div>
    );
  if (state === "no") return <Navigate to="/" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/" element={<JoinPage />} />
      <Route path="/login" element={<LoginPage />} />
      <Route path="/join" element={<JoinPage />} />
      <Route
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route path="/dashboard" element={<Overview />} />
        <Route path="/accounts" element={<Accounts />} />
        <Route path="/models" element={<Models />} />
        <Route path="/keys" element={<Keys />} />
        <Route path="/logs" element={<Logs />} />
        <Route path="/settings" element={<Settings />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}
