import type { Budgets, Usage } from "@/contracts/generated";
export function CostMeter({ usage, budgets }: { usage: Usage; budgets: Budgets }) {
  const costLimit = budgets.max_cost_usd;
  const percent = costLimit === null ? Math.min(100, usage.steps / budgets.max_total_steps * 100) : costLimit === 0 ? 100 : Math.min(100, usage.cost_usd / costLimit * 100);
  return <div className="space-y-2 text-sm"><p>${usage.cost_usd.toFixed(3)}{costLimit === null ? " · No cost limit" : ` / $${costLimit.toFixed(2)}`} · {usage.steps} / {budgets.max_total_steps} steps</p><div role="progressbar" aria-label={costLimit === null ? "Step budget used" : "Cost budget used"} aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100} className="h-1.5 overflow-hidden rounded bg-muted"><div className="h-full bg-sky-500" style={{ width: `${percent}%` }} /></div></div>;
}
