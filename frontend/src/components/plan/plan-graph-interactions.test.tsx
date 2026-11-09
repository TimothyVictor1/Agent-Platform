import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactFlowProps } from "@xyflow/react";
import { describe, expect, it, vi } from "vitest";
import { planFixture } from "@/mocks/plan";
import { PlanGraph } from "./plan-graph";

vi.mock("@xyflow/react", async importOriginal => {
  const original = await importOriginal<typeof import("@xyflow/react")>();
  return { ...original, ReactFlow: ({ nodes = [], onNodesChange }: ReactFlowProps) => <div>
    {nodes.map(node => <output key={node.id} aria-label={`${node.id} position`}>{JSON.stringify(node.position)}</output>)}
    <button onClick={() => onNodesChange?.([{ id: "T1", type: "position", position: { x: 900, y: 900 } }])}>Move T1</button>
  </div> };
});

describe("graph position lifecycle", () => {
  it("drops removed tasks' stored positions and labels the graph region", () => {
    const props = { tasks: planFixture.tasks, errors: [], selection: null, statuses: {}, onSelect: vi.fn(), onConnect: vi.fn() };
    const view = render(<PlanGraph {...props} />);
    expect(screen.getByRole("region", { name: "Plan dependency graph" })).toBeVisible();
    const initial = screen.getByLabelText("T1 position").textContent;
    fireEvent.click(screen.getByRole("button", { name: "Move T1" }));
    expect(screen.getByLabelText("T1 position")).toHaveTextContent('{"x":900,"y":900}');
    view.rerender(<PlanGraph {...props} tasks={props.tasks.filter(task => task.key !== "T1")} />);
    view.rerender(<PlanGraph {...props} />);
    expect(screen.getByLabelText("T1 position").textContent).toBe(initial);
  });
});
