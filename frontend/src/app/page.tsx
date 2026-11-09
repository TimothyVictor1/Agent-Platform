"use client";
import Link from "next/link";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Budgets, CreateSessionRequest } from "@/contracts/generated";
import { api } from "@/lib/api";
import { env } from "@/lib/env";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/shared/empty-state";
import { KeyboardShortcut } from "@/components/shared/keyboard-shortcut";
import { StatusChip } from "@/components/shared/status-chip";
import { ProviderHealth } from "@/components/provider-health";
import { useToast } from "@/components/providers";

export default function HomePage() {
  const router = useRouter(); const client = useQueryClient(); const toast = useToast();
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: api.profiles });
  const recent = useQuery({ queryKey: ["sessions"], queryFn: () => api.sessions({ limit: 10 }) });
  const [profile, setProfile] = useState("");
  const [validation, setValidation] = useState<string | null>(null);
  const selectedProfile = profile || profiles.data?.default_profile || profiles.data?.items[0]?.name || "";
  const create = useMutation({ mutationFn: api.createSession, onSuccess: session => {
    void client.invalidateQueries({ queryKey: ["sessions"] });
    toast("Session created. Preparing your plan.");
    router.push(`/sessions/${encodeURIComponent(session.id)}/plan`);
  } });
  function submit(form: HTMLFormElement) {
    if (create.isPending) return;
    const data = new FormData(form);
    const prompt = String(data.get("prompt") || "").trim();
    const count = Number(data.get("agent_count"));
    const provider = data.get("sandbox_provider"); const mode = data.get("mode");
    const stepLimit = Number(data.get("max_steps_per_task")); const totalLimit = Number(data.get("max_total_steps")); const minutes = Number(data.get("max_wall_minutes"));
    const cost = String(data.get("max_cost_usd") || ""); const tokens = String(data.get("max_tokens") || "");
    const invalidInteger = (value: number, max: number) => !Number.isInteger(value) || value < 1 || value > max;
    let error: string | null = null;
    if (!prompt || prompt.length > 8000) error = "Enter a prompt between 1 and 8,000 characters.";
    else if (invalidInteger(count, 6)) error = "Agent count must be a whole number between 1 and 6.";
    else if (!profiles.data?.items.some(item => item.name === selectedProfile)) error = "Select an available model profile.";
    else if (invalidInteger(stepLimit, 10000) || invalidInteger(totalLimit, 100000) || invalidInteger(minutes, 1440)) error = "Budgets require whole numbers: steps per task 1–10,000, total steps 1–100,000, minutes 1–1,440.";
    else if (cost && (!Number.isFinite(Number(cost)) || Number(cost) < 0)) error = "Cost budget must be zero or greater.";
    else if (tokens && (!Number.isInteger(Number(tokens)) || Number(tokens) < 1)) error = "Token budget must be a positive whole number.";
    else if (provider !== "docker_local" && provider !== "e2b") error = "Select a sandbox provider.";
    else if (mode !== "coordinated" && mode !== "parallel_isolated" && mode !== "solo") error = "Select a session mode.";
    setValidation(error); if (error) return;
    if ((provider !== "docker_local" && provider !== "e2b") || (mode !== "coordinated" && mode !== "parallel_isolated" && mode !== "solo")) return;
    const budgets: Budgets = { max_steps_per_task: stepLimit, max_total_steps: totalLimit, max_wall_minutes: minutes, max_cost_usd: cost ? Number(cost) : null, max_tokens: tokens ? Number(tokens) : null };
    const request: CreateSessionRequest = { prompt, agent_count: count, model_profile: selectedProfile, sandbox_provider: provider, mode, budgets };
    create.mutate(request);
  }
  return <main className="mx-auto w-full max-w-6xl flex-1 space-y-8 px-6 py-10">
    <div><p className="mb-2 text-xs font-medium uppercase tracking-widest text-muted-foreground">Mission control</p><h1 className="text-3xl font-semibold tracking-tight">{env.appName}</h1><p className="mt-2 text-muted-foreground">Give your team a goal. Review the plan before work begins.</p></div>
    <div className="grid gap-8 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
      <section className="rounded-xl border bg-card p-6" aria-labelledby="new-session-title"><h2 id="new-session-title" className="mb-5 text-lg font-semibold">New session</h2>
        <form noValidate onSubmit={event => { event.preventDefault(); submit(event.currentTarget); }} onKeyDown={event => { if ((event.metaKey || event.ctrlKey) && event.key === "Enter") { event.preventDefault(); event.currentTarget.requestSubmit(); } }}>
          <fieldset disabled={create.isPending} className="space-y-5">
            <div className="space-y-2"><label htmlFor="prompt">What should your agents do?</label><textarea id="prompt" name="prompt" rows={5} maxLength={8000} placeholder="Research three options, compare tradeoffs, and prepare a report…" aria-describedby={validation ? "form-validation" : undefined} /></div>
            <div className="grid gap-4 sm:grid-cols-3">
              <div className="space-y-2"><label htmlFor="agent_count">Agents</label><input id="agent_count" name="agent_count" type="number" min={1} max={6} step={1} defaultValue={3} /></div>
              <div className="space-y-2"><label htmlFor="model_profile">Model profile</label><select id="model_profile" name="model_profile" value={selectedProfile} onChange={event => setProfile(event.target.value)} disabled={!profiles.data?.items.length}>{!profiles.data?.items.length && <option value="">{profiles.isPending ? "Loading…" : "No profiles"}</option>}{profiles.data?.items.map(item => <option key={item.name} value={item.name}>{item.name}</option>)}</select></div>
              <div className="space-y-2"><label htmlFor="sandbox_provider">Sandbox provider</label><select id="sandbox_provider" name="sandbox_provider" defaultValue="docker_local"><option value="docker_local">Docker local</option><option value="e2b">E2B cloud</option></select></div>
            </div>
            <details className="rounded-lg border p-4"><summary className="cursor-pointer text-sm font-medium">Advanced</summary><div className="mt-4 grid gap-4 sm:grid-cols-2">
              <div className="space-y-2"><label htmlFor="mode">Mode</label><select id="mode" name="mode" defaultValue="coordinated"><option value="coordinated">Coordinated</option><option value="parallel_isolated">Parallel isolated</option><option value="solo">Solo</option></select></div>
              <div className="space-y-2"><label htmlFor="max_steps_per_task">Steps per task</label><input id="max_steps_per_task" name="max_steps_per_task" type="number" min={1} max={10000} defaultValue={150} /></div>
              <div className="space-y-2"><label htmlFor="max_total_steps">Total steps</label><input id="max_total_steps" name="max_total_steps" type="number" min={1} max={100000} defaultValue={1500} /></div>
              <div className="space-y-2"><label htmlFor="max_wall_minutes">Time budget (minutes)</label><input id="max_wall_minutes" name="max_wall_minutes" type="number" min={1} max={1440} defaultValue={60} /></div>
              <div className="space-y-2"><label htmlFor="max_cost_usd">Cost budget (USD, optional)</label><input id="max_cost_usd" name="max_cost_usd" type="number" min={0} step="any" placeholder="Unlimited" /></div>
              <div className="space-y-2"><label htmlFor="max_tokens">Token budget (optional)</label><input id="max_tokens" name="max_tokens" type="number" min={1} placeholder="Unlimited" /></div>
            </div></details>
            {validation && <p id="form-validation" role="alert" className="text-sm text-destructive">{validation}</p>}
            {create.error && <p role="alert" className="text-sm text-destructive">Could not create session: {create.error.message}</p>}
            {profiles.error && <p role="alert" className="text-sm text-destructive">Could not load profiles: {profiles.error.message}</p>}
            {profiles.data?.items.length === 0 && <p role="status">Configure a model profile before starting a session.</p>}
            <div className="flex items-center justify-between gap-4"><Button type="submit" disabled={create.isPending || !profiles.data?.items.length}>{create.isPending ? "Planning…" : "Plan"}</Button><KeyboardShortcut keys={["Ctrl / ⌘", "Enter"]} /></div>
          </fieldset>
        </form>
      </section>
      <aside className="space-y-6"><div className="rounded-xl border bg-card p-5"><ProviderHealth /></div><p className="px-1 text-sm leading-relaxed text-muted-foreground">Agents share progress and findings on the session board. You can pause, steer, or take control during a session.</p></aside>
    </div>
    <section aria-labelledby="recent-title" className="space-y-4"><h2 id="recent-title" className="text-lg font-semibold">Recent sessions</h2>
      {recent.isPending && <p role="status">Loading recent sessions…</p>}
      {recent.error && <p role="alert">Could not load sessions: {recent.error.message}</p>}
      {recent.data?.items.length === 0 && <EmptyState title="No sessions yet">Start with a goal above to create your first session.</EmptyState>}
      <ul className="divide-y rounded-xl border bg-card">{recent.data?.items.map(item => <li key={item.id}><Link className="flex flex-wrap items-center justify-between gap-3 p-4 hover:bg-muted/50" href={`/sessions/${encodeURIComponent(item.id)}${item.status === "planning" || item.status === "awaiting_approval" ? "/plan" : ""}`}><span className="font-medium">{item.title}</span><span className="flex items-center gap-3 text-xs text-muted-foreground"><span>{item.agent_count} agents</span><time dateTime={item.created_at}>{new Date(item.created_at).toLocaleDateString()}</time><StatusChip status={item.status} /></span></Link></li>)}</ul>
    </section>
  </main>;
}
