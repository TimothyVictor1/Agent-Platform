"use client";

import { useMemo, useState, useSyncExternalStore } from "react";
import dagre from "@dagrejs/dagre";
import { Background, Controls, Handle, MarkerType, Position, ReactFlow, type Edge, type Node, type NodeProps, type XYPosition } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { PlanError, ProblemError, Task, TaskDraft } from "@/contracts/generated";
import { StatusChip } from "@/components/shared/status-chip";
import { cycleEdges, nodeErrors } from "./validation";

type TaskNode = Node<{ task: TaskDraft; status: Task["status"]; errors: string[]; cycle: boolean }, "task">;
export type Selection = { type: "node"; key: string } | { type: "edge"; source: string; target: string } | null;

function TaskCard({ data, selected }: NodeProps<TaskNode>) {
  return <div className={`w-[260px] rounded-xl border-2 bg-card p-4 text-card-foreground shadow-sm ${data.errors.length ? "border-destructive" : selected ? "border-sky-500" : "border-border"}`}>
    <Handle type="target" position={Position.Left} aria-label={`Dependency target for ${data.task.key}`} className="!h-3 !w-3" />
    <div className="mb-2 flex items-center justify-between gap-2"><span className="font-mono text-xs font-semibold text-muted-foreground">{data.task.key}</span><StatusChip status={data.status} /></div>
    <p className="font-semibold">{data.task.title || "Untitled task"}</p>
    <p className="mt-2 text-xs text-muted-foreground">{data.task.acceptance_criteria.length} criteria · {data.task.outputs.length} outputs</p>
    <p className="mt-1 truncate text-xs text-muted-foreground">{data.task.outputs.map(o => `${o.key || "(unnamed)"} (${o.kind})`).join(", ") || "No outputs"}</p>
    {data.cycle && <p className="mt-2 text-xs font-semibold text-destructive">Cycle path</p>}
    {data.errors.length > 0 && <ul className="mt-2 space-y-1 text-xs text-destructive">{data.errors.map((error, i) => <li key={i}>{error}</li>)}</ul>}
    <Handle type="source" position={Position.Right} aria-label={`Output source for ${data.task.key}`} className="!h-3 !w-3" />
  </div>;
}
const nodeTypes = { task: TaskCard };

function subscribeTheme(onChange: () => void) {
  const observer = new MutationObserver(onChange);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
  return () => observer.disconnect();
}
const currentTheme = () => document.documentElement.classList.contains("dark") ? "dark" : "light";
const serverTheme = () => "dark" as const;

export function graphElements(tasks: TaskDraft[], errors: (PlanError | ProblemError)[], selection: Selection, statuses: Record<string, Task["status"]> = {}) {
  const graph = new dagre.graphlib.Graph().setGraph({ rankdir: "LR", ranksep: 90, nodesep: 40 }).setDefaultEdgeLabel(() => ({}));
  tasks.forEach(task => graph.setNode(task.key, { width: 260, height: 210 + nodeErrors(task.key, errors).length * 35 }));
  tasks.forEach(task => task.depends_on.forEach(dep => { if (tasks.some(t => t.key === dep)) graph.setEdge(dep, task.key); }));
  dagre.layout(graph);
  const cycles = cycleEdges(errors.filter((error): error is PlanError => "cycle" in error));
  const nodes: TaskNode[] = tasks.map(task => {
    const point = graph.node(task.key);
    const taskErrors = nodeErrors(task.key, errors);
    return { id: task.key, type: "task", position: { x: point.x - 130, y: point.y - point.height / 2 }, selected: selection?.type === "node" && selection.key === task.key, ariaLabel: `Task ${task.key}: ${task.title}`, data: { task, status: statuses[task.key] ?? "pending", errors: taskErrors, cycle: errors.some(e => "cycle" in e && e.cycle?.includes(task.key)) } };
  });
  const edges: Edge[] = tasks.flatMap(task => task.depends_on.filter(dep => tasks.some(t => t.key === dep)).map(dep => {
    const cycle = cycles.has(`${dep}->${task.key}`);
    return { id: `${dep}->${task.key}`, source: dep, target: task.key, selected: selection?.type === "edge" && selection.source === dep && selection.target === task.key, ariaLabel: `Dependency ${dep} to ${task.key}${cycle ? ", cycle path" : ""}`, label: cycle ? "Cycle" : undefined, style: { stroke: cycle ? "var(--destructive)" : "#64748b", strokeWidth: cycle ? 3 : 2, strokeDasharray: cycle ? "6 3" : undefined }, markerEnd: { type: MarkerType.ArrowClosed, color: cycle ? "var(--destructive)" : "#64748b" } };
  }));
  return { nodes, edges };
}

export function PlanGraph({ tasks, errors, selection, statuses, onSelect, onConnect }: {
  tasks: TaskDraft[]; errors: (PlanError | ProblemError)[]; selection: Selection; statuses: Record<string, Task["status"]>;
  onSelect: (selection: Selection) => void; onConnect: (source: string, target: string) => void;
}) {
  const { nodes, edges } = useMemo(() => graphElements(tasks, errors, selection, statuses), [tasks, errors, selection, statuses]);
  const [positions, setPositions] = useState<Record<string, XYPosition>>({});
  const theme = useSyncExternalStore(subscribeTheme, currentTheme, serverTheme);
  return <div aria-label="Plan dependency graph" className="h-[620px] min-w-0 overflow-hidden rounded-xl border bg-muted/20">
    <ReactFlow nodes={nodes.map(node => ({ ...node, position: positions[node.id] ?? node.position }))} edges={edges} nodeTypes={nodeTypes} fitView colorMode={theme} minZoom={0.2} maxZoom={1.6} deleteKeyCode={null} multiSelectionKeyCode={null}
      onNodeClick={(_, node) => onSelect({ type: "node", key: node.id })} onEdgeClick={(_, edge) => onSelect({ type: "edge", source: edge.source, target: edge.target })} onPaneClick={() => onSelect(null)}
      onNodesChange={changes => { for (const change of changes) {
        if (change.type === "select" && change.selected) onSelect({ type: "node", key: change.id });
        if (change.type === "position" && change.position) { const position = change.position; setPositions(previous => ({ ...previous, [change.id]: position })); }
      } }}
      onEdgesChange={changes => { for (const change of changes) if (change.type === "select" && change.selected) { const edge = edges.find(e => e.id === change.id); if (edge) onSelect({ type: "edge", source: edge.source, target: edge.target }); } }}
      onConnect={({ source, target }) => onConnect(source, target)}>
      <Background /><Controls showInteractive={false} />
    </ReactFlow>
  </div>;
}
