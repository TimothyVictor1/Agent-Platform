"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { useStore } from "zustand";
import { AlertDialog } from "radix-ui";
import type { Plan, ProblemError } from "@/contracts/generated";
import { api, ApiError } from "@/lib/api";
import { createSessionStore } from "@/lib/session-store";
import { SessionSocket, type ConnectionState } from "@/lib/ws";
import { Button } from "@/components/ui/button";
import { createDraftStore } from "./draft-store";
import { PlanGraph, type Selection } from "./plan-graph";
import { TaskEditor } from "./task-editor";
import { RefineDiff } from "./refine-diff";
import { fieldErrors, nodeErrors, validatePlan } from "./validation";

/** Commands are asynchronous; wait for the socket or a refreshed REST snapshot. */
export function waitForPlan(store: ReturnType<typeof createSessionStore>, predicate: (plan: Plan) => boolean): Promise<Plan> {
  return new Promise((resolve, reject) => {
    const current = store.getState().plan;
    if (current && predicate(current)) { resolve(current); return; }
    const timer = setTimeout(() => { unsubscribe(); reject(new Error("The updated plan has not arrived. Retry after reconnecting.")); }, 20000);
    const unsubscribe = store.subscribe(state => {
      if (state.plan && predicate(state.plan)) { clearTimeout(timer); unsubscribe(); resolve(state.plan); }
    });
  });
}

export function PlanReview({ sessionId }: { sessionId: string }) {
  const router = useRouter();
  const [sessionStore] = useState(() => createSessionStore(sessionId));
  const [draftStore] = useState(createDraftStore);
  const session = useStore(sessionStore);
  const draft = useStore(draftStore);
  const [connection, setConnection] = useState<ConnectionState>("disconnected");
  const [selection, setSelection] = useState<Selection>(null);
  const [deleteKey, setDeleteKey] = useState<string | null>(null);
  const [instruction, setInstruction] = useState("");
  const [candidate, setCandidate] = useState<Plan | null>(null);
  const [message, setMessage] = useState("");
  const [serverErrors, setServerErrors] = useState<ProblemError[]>([]);
  const [waiting, setWaiting] = useState(false);
  const locked = useRef(false);
  const detail = useQuery({ queryKey: ["session", sessionId], queryFn: () => api.session(sessionId), refetchInterval: waiting ? 600 : false });
  useEffect(() => { if (detail.data) sessionStore.getState().hydrate(detail.data); }, [detail.data, sessionStore]);
  useEffect(() => { if (session.plan) draftStore.getState().initialize(session.plan); }, [session.plan, draftStore]);
  useEffect(() => {
    if (!detail.isSuccess) return;
    const socket = new SessionSocket();
    socket.onEvent(event => sessionStore.getState().receive(event)); socket.onState(setConnection);
    socket.connect(sessionId, sessionStore.getState().lastSeq);
    return () => socket.disconnect();
  }, [detail.isSuccess, sessionId, sessionStore]);

  const proposal = draft.proposal;
  const tasks = proposal?.tasks ?? [];
  const clientErrors = validatePlan(tasks);
  const schemaErrors: ProblemError[] = Object.entries(draft.schemaErrors).map(([id, error]) => ({ message: `Invalid JSON schema: ${error}`, code: null, task_key: id.split(":")[0], loc: null, details: null }));
  const errors = [...clientErrors, ...fieldErrors(tasks), ...schemaErrors, ...serverErrors];
  const valid = Boolean(proposal) && errors.length === 0;
  const dirty = Boolean(proposal && draft.base && JSON.stringify(proposal.tasks) !== JSON.stringify(draft.base.proposal.tasks));

  async function saveDraft(force = false) {
    const { proposal, base } = draftStore.getState();
    if (!proposal || !base) throw new Error("The plan is still loading.");
    if (!force && JSON.stringify(proposal.tasks) === JSON.stringify(base.proposal.tasks)) return base;
    await api.updatePlan(sessionId, { tasks: proposal.tasks, base_version: base.version });
    const saved = await waitForPlan(sessionStore, plan => plan.version > base.version && JSON.stringify(plan.proposal.tasks) === JSON.stringify(proposal.tasks));
    draftStore.getState().saved(saved);
    return saved;
  }
  const action = useMutation({ mutationFn: async (kind: "save" | "approve" | "refine") => {
    const saved = await saveDraft(kind === "save");
    if (kind === "approve") { await api.approve(sessionId, { plan_version: saved.version }); router.push(`/sessions/${encodeURIComponent(sessionId)}`); }
    else if (kind === "refine") {
      const version = sessionStore.getState().plan?.version ?? saved.version;
      await api.refine(sessionId, { instruction: instruction.trim() });
      const next = await waitForPlan(sessionStore, plan => plan.version > version);
      setCandidate(next);
    } else setMessage("Plan saved.");
  }, onError: (error: Error) => {
    if (error instanceof ApiError) {
      // Structural validation errors may identify a node by request-array location.
      setServerErrors((error.problem.errors ?? []).map(item => {
        const index = item.loc?.findIndex(part => part === "tasks") ?? -1;
        const taskIndex = index >= 0 ? item.loc?.[index + 1] : undefined;
        return { ...item, task_key: item.task_key ?? (typeof taskIndex === "number" ? tasks[taskIndex]?.key ?? null : null) };
      }));
    }
    setMessage(error.message);
  }, onSettled: () => { locked.current = false; setWaiting(false); } });

  const execute = (kind: "save" | "approve" | "refine") => {
    if (locked.current || !valid || candidate || deleteKey || (kind === "refine" && !instruction.trim())) return;
    locked.current = true; setWaiting(true); setMessage(""); setServerErrors([]); action.mutate(kind);
  };
  const select = (next: Selection) => setSelection(next);
  const deleteSelected = () => {
    if (locked.current || candidate) return;
    if (selection?.type === "node") setDeleteKey(selection.key);
    if (selection?.type === "edge") { draftStore.getState().disconnect(selection.source, selection.target); setSelection(null); setServerErrors([]); }
  };
  useEffect(() => {
    const handle = (event: KeyboardEvent) => {
      if (event.target instanceof Element && event.target.closest("input, textarea, select, [contenteditable], [role='dialog'], [role='alertdialog']")) return;
      if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); execute("approve"); }
      if (event.key === "Delete" || event.key === "Backspace") { event.preventDefault(); deleteSelected(); }
    };
    window.addEventListener("keydown", handle); return () => window.removeEventListener("keydown", handle);
  });
  const busy = action.isPending;
  const selectedTask = selection?.type === "node" ? tasks.find(t => t.key === selection.key) : null;
  const statuses = Object.fromEntries(Object.values(session.tasks).map(task => [task.key, task.status]));

  return <main className="mx-auto w-full max-w-[1600px] space-y-5 px-6 py-8">
    <header className="flex flex-wrap items-start justify-between gap-4"><div><Link className="text-sm text-muted-foreground underline" href="/">Sessions</Link><h1 className="mt-2 text-3xl font-semibold">Plan review</h1><p className="mt-1 text-muted-foreground">{session.session?.title ?? "Loading session…"}</p></div><div className="flex items-center gap-2"><span className="mr-2 text-xs text-muted-foreground">{draft.base ? `Version ${draft.base.version}` : "Planning"}{dirty ? " · Unsaved edits" : ""}</span><Button variant="outline" disabled={!valid || busy || Boolean(candidate)} onClick={() => execute("save")}>Save plan</Button><Button disabled={!valid || busy || Boolean(candidate)} onClick={() => execute("approve")}>Approve plan</Button></div></header>
    <p role="status" className="text-xs text-muted-foreground">Connection: {connection}{busy ? " · Waiting for the plan command…" : ""} · Cmd/Ctrl+Enter to approve · Delete/Backspace to remove selection</p>
    {detail.error && <div role="alert" className="flex items-center gap-3 text-destructive">Could not load session: {detail.error.message}<Button variant="outline" onClick={() => void detail.refetch()}>Retry loading</Button></div>}
    {message && <p role={action.isError ? "alert" : "status"}>{message}</p>}
    {!proposal && !detail.error && <p role="status">Waiting for the planner’s proposal…</p>}
    {proposal && <>
      <section aria-label="Planner context" className="grid gap-5 rounded-xl border bg-card p-5 md:grid-cols-2"><div><h2 className="font-semibold">Assumptions</h2>{proposal.assumptions.length ? <ul className="mt-2 list-inside list-disc text-sm text-muted-foreground">{proposal.assumptions.map((text, i) => <li key={i}>{text}</li>)}</ul> : <p className="mt-2 text-sm text-muted-foreground">No assumptions listed.</p>}</div><div><h2 className="font-semibold">Questions for you</h2>{proposal.questions_for_user.length ? <ul className="mt-2 list-inside list-disc text-sm text-muted-foreground">{proposal.questions_for_user.map((text, i) => <li key={i}>{text}</li>)}</ul> : <p className="mt-2 text-sm text-muted-foreground">No open questions.</p>}</div></section>
      {errors.length > 0 && <section aria-label="Plan validation errors" className="rounded-lg border border-destructive/40 bg-destructive/5 p-3"><p className="text-sm font-semibold">Resolve {errors.length} validation issue{errors.length === 1 ? "" : "s"} before approving.</p><ul className="mt-2 space-y-1 text-sm">{errors.map((error, i) => <li key={i}>{error.task_key && <button className="mr-2 underline" onClick={() => setSelection({ type: "node", key: error.task_key! })}>{error.task_key}</button>}{error.message}</li>)}</ul>{serverErrors.length > 0 && <Button variant="outline" className="mt-3" onClick={() => { setServerErrors([]); setMessage(""); }}>Clear server errors after correcting</Button>}</section>}
      <fieldset disabled={busy || Boolean(candidate)} className="min-w-0 space-y-4 disabled:opacity-70" onChange={() => { setServerErrors([]); setMessage(""); }}>
        <legend className="sr-only">Plan editing</legend>
        <div className="flex flex-wrap items-center gap-3"><h2 className="mr-auto text-sm font-semibold">Task graph · {tasks.length}/12</h2><Button variant="outline" disabled={tasks.length >= 12 || busy || Boolean(candidate)} onClick={() => { const key = draftStore.getState().add(); setSelection({ type: "node", key }); setServerErrors([]); }}>Add task</Button><Button variant="outline" disabled={!selection || busy || Boolean(candidate)} onClick={deleteSelected}>Delete selected</Button></div>
        <div className="grid min-w-0 gap-5 xl:grid-cols-[minmax(0,1fr)_390px]">
          <div className={busy || candidate ? "pointer-events-none min-w-0" : "min-w-0"} inert={busy || Boolean(candidate)}><PlanGraph tasks={tasks} errors={errors} selection={selection} statuses={statuses} onSelect={select} onConnect={(source, target) => { draftStore.getState().connect(source, target); setServerErrors([]); }} /></div>
          {selectedTask ? <TaskEditor key={selectedTask.key} store={draftStore} taskKey={selectedTask.key} errors={nodeErrors(selectedTask.key, errors)} onDelete={() => setDeleteKey(selectedTask.key)} /> : <aside className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">Select a task to edit its goal, criteria, outputs and inputs. Select an edge and press Delete/Backspace to remove a dependency.</aside>}
        </div>
      </fieldset>
      <section aria-label="Refine plan" className="space-y-3 rounded-xl border bg-card p-5"><label className="block space-y-2"><span className="font-semibold">Refine with the planner</span><textarea rows={2} maxLength={2000} placeholder="Make it fewer tasks…" value={instruction} disabled={busy || Boolean(candidate)} onChange={e => setInstruction(e.target.value)} onKeyDown={event => {
        if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); event.stopPropagation(); execute("refine"); }
      }} /></label><Button variant="outline" disabled={!valid || busy || Boolean(candidate) || !instruction.trim()} onClick={() => execute("refine")}>Refine plan</Button></section>
      {candidate && <RefineDiff before={proposal} after={candidate.proposal} onAccept={() => { draftStore.getState().replace(candidate.proposal); draftStore.getState().saved(candidate); setCandidate(null); setSelection(null); setInstruction(""); }} onDiscard={() => { draftStore.getState().saved(candidate); setCandidate(null); setMessage("Refinement discarded. Save to restore the reviewed tasks on the server."); }} />}
    </>}
    <AlertDialog.Root open={deleteKey !== null} onOpenChange={open => { if (!open) setDeleteKey(null); }}><AlertDialog.Portal><AlertDialog.Overlay className="fixed inset-0 z-40 bg-black/60" /><AlertDialog.Content className="fixed top-1/2 left-1/2 z-50 w-[min(90vw,440px)] -translate-x-1/2 -translate-y-1/2 space-y-4 rounded-xl border bg-card p-6 shadow-xl"><AlertDialog.Title className="text-lg font-semibold">Delete task {deleteKey}?</AlertDialog.Title><AlertDialog.Description className="text-sm text-muted-foreground">This removes the task, its connected edges, and inputs wired from it.</AlertDialog.Description><div className="flex justify-end gap-2"><AlertDialog.Cancel asChild><Button variant="outline">Cancel deletion</Button></AlertDialog.Cancel><AlertDialog.Action asChild><Button variant="destructive" onClick={() => { if (deleteKey) draftStore.getState().remove(deleteKey); setSelection(null); setDeleteKey(null); setServerErrors([]); }}>Confirm delete task</Button></AlertDialog.Action></div></AlertDialog.Content></AlertDialog.Portal></AlertDialog.Root>
  </main>;
}
