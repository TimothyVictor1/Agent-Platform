import type { CreateSessionRequest, EventPage, Session, SessionDetail, SessionPage } from "@/contracts/generated";
import { applyEvent, initialSessionState } from "@/lib/session-store";
import { budgets, health, profiles, scriptedEvents, session } from "./fixtures";
import { thumbnailJpeg } from "./socket";

// Browser storage preserves mock-created sessions across route reloads without a global singleton.
function sessions(): Session[] {
  if (typeof localStorage === "undefined") return [session];
  try { const value: unknown = JSON.parse(localStorage.getItem("agentplatform.mock.sessions") || "null"); return Array.isArray(value) ? value as Session[] : [session]; } catch { return [session]; }
}
function save(items: Session[]) { if (typeof localStorage !== "undefined") localStorage.setItem("agentplatform.mock.sessions", JSON.stringify(items)); }
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
export async function mockRequest(rawUrl: string, init: RequestInit): Promise<Response> {
  const url = new URL(rawUrl);
  const segments = url.pathname.replace(/^\/api\/v1\//, "").split("/").map(decodeURIComponent);
  const [resource, resourceId, action] = segments;
  const body: unknown = typeof init.body === "string" ? JSON.parse(init.body) : undefined;
  if (resource === "config") return json(resourceId === "profiles" ? profiles : health);
  if (resource === "sessions" && !resourceId) {
    if (init.method === "POST") {
      const request = body as CreateSessionRequest;
      const created: Session = { ...session, ...request, budgets: request.budgets ?? budgets, id: crypto.randomUUID(), title: request.title || request.prompt.slice(0, 80), status: "planning", created_at: new Date().toISOString() };
      save([created, ...sessions()]); return json(created, 201);
    }
    const items = sessions(); const start = Number(url.searchParams.get("cursor") || 0); const limit = Number(url.searchParams.get("limit") || 20);
    return json({ items: items.slice(start, start + limit), next_cursor: start + limit < items.length ? String(start + limit) : null } satisfies SessionPage);
  }
  if (resource === "sessions") {
    const current = sessions().find(item => item.id === resourceId);
    if (!current) return json({ type: "about:blank", title: "Session not found", status: 404, detail: "Unknown mock session" }, 404);
    const events = scriptedEvents(resourceId);
    const state = events.slice(0, 3).reduce(applyEvent, initialSessionState(resourceId));
    if (!action) return json({ session: { ...current, status: "awaiting_approval", plan_version: 1 }, plan: state.plan, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 3 } satisfies SessionDetail);
    if (init.method === "GET") {
      if (action === "events") { const filtered = events.filter(event => event.seq > Number(url.searchParams.get("after_seq") || 0)); const limit = Number(url.searchParams.get("limit") || 200); return json({ items: filtered.slice(0, limit), next_after_seq: filtered.length > limit ? filtered[limit - 1].seq : null } satisfies EventPage); }
      if (action === "board") return json({ items: [], next_after_seq: null });
      if (["approvals", "artifacts", "frames"].includes(action)) return json({ items: [] });
    }
    if (action === "artifacts" && init.body instanceof FormData) {
      const file = init.body.get("file");
      if (file instanceof File) return json({ ...((await import("./fixtures")).artifact), id: crypto.randomUUID(), session_id: resourceId, name: file.name, mime: file.type || "application/octet-stream", size_bytes: file.size, description: init.body.get("description") }, 201);
    }
    return json({ command_id: crypto.randomUUID(), session_id: resourceId });
  }
  if (resource === "frames") return new Response(Uint8Array.from(atob(thumbnailJpeg), char => char.charCodeAt(0)), { headers: { "Content-Type": "image/jpeg" } });
  if (resource === "artifacts" && action === "download") return new Response("Launch report", { headers: { "Content-Type": "text/plain" } });
  if (["agents", "approvals"].includes(resource)) return json({ command_id: crypto.randomUUID(), session_id: session.id });
  return json({ type: "about:blank", title: "Not found", status: 404 }, 404);
}
