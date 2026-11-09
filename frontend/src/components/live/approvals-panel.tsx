"use client";

import { useState } from "react";
import type { Agent, Approval, ApprovalDecisionRequest } from "@/contracts/generated";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CommandFeedback, FrameImage, useCommand } from "./common";

export function ApprovalsPanel({ approvals, agents }: { approvals: Approval[]; agents: Agent[] }) {
  const [permission, setPermission] = useState<string | null>(null);
  return <div className="space-y-4">
    <Button variant="outline" onClick={async () => {
      if (!("Notification" in window)) { setPermission("Browser notifications are unavailable."); return; }
      try { setPermission(`Notifications: ${await Notification.requestPermission()}`); }
      catch { setPermission("Could not enable browser notifications."); }
    }}>Enable approval notifications</Button>
    {permission && <p role="status" className="text-sm">{permission}</p>}
    {!approvals.length && <p className="text-sm text-muted-foreground">No pending approvals.</p>}
    {approvals.map(approval => <ApprovalCard key={approval.id} approval={approval} agent={agents.find(agent => agent.id === approval.agent_id)} />)}
  </div>;
}

function ApprovalCard({ approval, agent }: { approval: Approval; agent?: Agent }) {
  const [note, setNote] = useState("");
  const decide = useCommand((body: ApprovalDecisionRequest) => api.decideApproval(approval.id, body));
  return <article aria-label={`Approval: ${approval.action_summary}`} className="space-y-3 rounded-lg border border-amber-500/50 p-3">
    <h3 className="whitespace-pre-wrap break-words font-semibold">{approval.action_summary}</h3>
    <p className="text-sm">{agent?.name || approval.agent_id} · Risk: {approval.risk}</p>
    {approval.frame_id ? <FrameImage frameId={approval.frame_id} alt={`Approval screenshot for ${approval.action_summary}`} /> : <p className="text-xs text-muted-foreground">No screenshot attached.</p>}
    {approval.expires_at && <p className="text-xs">Expires: <time dateTime={approval.expires_at}>{new Date(approval.expires_at).toLocaleString()}</time></p>}
    <label className="block">Decision note<textarea value={note} rows={2} maxLength={1500} onChange={event => setNote(event.target.value)} /></label>
    <div className="flex gap-2"><Button disabled={decide.isPending || decide.isSuccess} onClick={() => decide.mutate({ decision: "approve", note: note.trim() || null })}>Approve</Button>
      <Button variant="destructive" disabled={decide.isPending || decide.isSuccess} onClick={() => decide.mutate({ decision: "deny", note: note.trim() || null })}>Deny</Button></div>
    <CommandFeedback error={decide.error} success={decide.isSuccess} />
  </article>;
}
