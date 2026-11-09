import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/lib/api";
import { artifact } from "@/mocks/fixtures";
import { artifactsZip } from "./exports";

afterEach(() => vi.restoreAllMocks());
function bytes(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader(); reader.onload = () => resolve(reader.result as ArrayBuffer); reader.onerror = reject; reader.readAsArrayBuffer(blob);
  });
}

describe("artifact ZIP", () => {
  it("uses existing list/download APIs and writes a safe UTF-8 stored entry with CRC and central directory", async () => {
    vi.spyOn(api, "artifacts").mockResolvedValue({ items: [{ ...artifact, name: "../résumé.txt" }] });
    const file = new Blob(["123456789"], { type: "text/plain" });
    Object.defineProperty(file, "arrayBuffer", { value: () => bytes(file) });
    const fetch = vi.spyOn(api, "downloadArtifact").mockResolvedValue(file);
    const zip = await artifactsZip("session");
    const buffer = await bytes(zip), view = new DataView(buffer);
    expect(fetch).toHaveBeenCalledWith(artifact.id);
    expect(view.getUint32(0, true)).toBe(0x04034b50);
    expect(view.getUint16(6, true)).toBe(0x800);
    expect(view.getUint32(14, true)).toBe(0xcbf43926); // Standard CRC32 test vector.
    expect(view.getUint32(18, true)).toBe(9);
    const nameLength = view.getUint16(26, true);
    const name = new TextDecoder().decode(buffer.slice(30, 30 + nameLength));
    expect(name).toBe("1-.._résumé.txt"); expect(name).not.toContain("/");
    expect(new TextDecoder().decode(buffer.slice(30 + nameLength, 39 + nameLength))).toBe("123456789");
    expect(view.getUint32(39 + nameLength, true)).toBe(0x02014b50);
    const end = buffer.byteLength - 22;
    expect(view.getUint32(end, true)).toBe(0x06054b50);
    expect(view.getUint16(end + 10, true)).toBe(1);
    expect(view.getUint32(end + 16, true)).toBe(39 + nameLength);
  });
  it("does not silently export an incomplete archive when an artifact fails", async () => {
    vi.spyOn(api, "artifacts").mockResolvedValue({ items: [artifact] });
    vi.spyOn(api, "downloadArtifact").mockRejectedValue(new Error("Download failed"));
    await expect(artifactsZip("session")).rejects.toThrow("Download failed");
  });
});
