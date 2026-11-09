import type { PlanProposal } from "@/contracts/generated";
import { Button } from "@/components/ui/button";
import { diffPlans } from "./draft-store";

export function RefineDiff({ before, after, onAccept, onDiscard, title = "Review refinement", acceptLabel = "Accept refinement", discardLabel = "Discard refinement" }: { before: PlanProposal; after: PlanProposal; onAccept: () => void; onDiscard: () => void; title?: string; acceptLabel?: string; discardLabel?: string }) {
  const diff = diffPlans(before, after);
  return <section aria-label="Refinement diff" className="space-y-4 rounded-xl border bg-card p-5">
    <h2 className="text-lg font-semibold">{title}</h2>
    {!diff.added.length && !diff.removed.length && !diff.changed.length && <p>No task changes.</p>}
    {diff.added.map(task => <div key={task.key} className="rounded-lg border border-green-600 bg-green-500/10 p-3 text-green-700 dark:text-green-400"><p className="font-medium">Added · {task.key} · {task.title}</p><pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(task, null, 2)}</pre></div>)}
    {diff.removed.map(key => <div key={key} className="rounded-lg border border-red-600 bg-red-500/10 p-3 text-red-700 dark:text-red-400"><p className="font-medium">Removed · {key} · {before.tasks.find(t => t.key === key)?.title}</p><pre className="mt-2 overflow-auto whitespace-pre-wrap text-xs">{JSON.stringify(before.tasks.find(t => t.key === key), null, 2)}</pre></div>)}
    {diff.changed.map(task => <div key={task.key} className="space-y-2 rounded-lg border border-amber-600 bg-amber-500/10 p-3 text-amber-800 dark:text-amber-300"><p className="font-medium">Changed · {task.key}</p>{task.fields.map(field => <div key={field.field}><p className="text-xs font-semibold">{field.field}</p><div className="grid gap-2 text-xs sm:grid-cols-2"><pre className="overflow-auto whitespace-pre-wrap">Before: {JSON.stringify(field.before, null, 2)}</pre><pre className="overflow-auto whitespace-pre-wrap">After: {JSON.stringify(field.after, null, 2)}</pre></div></div>)}</div>)}
    <p className="text-sm text-muted-foreground">{after.rationale}</p>
    <div className="flex gap-2"><Button onClick={onAccept}>{acceptLabel}</Button><Button variant="outline" onClick={onDiscard}>{discardLabel}</Button></div>
  </section>;
}
