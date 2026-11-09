import type { Agent, Approval, BoardEntry, Session, Task } from "@/contracts/generated";
type Status = Agent["status"] | Approval["status"] | NonNullable<BoardEntry["status"]> | Session["status"] | Task["status"];
const styles: Record<Status, string> = {
  planning: "text-sky-500", awaiting_approval: "text-amber-500", running: "text-sky-500", paused: "text-amber-500", completed: "text-emerald-500", failed: "text-red-500", stopped: "text-muted-foreground",
  booting: "text-sky-500", idle: "text-muted-foreground", working: "text-sky-500", waiting: "text-amber-500", paused_by_user: "text-amber-500", stuck: "text-red-500", error: "text-red-500",
  pending: "text-muted-foreground", ready: "text-sky-500", assigned: "text-sky-500", blocked: "text-amber-500", in_review: "text-violet-500", done: "text-emerald-500", cancelled: "text-muted-foreground",
  approved: "text-emerald-500", denied: "text-red-500", expired: "text-muted-foreground", open: "text-sky-500", resolved: "text-emerald-500", superseded: "text-muted-foreground",
};
export function StatusChip({ status }: { status: Status }) { return <span className={`inline-flex rounded-full border px-2 py-0.5 text-xs font-medium ${styles[status]}`}>{status.replaceAll("_", " ")}</span>; }
