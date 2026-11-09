"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
export function ProviderHealth() {
  const query = useQuery({ queryKey: ["provider-health"], queryFn: api.providerHealth, refetchInterval: 30000 });
  return <section aria-labelledby="provider-health-title" className="space-y-3"><h2 id="provider-health-title" className="text-sm font-semibold">Provider health</h2>
    {query.isPending && <p role="status" className="text-sm text-muted-foreground">Checking providers…</p>}
    {query.error && <p role="alert">Could not check providers: {query.error.message}</p>}
    <ul className="flex flex-wrap gap-2">{query.data?.items.map(item => <li key={`${item.kind}-${item.name}`} className="rounded-full border px-3 py-1 text-xs" title={item.error || `${item.latency_ms ?? "—"} ms`}><span className={item.ok ? "text-emerald-500" : "text-amber-500"}>{item.ok ? "●" : "○"}</span> {item.name} · {item.ok ? "Healthy" : "Unavailable"}</li>)}</ul>
    {query.data?.items.length === 0 && <p className="text-sm text-muted-foreground">No providers configured.</p>}
  </section>;
}
