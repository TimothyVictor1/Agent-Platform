"use client";

import { useId, useMemo, useState } from "react";
import { graphlib, layout, type EdgeLabel, type GraphLabel, type NodeLabel } from "@dagrejs/dagre";
import type { Artifact, Task } from "@/contracts/generated";
import { StatusChip } from "@/components/shared/status-chip";
import { Button } from "@/components/ui/button";
import { ArtifactDownload } from "./common";

export function TasksPanel({ tasks, artifacts }: { tasks: Task[]; artifacts: Record<string, Artifact> }) {
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const markerId = useId();
  const graph = useMemo(() => {
    const graph = new graphlib.Graph<GraphLabel, NodeLabel, EdgeLabel>().setGraph({ rankdir: "TB", nodesep: 20, ranksep: 35, marginx: 12, marginy: 12 }).setDefaultEdgeLabel(() => ({}));
    for (const task of tasks) graph.setNode(task.key, { width: 160, height: 76 });
    for (const task of tasks) for (const parent of task.depends_on) if (graph.hasNode(parent)) graph.setEdge(parent, task.key);
    layout(graph);
    return graph;
  }, [tasks]);
  const selected = tasks.find(task => task.id === selectedId);
  if (!tasks.length) return <p className="text-sm text-muted-foreground">Waiting for tasks…</p>;
  return <div className="space-y-4">
    <p className="text-xs text-muted-foreground">Dependencies flow from top to bottom. Select a task for details.</p>
    <div className="overflow-auto rounded-lg border" aria-label="Task dependency graph" tabIndex={0}>
      <div className="relative mx-auto" style={{ width: graph.graph().width, height: graph.graph().height }}>
        <svg aria-hidden="true" className="absolute inset-0 h-full w-full"><defs><marker id={markerId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" /></marker></defs>
          {graph.edges().map(edge => <polyline key={`${edge.v}:${edge.w}`} points={graph.edge(edge).points?.map(point => `${point.x},${point.y}`).join(" ")} fill="none" stroke="currentColor" className="text-muted-foreground" markerEnd={`url(#${markerId})`} />)}
        </svg>
        {tasks.map(task => {
          const node = graph.node(task.key);
          return <Button key={task.id} variant="outline" aria-label={`Task ${task.key}: ${task.title}`} aria-pressed={task.id === selectedId} className="absolute h-[76px] w-40 flex-col items-start gap-1 overflow-hidden bg-card p-2 text-left" style={{ left: (node.x ?? 80) - 80, top: (node.y ?? 38) - 38 }} onClick={() => setSelectedId(task.id)}>
            <span className="w-full truncate text-xs">{task.key} · {task.title}</span><StatusChip status={task.status} />
          </Button>;
        })}
      </div>
    </div>
    {selected && <section aria-label="Task details" className="space-y-3 rounded-lg border p-3 text-sm">
      <h3 className="font-semibold">{selected.title}</h3><StatusChip status={selected.status} /><p className="whitespace-pre-wrap break-words">{selected.goal}</p>
      <h4 className="font-medium">Acceptance criteria</h4><ul className="list-inside list-disc">{selected.acceptance_criteria.map((criterion, index) => <li key={index} className="whitespace-pre-wrap break-words">{criterion}</li>)}</ul>
      <p>Depends on: {selected.depends_on.join(", ") || "None"}</p><p>Attempt {selected.attempt} / {selected.max_attempts}</p>
      {selected.result ? <><h4 className="font-medium">Result</h4><p className="whitespace-pre-wrap break-words">{selected.result.summary}</p>
        <h4 className="font-medium">Result outputs</h4><pre className="overflow-auto whitespace-pre-wrap break-words">{JSON.stringify(selected.result.outputs, null, 2)}</pre>
        <p>Confidence: {selected.result.confidence}</p>
        {!!selected.result.open_issues.length && <p className="whitespace-pre-wrap break-words">Open issues: {selected.result.open_issues.join("; ")}</p>}
        {selected.result.artifact_ids.map(id => <ArtifactDownload key={id} artifactId={id} artifact={artifacts[id]} />)}
      </> : <p>No result submitted yet.</p>}
    </section>}
  </div>;
}
