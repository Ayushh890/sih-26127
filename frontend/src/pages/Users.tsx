/**
 * User & role administration (users:manage). Passwords are write-only: they are sent on
 * create / reset and never displayed. Roles are fixed server-side; shown as a permission matrix.
 */
import { useMemo, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { useAuth } from "../lib/auth";
import { fmtAgo, fmtTime } from "../lib/format";
import { ActionButton, Badge, Field, Modal, PageHeader, Panel, QueryState } from "../components/ui";

interface UserRow {
  id: number;
  username: string;
  full_name: string | null;
  role: string;
  is_active: boolean;
  is_demo: boolean;
  created_at: string | null;
  last_login_at: string | null;
}
interface RoleRow {
  name: string;
  description: string;
  permissions: string[];
}

const MIN_PW = 10; // UserCreate / UserUpdate password min_length

export default function Users() {
  const { user: me } = useAuth();
  const qc = useQueryClient();
  const users = useQuery({ queryKey: ["users"], queryFn: () => api.get<UserRow[]>("/api/users") });
  const roles = useQuery({ queryKey: ["users", "roles"], queryFn: () => api.get<RoleRow[]>("/api/users/roles"), staleTime: 300_000 });
  const [creating, setCreating] = useState(false);
  const [editing, setEditing] = useState<UserRow | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["users"] });
  const roleNames = roles.data?.map((r) => r.name) ?? [];

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Users & Roles"
        subtitle={
          <>
            Accounts are audited on every change. To change your own password use{" "}
            <Link to="/settings" className="text-cyan-300 hover:underline">
              Settings
            </Link>
            .
          </>
        }
        actions={
          <button className="btn btn-primary" onClick={() => setCreating(true)} disabled={!roles.data}>
            + New user
          </button>
        }
      />

      <Panel title="Users" subtitle={users.data ? `${users.data.length} accounts` : undefined} bodyClass="overflow-auto p-0">
        <QueryState q={users} isEmpty={(d) => d.length === 0} empty="No user accounts.">
          {(list) => (
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">Username</th>
                  <th className="th">Full name</th>
                  <th className="th">Role</th>
                  <th className="th">Status</th>
                  <th className="th">Created</th>
                  <th className="th">Last login</th>
                  <th className="th" />
                </tr>
              </thead>
              <tbody>
                {list.map((u) => {
                  const self = u.id === me?.id;
                  return (
                    <tr key={u.id} className={`hover:bg-ink-800/60 ${u.is_active ? "" : "opacity-60"}`}>
                      <td className="td mono">
                        {u.username} {self && <Badge tone="cyan">you</Badge>} {u.is_demo && <Badge tone="violet">Synthetic</Badge>}
                      </td>
                      <td className="td">{u.full_name || <span className="text-slate-600">—</span>}</td>
                      <td className="td">
                        <Badge tone={u.role === "admin" ? "red" : u.role === "operator" ? "orange" : u.role === "analyst" ? "blue" : "slate"}>{u.role}</Badge>
                      </td>
                      <td className="td">{u.is_active ? <Badge tone="green">active</Badge> : <Badge tone="slate">disabled</Badge>}</td>
                      <td className="td mono text-slate-400">{fmtTime(u.created_at, true)}</td>
                      <td className="td text-slate-400" title={u.last_login_at ? fmtTime(u.last_login_at, true) : undefined}>
                        {u.last_login_at ? fmtAgo(u.last_login_at) : "never"}
                      </td>
                      <td className="td whitespace-nowrap text-right">
                        <span className="inline-flex gap-1">
                          <button className="btn btn-ghost !px-1.5 !py-0" onClick={() => setEditing(u)} disabled={!roles.data}>
                            Edit
                          </button>
                          {!self && (
                            <ActionButton
                              className={`btn !px-1.5 !py-0 ${u.is_active ? "btn-danger" : ""}`}
                              confirm={u.is_active ? `Disable ${u.username}? They will no longer be able to sign in.` : undefined}
                              onClick={async () => {
                                await api.patch(`/api/users/${u.id}`, { is_active: !u.is_active });
                                await refresh();
                              }}
                            >
                              {u.is_active ? "Disable" : "Enable"}
                            </ActionButton>
                          )}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </QueryState>
      </Panel>

      <Panel title="Role permission matrix" subtitle="Roles and permissions are defined by the server" bodyClass="overflow-auto p-0">
        <QueryState q={roles} isEmpty={(d) => d.length === 0} empty="No roles returned.">
          {(rs) => <RoleMatrix roles={rs} />}
        </QueryState>
      </Panel>

      {creating && roles.data && <CreateUserModal roles={roleNames} onClose={() => setCreating(false)} onDone={refresh} />}
      {editing && roles.data && <EditUserModal user={editing} self={editing.id === me?.id} roles={roleNames} onClose={() => setEditing(null)} onDone={refresh} />}
    </div>
  );
}

function RoleMatrix({ roles }: { roles: RoleRow[] }) {
  const perms = useMemo(() => [...new Set(roles.flatMap((r) => r.permissions))].sort(), [roles]);
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr>
          <th className="th">Permission</th>
          {roles.map((r) => (
            <th key={r.name} className="th text-center" title={r.description}>
              {r.name}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        <tr>
          <td className="td text-slate-500">Description</td>
          {roles.map((r) => (
            <td key={r.name} className="td max-w-[220px] text-center text-[11px] text-slate-400">
              {r.description}
            </td>
          ))}
        </tr>
        {perms.map((p) => (
          <tr key={p} className="hover:bg-ink-800/60">
            <td className="td mono">{p}</td>
            {roles.map((r) => (
              <td key={r.name} className="td text-center">
                {r.permissions.includes(p) ? <span className="text-emerald-300" aria-label="granted">●</span> : <span className="text-slate-700" aria-label="not granted">·</span>}
              </td>
            ))}
          </tr>
        ))}
        <tr>
          <td className="td text-slate-500">Total</td>
          {roles.map((r) => (
            <td key={r.name} className="td mono text-center text-slate-400">
              {r.permissions.length}
            </td>
          ))}
        </tr>
      </tbody>
    </table>
  );
}

function CreateUserModal({ roles, onClose, onDone }: { roles: string[]; onClose: () => void; onDone: () => unknown }) {
  const [f, setF] = useState({ username: "", full_name: "", password: "", confirm: "", role: roles.includes("viewer") ? "viewer" : roles[0] ?? "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const mismatch = f.confirm.length > 0 && f.password !== f.confirm;
  const short = f.password.length > 0 && f.password.length < MIN_PW;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (mismatch || short) return;
    setBusy(true);
    setErr(null);
    try {
      await api.post("/api/users", { username: f.username.trim(), full_name: f.full_name.trim(), password: f.password, role: f.role });
      await onDone();
      onClose();
    } catch (ex) {
      setErr(errorText(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title="Create user" onClose={onClose}>
      <form onSubmit={submit} className="space-y-2" autoComplete="off">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Username" hint="3–64 chars: letters, digits, . _ -">
            <input className="input mono" value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} required minLength={3} maxLength={64} pattern="[a-zA-Z0-9_.\-]+" autoComplete="off" />
          </Field>
          <Field label="Full name">
            <input className="input" value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} maxLength={128} />
          </Field>
          <Field label="Password" hint={short ? <span className="text-rose-300">At least {MIN_PW} characters</span> : `Min. ${MIN_PW} characters — never displayed`}>
            <input className="input" type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} required minLength={MIN_PW} />
          </Field>
          <Field label="Confirm password" hint={mismatch ? <span className="text-rose-300">Passwords do not match</span> : undefined}>
            <input className="input" type="password" autoComplete="new-password" value={f.confirm} onChange={(e) => setF({ ...f, confirm: e.target.value })} required />
          </Field>
          <Field label="Role">
            <select className="input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })} required>
              {roles.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </Field>
        </div>
        {err && <div className="text-[12px] text-rose-300" role="alert">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || mismatch || short}>
            {busy ? "Creating…" : "Create user"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function EditUserModal({ user, self, roles, onClose, onDone }: { user: UserRow; self: boolean; roles: string[]; onClose: () => void; onDone: () => unknown }) {
  const [f, setF] = useState({ full_name: user.full_name ?? "", role: user.role, is_active: user.is_active, password: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const short = f.password.length > 0 && f.password.length < MIN_PW;

  async function submit(e: FormEvent) {
    e.preventDefault();
    if (short) return;
    const body: Record<string, unknown> = {};
    if (f.full_name !== (user.full_name ?? "")) body.full_name = f.full_name;
    if (f.role !== user.role) body.role = f.role;
    if (f.is_active !== user.is_active) body.is_active = f.is_active;
    if (f.password) body.password = f.password;
    if (Object.keys(body).length === 0) return onClose();
    setBusy(true);
    setErr(null);
    try {
      await api.patch(`/api/users/${user.id}`, body);
      await onDone();
      onClose();
    } catch (ex) {
      setErr(errorText(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit user ${user.username}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-2" autoComplete="off">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Full name">
            <input className="input" value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} maxLength={128} />
          </Field>
          <Field label="Role" hint={self ? "You cannot demote your own account" : undefined}>
            <select className="input" value={f.role} onChange={(e) => setF({ ...f, role: e.target.value })} disabled={self}>
              {roles.map((r) => (
                <option key={r}>{r}</option>
              ))}
            </select>
          </Field>
          <Field label="Reset password" hint={short ? <span className="text-rose-300">At least {MIN_PW} characters</span> : "Leave blank to keep the current password"}>
            <input className="input" type="password" autoComplete="new-password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} />
          </Field>
          <label className="flex items-end gap-1.5 pb-1.5 text-[12px]">
            <input type="checkbox" checked={f.is_active} disabled={self} onChange={(e) => setF({ ...f, is_active: e.target.checked })} /> Account active
          </label>
        </div>
        {err && <div className="text-[12px] text-rose-300" role="alert">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || short}>
            {busy ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
