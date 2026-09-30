/** Operator workflow controls for one alert: acknowledge / resolve / reopen (all audited server-side). */
import { useState } from "react";
import { api, errorText } from "../../lib/api";
import { P, useAuth } from "../../lib/auth";
import { ActionButton, Field, Modal } from "../ui";
import { allowedActions, type Alert } from "./types";

type Action = "acknowledge" | "resolve" | "reopen";

const LABEL: Record<Action, string> = { acknowledge: "Acknowledge", resolve: "Resolve", reopen: "Reopen" };

export function actOnAlert(code: string, action: Action, note?: string): Promise<Alert> {
  const n = note?.trim();
  return api.post<Alert>(`/api/alerts/${encodeURIComponent(code)}/${action}`, n ? { note: n } : {});
}

/** Compact row controls: acknowledge is one click; resolve / reopen ask for a note first. */
export function AlertRowActions({ alert, onDone }: { alert: Alert; onDone: (a: Alert) => void }) {
  const { has } = useAuth();
  const [noteFor, setNoteFor] = useState<Action | null>(null);
  if (!has(P.ALERTS_ACT)) return null;
  const acts = allowedActions(alert.status);
  return (
    <div className="flex flex-wrap gap-1">
      {acts.includes("acknowledge") && (
        <ActionButton className="btn px-1.5 py-0.5 text-[11px]" onClick={async () => onDone(await actOnAlert(alert.code, "acknowledge"))}>
          Ack
        </ActionButton>
      )}
      {acts.includes("resolve") && (
        <button type="button" className="btn px-1.5 py-0.5 text-[11px]" onClick={() => setNoteFor("resolve")}>
          Resolve…
        </button>
      )}
      {acts.includes("reopen") && (
        <button type="button" className="btn px-1.5 py-0.5 text-[11px]" onClick={() => setNoteFor("reopen")}>
          Reopen…
        </button>
      )}
      {noteFor && (
        <NoteModal
          alert={alert}
          action={noteFor}
          onClose={() => setNoteFor(null)}
          onDone={(a) => {
            setNoteFor(null);
            onDone(a);
          }}
        />
      )}
    </div>
  );
}

function NoteModal({ alert, action, onClose, onDone }: { alert: Alert; action: Action; onClose: () => void; onDone: (a: Alert) => void }) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <Modal title={`${LABEL[action]} ${alert.code}`} onClose={onClose}>
      <form
        className="space-y-2"
        onSubmit={async (e) => {
          e.preventDefault();
          setBusy(true);
          setErr(null);
          try {
            onDone(await actOnAlert(alert.code, action, note));
          } catch (x) {
            setErr(errorText(x));
          } finally {
            setBusy(false);
          }
        }}
      >
        <div className="text-[12px] text-slate-300">{alert.title}</div>
        <Field label={action === "resolve" ? "Resolution note" : "Note"} hint="Recorded in the audit log with your username.">
          <textarea className="input h-24" maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} autoFocus />
        </Field>
        {err && <div className="text-[12px] text-rose-300">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className={action === "resolve" ? "btn btn-primary" : "btn"} disabled={busy}>
            {LABEL[action]}
          </button>
        </div>
      </form>
    </Modal>
  );
}

/** Full panel used on the alert detail page: one note field shared by every allowed action. */
export function AlertActionPanel({ alert, onDone }: { alert: Alert; onDone: (a: Alert) => void }) {
  const { has } = useAuth();
  const [note, setNote] = useState("");
  if (!has(P.ALERTS_ACT)) return <div className="text-[12px] text-slate-500">Your role cannot act on alerts (alerts:act).</div>;
  const acts = allowedActions(alert.status);
  if (!acts.length) return <div className="text-[12px] text-slate-500">No actions available for status {alert.status}.</div>;
  return (
    <div className="space-y-2">
      <Field label="Note (optional)" hint="Stored with the action in the audit history.">
        <textarea className="input h-16" maxLength={2000} value={note} onChange={(e) => setNote(e.target.value)} />
      </Field>
      <div className="flex flex-wrap gap-1.5">
        {acts.map((a) => (
          <ActionButton
            key={a}
            className={a === "resolve" ? "btn btn-primary" : "btn"}
            onClick={async () => {
              const r = await actOnAlert(alert.code, a, note);
              setNote("");
              onDone(r);
            }}
          >
            {LABEL[a]}
          </ActionButton>
        ))}
      </div>
    </div>
  );
}
