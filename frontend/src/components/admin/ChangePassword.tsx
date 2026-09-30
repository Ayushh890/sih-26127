/** "Change my password" — POST /api/auth/password; available to every signed-in user. */
import { useState, type FormEvent } from "react";
import { api, errorText } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Field, Panel } from "../ui";

const MIN_LEN = 10; // PasswordChange.new_password min_length on the backend

export function ChangePassword() {
  const { user } = useAuth();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const mismatch = confirm.length > 0 && next !== confirm;
  const tooShort = next.length > 0 && next.length < MIN_LEN;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (mismatch || tooShort) return;
    setBusy(true);
    setMsg(null);
    try {
      await api.post("/api/auth/password", { current_password: current, new_password: next });
      setMsg({ ok: true, text: "Password changed. Use the new password at your next sign-in." });
      setCurrent("");
      setNext("");
      setConfirm("");
    } catch (ex) {
      setMsg({ ok: false, text: errorText(ex) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel title="Change my password" subtitle={user ? `Signed in as ${user.username} (${user.role})` : undefined}>
      <form onSubmit={submit} className="grid grid-cols-1 gap-2 sm:grid-cols-4 sm:items-end" autoComplete="off">
        <Field label="Current password">
          <input className="input" type="password" autoComplete="current-password" value={current} onChange={(e) => setCurrent(e.target.value)} required />
        </Field>
        <Field label="New password" hint={tooShort ? <span className="text-rose-300">At least {MIN_LEN} characters</span> : `Min. ${MIN_LEN} characters`}>
          <input className="input" type="password" autoComplete="new-password" value={next} onChange={(e) => setNext(e.target.value)} required minLength={MIN_LEN} />
        </Field>
        <Field label="Confirm new password" hint={mismatch ? <span className="text-rose-300">Passwords do not match</span> : undefined}>
          <input className="input" type="password" autoComplete="new-password" value={confirm} onChange={(e) => setConfirm(e.target.value)} required />
        </Field>
        <div className="pb-[18px]">
          <button className="btn btn-primary" disabled={busy || mismatch || tooShort || !current || !next || !confirm}>
            {busy ? "Saving…" : "Change password"}
          </button>
        </div>
      </form>
      {msg && (
        <div role={msg.ok ? "status" : "alert"} className={`mt-1 text-[12px] ${msg.ok ? "text-emerald-300" : "text-rose-300"}`}>
          {msg.text}
        </div>
      )}
    </Panel>
  );
}
