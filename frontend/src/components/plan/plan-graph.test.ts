import { describe, expect, it } from "vitest";
import { planFixture } from "@/mocks/plan";
import { graphElements } from "./plan-graph";
import { validatePlan } from "./validation";

describe("DAG rendering", () => {
  it("lays out dependencies left to right and derives node status and selected edges", () => {
    const result = graphElements(planFixture.tasks, [], { type: "edge", source: "T1", target: "T3" }, { T1: "done" });
    expect(result.nodes[0].position.x).toBeLessThan(result.nodes[2].position.x);
    expect(result.nodes[1].position.x).toBeLessThan(result.nodes[2].position.x);
    expect(result.nodes[0].data.status).toBe("done"); expect(result.edges.find(e => e.id === "T1->T3")?.selected).toBe(true);
  });
  it("labels and highlights exactly the cycle path", () => {
    const tasks = structuredClone(planFixture.tasks); tasks[0].depends_on = ["T3"];
    const result = graphElements(tasks, validatePlan(tasks), null);
    expect(result.edges.filter(e => e.label === "Cycle").map(e => e.id).sort()).toEqual(["T1->T3", "T3->T1"]);
    expect(result.nodes.filter(n => n.data.cycle).map(n => n.id)).toEqual(["T1", "T3"]);
    expect(result.nodes[1].data.errors).toEqual([]);
  });
});
