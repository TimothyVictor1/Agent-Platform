import type { Agent } from "@/contracts/generated";
export function AgentAvatar({ name, color }: Pick<Agent, "name" | "color">) {
  return <span className="inline-flex items-center gap-2 text-sm"><span className="grid size-7 place-items-center rounded-full border text-xs font-semibold" style={{ borderColor: color, color }} aria-hidden="true">{name.slice(0, 2).toUpperCase()}</span>{name}</span>;
}
