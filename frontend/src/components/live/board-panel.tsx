"use client";

import { useState } from "react";
import type { Agent, BoardEntry, UserBoardPostRequest } from "@/contracts/generated";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { CommandFeedback, useCommand } from "./common";

const kinds: BoardEntry["kind"][] = ["fact", "claim", "question", "answer", "decision", "blocker", "progress", "artifact", "note"];

export function BoardEntries({ entries, agents }: { entries: BoardEntry[]; agents: Agent[] }) {
  return entries.length ? <ul aria-label="Board entries" className="space-y-3">{entries.map(entry => <li key={entry.id} className="space-y-2 rounded-lg border-l-4 bg-muted/40 p-3" style={{ borderLeftColor: agents.find(agent => agent.id === entry.author.id)?.color || "var(--border)" }}>
    <p className="text-xs text-muted-foreground">{entry.author.name} · {entry.kind}{entry.status ? ` · ${entry.status}` : ""} · <time dateTime={entry.created_at}>{new Date(entry.created_at).toLocaleTimeString()}</time></p>
    <h4 className="whitespace-pre-wrap break-words font-medium">{entry.title}</h4><p className="whitespace-pre-wrap break-words text-sm">{entry.body}</p>
    {!!entry.tags.length && <p className="break-words text-xs text-muted-foreground">{entry.tags.map(tag => `#${tag}`).join(" ")}</p>}
    {entry.to && <p className="text-xs text-muted-foreground">To: {entry.to.map(id => agents.find(agent => agent.id === id)?.name || id).join(", ")}</p>}
  </li>)}</ul> : <p className="text-sm text-muted-foreground">No board entries match.</p>;
}

export function BoardPanel({ sessionId, entries, agents }: { sessionId: string; entries: BoardEntry[]; agents: Agent[] }) {
  const [kind, setKind] = useState("");
  const [agentId, setAgentId] = useState("");
  const [tag, setTag] = useState("");
  const [search, setSearch] = useState("");
  const tags = [...new Set(entries.flatMap(entry => entry.tags))].sort();
  const filtered = entries.filter(entry => (!kind || entry.kind === kind) && (!agentId || entry.author.id === agentId) && (!tag || entry.tags.some(value => value === tag)) && `${entry.title}\n${entry.body}\n${entry.tags.join(" ")}`.toLowerCase().includes(search.toLowerCase())).toReversed();
  return <div className="space-y-4">
    <div className="grid grid-cols-2 gap-2">
      <label>Board kind<select value={kind} onChange={event => setKind(event.target.value)}><option value="">All kinds</option>{kinds.map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Board agent<select value={agentId} onChange={event => setAgentId(event.target.value)}><option value="">All authors</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
      <label>Board tag<select value={tag} onChange={event => setTag(event.target.value)}><option value="">All tags</option>{tags.map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Search board<input type="search" value={search} onChange={event => setSearch(event.target.value)} /></label>
    </div>
    <BoardEntries entries={filtered} agents={agents} />
    <BoardComposer sessionId={sessionId} agents={agents} />
  </div>;
}

function BoardComposer({ sessionId, agents }: { sessionId: string; agents: Agent[] }) {
  const [kind, setKind] = useState<"note" | "decision">("note");
  const [recipient, setRecipient] = useState("");
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const post = useCommand((request: UserBoardPostRequest) => api.postBoard(sessionId, request));
  return <form aria-label="Compose board entry" className="space-y-3 rounded-lg border p-3" onSubmit={event => {
    event.preventDefault();
    if (!title.trim() || post.isPending) return;
    post.mutate({ kind, title: title.trim(), body: body.trim(), to: recipient ? [recipient] : null }, { onSuccess: () => { setTitle(""); setBody(""); } });
  }}>
    <h3 className="font-medium">Post as user</h3>
    <label className="block">Post kind<select value={kind} onChange={event => setKind(event.target.value === "decision" ? "decision" : "note")}><option value="note">Note</option><option value="decision">Decision</option></select></label>
    <label className="block">Post recipient<select value={recipient} onChange={event => setRecipient(event.target.value)}><option value="">Everyone</option>{agents.map(agent => <option key={agent.id} value={agent.id}>{agent.name}</option>)}</select></label>
    <label className="block">Post title<input required maxLength={120} value={title} onChange={event => setTitle(event.target.value)} /></label>
    <label className="block">Post text<textarea rows={3} maxLength={1500} value={body} onChange={event => setBody(event.target.value)} /></label>
    <Button type="submit" disabled={!title.trim() || post.isPending}>Post to board</Button><CommandFeedback error={post.error} success={post.isSuccess} />
  </form>;
}
