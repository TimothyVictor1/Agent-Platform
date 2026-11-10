import type { ApprovalDecisionRequest, ApprovalList, ApprovePlanRequest, Artifact, ArtifactList, BoardEntry, BoardPage, CommandAccepted, CreateSessionRequest, EventPage, FollowupRequest, FrameList, Problem, ProfileList, ProvidersHealth, PutPlanRequest, RefinePlanRequest, Session, SessionDetail, SessionPage, SteerRequest, UserBoardPostRequest, WsTicket } from "@/contracts/generated";
import { env } from "./env";

type Query = Record<string, string | number | undefined>;
export class ApiError extends Error {
  constructor(public readonly problem: Problem) { super(problem.detail || problem.title); this.name = "ApiError"; }
  get status() { return this.problem.status; }
}
export function apiUrl(path: string, query: Query = {}) {
  const url = new URL(`${env.apiUrl.replace(/\/$/, "")}/api/v1${path}`);
  for (const [key, value] of Object.entries(query)) if (value !== undefined) url.searchParams.set(key, String(value));
  return url.toString();
}
async function request<T>(method: string, path: string, body?: unknown, query?: Query, binary = false): Promise<T> {
  const headers = new Headers({ Accept: binary ? "*/*" : "application/json" });
  if (env.apiToken) headers.set("Authorization", `Bearer ${env.apiToken}`);
  if (body !== undefined && !(body instanceof FormData)) headers.set("Content-Type", "application/json");
  const url = apiUrl(path, query);
  const init: RequestInit = { method, headers, body: body === undefined ? undefined : body instanceof FormData ? body : JSON.stringify(body) };
  const response = env.mock ? await (await import("@/mocks/api")).mockRequest(url, init) : await fetch(url, init);
  if (!response.ok) {
    let data: Partial<Problem> = {};
    try { const value: unknown = await response.json(); if (value && typeof value === "object") data = value as Partial<Problem>; } catch { /* Non-JSON proxy errors also become ApiError. */ }
    throw new ApiError({ type: typeof data.type === "string" ? data.type : "about:blank", title: typeof data.title === "string" ? data.title : response.statusText || "Request failed", status: response.status, detail: typeof data.detail === "string" ? data.detail : null, instance: typeof data.instance === "string" ? data.instance : null, errors: Array.isArray(data.errors) ? data.errors : null });
  }
  if (response.status === 204) return undefined as T;
  if (binary) return await response.blob() as T;
  try { return await response.json() as T; }
  catch {
    throw new ApiError({ type: "about:blank", title: "Invalid response body", status: response.status, detail: null, instance: null, errors: null });
  }
}
const id = encodeURIComponent;
export const api = {
  createSession: (body: CreateSessionRequest) => request<Session>("POST", "/sessions", body),
  sessions: (query: { limit?: number; cursor?: string } = {}) => request<SessionPage>("GET", "/sessions", undefined, query),
  session: (sessionId: string) => request<SessionDetail>("GET", `/sessions/${id(sessionId)}`),
  wsTicket: (sessionId: string) => request<WsTicket>("POST", `/sessions/${id(sessionId)}/ws-ticket`),
  refine: (sessionId: string, body: RefinePlanRequest) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/refine`, body),
  updatePlan: (sessionId: string, body: PutPlanRequest) => request<CommandAccepted>("PUT", `/sessions/${id(sessionId)}/plan`, body),
  approve: (sessionId: string, body: ApprovePlanRequest = {}) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/approve`, body),
  followup: (sessionId: string, body: FollowupRequest) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/followup`, body),
  steer: (sessionId: string, body: SteerRequest) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/steer`, body),
  stop: (sessionId: string) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/stop`),
  pause: (sessionId: string) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/pause`),
  resume: (sessionId: string) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/resume`),
  takeover: (agentId: string) => request<CommandAccepted>("POST", `/agents/${id(agentId)}/takeover`),
  release: (agentId: string) => request<CommandAccepted>("POST", `/agents/${id(agentId)}/release`),
  board: (sessionId: string, query: { kinds?: string; tags?: string; q?: string; after_seq?: number; limit?: number } = {}) => request<BoardPage>("GET", `/sessions/${id(sessionId)}/board`, undefined, query),
  postBoard: (sessionId: string, body: UserBoardPostRequest) => request<CommandAccepted>("POST", `/sessions/${id(sessionId)}/board`, body),
  approvals: (sessionId: string, status?: ApprovalList["items"][number]["status"]) => request<ApprovalList>("GET", `/sessions/${id(sessionId)}/approvals`, undefined, { status }),
  decideApproval: (approvalId: string, body: ApprovalDecisionRequest) => request<CommandAccepted>("POST", `/approvals/${id(approvalId)}/decide`, body),
  artifacts: (sessionId: string) => request<ArtifactList>("GET", `/sessions/${id(sessionId)}/artifacts`),
  downloadArtifact: (artifactId: string) => request<Blob>("GET", `/artifacts/${id(artifactId)}/download`, undefined, undefined, true),
  uploadArtifact: (sessionId: string, file: File, description?: string) => {
    if (file.size > 50 * 1024 * 1024) return Promise.reject(new RangeError("Files must be at most 50 MB."));
    const body = new FormData(); body.set("file", file); if (description) body.set("description", description);
    return request<Artifact>("POST", `/sessions/${id(sessionId)}/artifacts`, body);
  },
  events: (sessionId: string, query: { after_seq?: number; limit?: number } = {}) => request<EventPage>("GET", `/sessions/${id(sessionId)}/events`, undefined, query),
  frames: (sessionId: string, query: { agent_id?: string; from_step?: number; to_step?: number } = {}) => request<FrameList>("GET", `/sessions/${id(sessionId)}/frames`, undefined, query),
  frame: (frameId: string) => request<Blob>("GET", `/frames/${id(frameId)}`, undefined, undefined, true),
  profiles: () => request<ProfileList>("GET", "/config/profiles"),
  providerHealth: () => request<ProvidersHealth>("GET", "/config/providers/health"),
};
// UI status/filter types are indexed from contracts rather than duplicated API models.
export type BoardKind = BoardEntry["kind"];
