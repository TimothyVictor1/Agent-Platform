import { api } from "@/lib/api";

function crc32(bytes: Uint8Array): number {
  let crc = 0xffffffff;
  for (const byte of bytes) { crc ^= byte; for (let bit = 0; bit < 8; bit++) crc = (crc >>> 1) ^ ((crc & 1) ? 0xedb88320 : 0); }
  return (crc ^ 0xffffffff) >>> 0;
}
function header(length: number) { const bytes = new Uint8Array(length); return { bytes, view: new DataView(bytes.buffer) }; }

/** Stored ZIP entries need no compressor dependency. Names cannot escape the archive folder. */
export async function artifactsZip(sessionId: string): Promise<Blob> {
  const { items } = await api.artifacts(sessionId);
  if (items.length > 65535) throw new Error("Too many artifacts for a ZIP download.");
  const local: BlobPart[] = [], central: BlobPart[] = [];
  let offset = 0, centralSize = 0;
  for (const [index, artifact] of items.entries()) {
    const blob = await api.downloadArtifact(artifact.id);
    const safeName = Array.from(artifact.name, char => char === "/" || char === "\\" || char.charCodeAt(0) < 32 || char.charCodeAt(0) === 127 ? "_" : char).join("");
    const name = new TextEncoder().encode(`${index + 1}-${safeName}`);
    if (blob.size > 0xffffffff || offset + blob.size + name.length + 30 > 0xffffffff || name.length > 65535) throw new Error("Artifacts exceed the ZIP download size limit.");
    const checksum = crc32(new Uint8Array(await blob.arrayBuffer()));
    const entry = header(30);
    entry.view.setUint32(0, 0x04034b50, true); entry.view.setUint16(4, 20, true); entry.view.setUint16(6, 0x800, true);
    entry.view.setUint16(12, 33, true); // 1980-01-01, minimum ZIP DOS date.
    entry.view.setUint32(14, checksum, true); entry.view.setUint32(18, blob.size, true); entry.view.setUint32(22, blob.size, true); entry.view.setUint16(26, name.length, true);
    local.push(entry.bytes.buffer, name.buffer, blob);
    const directory = header(46);
    directory.view.setUint32(0, 0x02014b50, true); directory.view.setUint16(4, 20, true); directory.view.setUint16(6, 20, true); directory.view.setUint16(8, 0x800, true);
    directory.view.setUint16(14, 33, true); directory.view.setUint32(16, checksum, true); directory.view.setUint32(20, blob.size, true); directory.view.setUint32(24, blob.size, true); directory.view.setUint16(28, name.length, true); directory.view.setUint32(42, offset, true);
    central.push(directory.bytes.buffer, name.buffer);
    offset += 30 + name.length + blob.size; centralSize += 46 + name.length;
  }
  if (offset + centralSize > 0xffffffff) throw new Error("Artifacts exceed the ZIP download size limit.");
  const end = header(22);
  end.view.setUint32(0, 0x06054b50, true); end.view.setUint16(8, items.length, true); end.view.setUint16(10, items.length, true); end.view.setUint32(12, centralSize, true); end.view.setUint32(16, offset, true);
  return new Blob([...local, ...central, end.bytes.buffer], { type: "application/zip" });
}

export function download(blob: Blob, name: string) {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a"); anchor.href = url; anchor.download = name; document.body.append(anchor); anchor.click(); anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
