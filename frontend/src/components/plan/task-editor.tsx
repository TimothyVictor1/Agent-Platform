"use client";

import { useState } from "react";
import { useStore } from "zustand";
import type { TaskDraft } from "@/contracts/generated";
import { Button } from "@/components/ui/button";
import type { createDraftStore } from "./draft-store";

type Store = ReturnType<typeof createDraftStore>;
const tools: TaskDraft["suggested_tools"] = ["browser", "terminal", "office", "files"];
const isOutputKind = (value: string): value is TaskDraft["outputs"][number]["kind"] => value === "text" || value === "json" || value === "artifact";

export function TaskEditor({ store, taskKey, errors, onDelete }: { store: Store; taskKey: string; errors: string[]; onDelete: () => void }) {
  const state = useStore(store);
  const task = state.proposal?.tasks.find(t => t.key === taskKey);
  const [tag, setTag] = useState("");
  if (!task || !state.proposal) return null;
  const others = state.proposal.tasks.filter(t => t.key !== task.key);
  const update = (patch: Partial<TaskDraft>) => state.update(task.key, patch);
  const output = (index: number, patch: Partial<TaskDraft["outputs"][number]>) => update({ outputs: task.outputs.map((o, i) => i === index ? { ...o, ...patch } : o) });
  const addTag = () => { const value = tag.trim(); if (value && !task.tags.includes(value)) update({ tags: [...task.tags, value] }); setTag(""); };
  return <aside aria-label={`Edit task ${task.key}`} className="max-h-[760px] space-y-5 overflow-y-auto rounded-xl border bg-card p-5">
    <div className="flex items-center justify-between gap-3"><h2 className="text-lg font-semibold">{task.key} · Task editor</h2><Button variant="destructive" onClick={onDelete}>Delete task</Button></div>
    {errors.length > 0 && <ul aria-label="Task errors" className="space-y-1 text-sm text-destructive">{errors.map((message, i) => <li key={i}>{message}</li>)}</ul>}
    <label className="block space-y-1"><span>Title</span><input value={task.title} maxLength={80} onChange={e => update({ title: e.target.value })} /></label>
    <label className="block space-y-1"><span>Goal</span><textarea value={task.goal} rows={3} maxLength={1200} onChange={e => update({ goal: e.target.value })} /></label>
    <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">Acceptance criteria · {task.acceptance_criteria.length}/6</legend>
      {task.acceptance_criteria.map((criterion, index) => <div key={index} className="space-y-1 rounded-lg border p-2">
        <textarea aria-label={`Criterion ${index + 1}`} rows={2} value={criterion} onChange={e => update({ acceptance_criteria: task.acceptance_criteria.map((c, i) => i === index ? e.target.value : c) })} />
        <div className="flex gap-1">
          <Button size="xs" variant="ghost" aria-label={`Move criterion ${index + 1} up`} disabled={index === 0} onClick={() => { const next = [...task.acceptance_criteria]; [next[index - 1], next[index]] = [next[index], next[index - 1]]; update({ acceptance_criteria: next }); }}>↑ Up</Button>
          <Button size="xs" variant="ghost" aria-label={`Move criterion ${index + 1} down`} disabled={index === task.acceptance_criteria.length - 1} onClick={() => { const next = [...task.acceptance_criteria]; [next[index], next[index + 1]] = [next[index + 1], next[index]]; update({ acceptance_criteria: next }); }}>↓ Down</Button>
          <Button size="xs" variant="ghost" aria-label={`Remove criterion ${index + 1}`} onClick={() => update({ acceptance_criteria: task.acceptance_criteria.filter((_, i) => i !== index) })}>Remove</Button>
        </div>
      </div>)}
      <Button variant="outline" disabled={task.acceptance_criteria.length >= 6} onClick={() => update({ acceptance_criteria: [...task.acceptance_criteria, ""] })}>Add criterion</Button>
    </fieldset>
    <fieldset className="space-y-3"><legend className="mb-2 text-sm font-medium">Outputs</legend>
      {task.outputs.map((item, index) => {
        const schemaId = `${task.key}:${index}`;
        return <div key={index} className="space-y-2 rounded-lg border p-3">
          <label className="block">Output {index + 1} key<input value={item.key} onChange={e => output(index, { key: e.target.value })} /></label>
          <label className="block">Output {index + 1} kind<select value={item.kind} onChange={e => { const kind = e.target.value; if (!isOutputKind(kind)) return; state.setSchema(task.key, index, ""); output(index, { kind, json_schema: null }); }}><option value="text">Text</option><option value="json">JSON</option><option value="artifact">Artifact</option></select></label>
          <label className="block">Output {index + 1} description<textarea value={item.description} rows={2} maxLength={500} onChange={e => output(index, { description: e.target.value })} /></label>
          {item.kind === "json" && <label className="block">Output {index + 1} JSON schema (optional)<textarea className="font-mono text-xs" rows={5} aria-invalid={Boolean(state.schemaErrors[schemaId])} aria-describedby={state.schemaErrors[schemaId] ? `schema-error-${task.key}-${index}` : undefined} value={state.schemaText[schemaId] ?? (item.json_schema ? JSON.stringify(item.json_schema, null, 2) : "")} onChange={e => state.setSchema(task.key, index, e.target.value)} /></label>}
          {state.schemaErrors[schemaId] && <p id={`schema-error-${task.key}-${index}`} role="alert" className="text-sm text-destructive">Invalid JSON: {state.schemaErrors[schemaId]}</p>}
          <Button size="sm" variant="ghost" aria-label={`Remove output ${index + 1}`} onClick={() => state.removeOutput(task.key, index)}>Remove output</Button>
        </div>;
      })}
      <Button variant="outline" onClick={() => update({ outputs: [...task.outputs, { key: "", kind: "text", description: "", json_schema: null }] })}>Add output</Button>
    </fieldset>
    <fieldset className="space-y-2"><legend className="mb-2 text-sm font-medium">Tags</legend>
      <ul className="flex flex-wrap gap-2" aria-label="Task tags">{task.tags.map((value, i) => <li key={`${value}-${i}`} className="flex items-center gap-1 rounded-full border bg-muted px-2 py-1 text-xs">{value}<Button size="icon-xs" variant="ghost" aria-label={`Remove tag ${value}`} onClick={() => update({ tags: task.tags.filter((_, index) => index !== i) })}>×</Button></li>)}</ul>
      <div className="flex gap-2"><input aria-label="New tag" value={tag} onChange={e => setTag(e.target.value)} onKeyDown={e => { if (e.key === "Enter") { e.preventDefault(); addTag(); } }} /><Button variant="outline" onClick={addTag} disabled={!tag.trim()}>Add tag</Button></div>
    </fieldset>
    <fieldset><legend className="mb-2 text-sm font-medium">Suggested tools</legend><div className="flex flex-wrap gap-3">{tools.map(tool => <label key={tool} className="flex items-center gap-2"><input className="w-4" type="checkbox" checked={task.suggested_tools.includes(tool)} onChange={e => update({ suggested_tools: e.target.checked ? [...task.suggested_tools, tool] : task.suggested_tools.filter(t => t !== tool) })} />{tool}</label>)}</div></fieldset>
    <label className="block space-y-1"><span>Dependencies (select multiple)</span><select aria-label="Dependencies (select multiple)" multiple size={Math.max(2, Math.min(5, others.length))} value={task.depends_on} onChange={e => {
      const depends_on = Array.from(e.target.selectedOptions, option => option.value);
      update({ depends_on, inputs: task.inputs.filter(input => depends_on.includes(input.from_task)) });
    }}>{others.map(t => <option key={t.key} value={t.key}>{t.key} · {t.title}</option>)}{task.depends_on.filter(key => !others.some(t => t.key === key)).map(key => <option key={key} value={key}>{key} (unknown)</option>)}</select><span className="text-xs text-muted-foreground">Use Cmd/Ctrl to select several. Drag between graph handles to connect tasks.</span></label>
    <fieldset className="space-y-3"><legend className="mb-2 text-sm font-medium">Inputs wiring</legend>
      {task.inputs.map((input, index) => {
        const upstream = state.proposal?.tasks.find(t => t.key === input.from_task);
        const setInput = (patch: Partial<typeof input>) => update({ inputs: task.inputs.map((item, i) => i === index ? { ...item, ...patch } : item) });
        return <div key={index} className="space-y-2 rounded-lg border p-3">
          <label className="block">Input {index + 1} upstream task<select value={input.from_task} onChange={e => {
            const from_task = e.target.value; const source = others.find(t => t.key === from_task);
            update({ depends_on: from_task && !task.depends_on.includes(from_task) ? [...task.depends_on, from_task] : task.depends_on, inputs: task.inputs.map((item, i) => i === index ? { from_task, output_key: source?.outputs[0]?.key ?? "" } : item) });
          }}><option value="">Choose upstream task</option>{others.map(t => <option key={t.key} value={t.key}>{t.key} · {t.title}</option>)}{input.from_task && !others.some(t => t.key === input.from_task) && <option value={input.from_task}>{input.from_task} (unknown)</option>}</select></label>
          <label className="block">Input {index + 1} output key<select value={input.output_key} onChange={e => setInput({ output_key: e.target.value })}><option value="">Choose output</option>{upstream?.outputs.map((o, i) => <option key={`${o.key}-${i}`} value={o.key}>{o.key} · {o.kind}</option>)}{input.output_key && !upstream?.outputs.some(o => o.key === input.output_key) && <option value={input.output_key}>{input.output_key} (unknown)</option>}</select></label>
          <Button size="sm" variant="ghost" aria-label={`Remove input ${index + 1}`} onClick={() => update({ inputs: task.inputs.filter((_, i) => i !== index) })}>Remove input</Button>
        </div>;
      })}
      <Button variant="outline" disabled={!others.length} onClick={() => update({ inputs: [...task.inputs, { from_task: "", output_key: "" }] })}>Add input</Button>
    </fieldset>
  </aside>;
}
