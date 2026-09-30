import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { errorText } from "../lib/api";
import { useAuth } from "../lib/auth";

export default function Login() {
  const { user, login } = useAuth();
  const loc = useLocation();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  if (user) {
    const from = (loc.state as { from?: string } | null)?.from ?? "/";
    return <Navigate to={from} replace />;
  }

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await login(username.trim(), password);
    } catch (ex) {
      setErr(errorText(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-ink-950 p-4">
      <form onSubmit={submit} className="w-full max-w-sm rounded-md border border-ink-700 bg-ink-900 p-6 shadow-2xl">
        <div className="mb-5">
          <div className="text-xl font-bold tracking-[0.3em] text-slate-100">NIRNAY</div>
          <div className="text-[12px] text-slate-400">One vehicle. Many cameras. One city-wide view.</div>
        </div>
        <label className="block">
          <span className="label">Username</span>
          <input className="input" autoComplete="username" value={username} onChange={(e) => setUsername(e.target.value)} required autoFocus />
        </label>
        <label className="mt-3 block">
          <span className="label">Password</span>
          <input className="input" type="password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {err && (
          <div className="mt-3 rounded border border-rose-700/60 bg-rose-950/40 px-2 py-1 text-[12px] text-rose-200" role="alert">
            {err}
          </div>
        )}
        <button className="btn btn-primary mt-4 w-full justify-center" disabled={busy}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
        <p className="mt-4 text-[11px] leading-snug text-slate-500">
          Authorized traffic-management use only. Access, searches and alert actions are audited. This system identifies vehicles, never people.
        </p>
      </form>
    </div>
  );
}
