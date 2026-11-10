import type { ApprovePlanRequest, CreateSessionRequest, EventPage, PutPlanRequest, RefinePlanRequest, Session, SessionDetail, SessionPage, WsTicket } from "@/contracts/generated";
import { fieldErrors, validatePlan } from "@/components/plan/validation";
import { applyEvent, initialSessionState } from "@/lib/session-store";
import { budgets, health, profiles, scriptedEvents, session } from "./fixtures";
import { thumbnailJpeg } from "./socket";
import { isReviewSession, reviewCommand, reviewDetail, reviewEvents } from "./plan";

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
    if (action === "ws-ticket" && init.method === "POST") {
      const ticket = `mock_ws_ticket_${crypto.randomUUID()}`; // gitleaks:allow
      return json({ ticket, expires_at: new Date(Date.now() + 60000).toISOString(), ttl_s: 60 } satisfies WsTicket, 201);
    }
    const review = isReviewSession(resourceId);
    const events = (review ? reviewEvents(resourceId) : null) ?? scriptedEvents(resourceId);
    if (!action) {
      if (review) return json(reviewDetail(current));
      const state = events.slice(0, 3).reduce(applyEvent, initialSessionState(resourceId));
      return json({ session: { ...current, status: "awaiting_approval", plan_version: 1 }, plan: state.plan, tasks: [], agents: [], counts: { total: 0, by_status: {} }, last_seq: 3 } satisfies SessionDetail);
    }
    if (review && action === "plan" && init.method === "PUT") {
      const request = body as PutPlanRequest;
      const detail = reviewDetail(current);
      if (request.base_version != null && request.base_version !== detail.plan?.version) return json({ type: "about:blank", title: "Plan version changed", status: 409, detail: "Reload the latest plan before saving." }, 409);
      const errors = [...validatePlan(request.tasks), ...fieldErrors(request.tasks)];
      if (errors.length) return json({ type: "about:blank", title: "Invalid plan", status: 422, errors: errors.map(error => ({ message: error.message, task_key: error.task_key, code: error.code, loc: null, details: null })) }, 422);
      reviewCommand(current, "plan", request.tasks);
    }
    if (review && action === "refine" && init.method === "POST") reviewCommand(current, "refine", undefined, (body as RefinePlanRequest).instruction);
    if (review && action === "approve" && init.method === "POST") {
      const detail = reviewDetail(current);
      const request = body as ApprovePlanRequest;
      if (request.plan_version != null && request.plan_version !== detail.plan?.version) return json({ type: "about:blank", title: "Plan version changed", status: 409 }, 409);
      reviewCommand(current, "approve");
    }
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
