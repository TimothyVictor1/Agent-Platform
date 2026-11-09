import { afterEach, describe, expect, it, vi } from "vitest";
import { api, ApiError } from "./api";
import { env } from "./env";

vi.mock("./env", () => ({ env: { apiUrl: "http://api.example", apiToken: "test-token", mock: false } }));
afterEach(() => vi.unstubAllGlobals());
describe("REST client", () => {
  it("sends typed JSON and bearer auth", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ id: "s1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetcher);
    await api.createSession({ prompt: "Research", model_profile: "local" });
    const [url, init] = fetcher.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://api.example/api/v1/sessions");
    expect(new Headers(init.headers).get("Authorization")).toBe("Bearer test-token");
    expect(JSON.parse(String(init.body))).toEqual({ prompt: "Research", model_profile: "local" });
  });
  it("maps problem+json and preserves structured validation errors", async () => {
    const problem = { type: "validation", title: "Invalid plan", status: 422, detail: "Cycle found", errors: [{ message: "Cycle", code: "cycle", loc: null, task_key: "a", details: null }], instance: null };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(JSON.stringify(problem), { status: 422 })));
    await expect(api.updatePlan("s", { tasks: [] })).rejects.toMatchObject({ name: "ApiError", status: 422, message: "Cycle found", problem });
  });
  it("maps non-JSON proxy errors and network errors predictably", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response("Gateway down", { status: 502, statusText: "Bad Gateway" })));
    await expect(api.profiles()).rejects.toBeInstanceOf(ApiError);
    await expect(api.profiles()).rejects.toMatchObject({ status: 502, message: "Bad Gateway" });
    vi.stubGlobal("fetch", vi.fn().mockRejectedValue(new TypeError("Network unavailable")));
    await expect(api.profiles()).rejects.toThrow("Network unavailable");
  });
  it("encodes path IDs and all filters and omits unset values", async () => {
    const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ items: [] })));
    vi.stubGlobal("fetch", fetcher);
    await api.board("a/b", { kinds: "note,decision", q: "one & two", after_seq: 0 });
    const url = new URL(fetcher.mock.calls[0][0] as string);
    expect(url.pathname).toBe("/api/v1/sessions/a%2Fb/board");
    expect(url.searchParams.get("q")).toBe("one & two");
    expect(url.searchParams.get("after_seq")).toBe("0");
    expect(url.searchParams.has("tags")).toBe(false);
  });
  it("supports multipart uploads, size limits and authenticated binary downloads", async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response(JSON.stringify({ id: "artifact" }))));
    vi.stubGlobal("fetch", fetcher);
    await api.uploadArtifact("s", new File(["report"], "report.txt"), "Report");
    const init = fetcher.mock.calls[0][1] as RequestInit;
    expect(init.body).toBeInstanceOf(FormData);
    expect(new Headers(init.headers).has("Content-Type")).toBe(false);
    const large = new File([], "large"); Object.defineProperty(large, "size", { value: 50 * 1024 * 1024 + 1 });
    await expect(api.uploadArtifact("s", large)).rejects.toThrow("50 MB");
    const blob = await api.downloadArtifact("artifact");
    expect(blob.size).toBeGreaterThan(0);
  });
  it("implements every endpoint with its intended verb", async () => {
    const fetcher = vi.fn().mockImplementation(() => Promise.resolve(new Response("{}")));
    vi.stubGlobal("fetch", fetcher);
    await Promise.all([api.sessions(), api.session("s"), api.refine("s", { instruction: "More" }), api.approve("s"), api.followup("s", { prompt: "Next" }), api.steer("s", { text: "Focus" }), api.stop("s"), api.pause("s"), api.resume("s"), api.takeover("a"), api.release("a"), api.postBoard("s", { kind: "note", title: "Note", body: "Text" }), api.approvals("s", "pending"), api.decideApproval("p", { decision: "approve" }), api.artifacts("s"), api.events("s", { after_seq: 5, limit: 10 }), api.frames("s", { agent_id: "a", from_step: 0, to_step: 10 }), api.frame("f"), api.profiles(), api.providerHealth()]);
    const calls = fetcher.mock.calls.map(([url, init]) => `${(init as RequestInit).method} ${new URL(url as string).pathname}`);
    expect(calls).toContain("GET /api/v1/config/providers/health");
    expect(calls).toContain("POST /api/v1/approvals/p/decide");
    expect(calls).toContain("POST /api/v1/sessions/s/pause");
    expect(calls).toHaveLength(20);
  });
  it("does not send an Authorization header when no token is configured", async () => {
    const original = env.apiToken;
    Object.assign(env, { apiToken: undefined });
    const fetcher = vi.fn().mockResolvedValue(new Response("{}")); vi.stubGlobal("fetch", fetcher);
    try { await api.profiles(); expect(new Headers((fetcher.mock.calls[0][1] as RequestInit).headers).has("Authorization")).toBe(false); }
    finally { Object.assign(env, { apiToken: original }); }
  });
});
