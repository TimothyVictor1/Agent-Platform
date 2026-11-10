"use client";

import { useEffect, useRef, useState } from "react";
import type { Agent, WsThumbnailMessage } from "@/contracts/generated";
import { env } from "@/lib/env";
import { Button } from "@/components/ui/button";

function streamUrl(raw: string | null, interactive: boolean): string | null {
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== "https:" && url.protocol !== "http:") return null;
    url.searchParams.set("view_only", interactive ? "0" : "1");
    url.searchParams.set("show_clipboard", "0");
    url.searchParams.set("autoconnect", "1");
    return url.toString();
  } catch { return null; }
}

export function AgentStream({ agent, thumbnail, interactive = false }: { agent: Agent; thumbnail?: WsThumbnailMessage; interactive?: boolean }) {
  const url = env.mock ? null : streamUrl(interactive ? agent.interactive_stream_url : agent.stream_url, interactive);
  // Key the player so a stream URL or control-mode change resets fallback state.
  return <StreamPlayer key={`${url}:${interactive}`} url={url} name={agent.name} thumbnail={thumbnail} interactive={interactive} />;
}

function StreamPlayer({ url, name, thumbnail, interactive }: { url: string | null; name: string; thumbnail?: WsThumbnailMessage; interactive: boolean }) {
  const [fallback, setFallback] = useState(false);
  const iframe = useRef<HTMLIFrameElement>(null);
  useEffect(() => {
    const frame = iframe.current;
    if (!frame) return;
    const unavailable = () => setFallback(true);
    frame.addEventListener("error", unavailable);
    return () => frame.removeEventListener("error", unavailable);
  }, [fallback]);
  return <div className="space-y-2">
    <div className="relative aspect-video overflow-hidden rounded-md border bg-muted">
      {url && !fallback ? <iframe
        ref={iframe}
        title={`${name} ${interactive ? "interactive" : "view-only"} desktop`}
        src={url}
        sandbox="allow-scripts allow-same-origin"
        allow="clipboard-read 'none'; clipboard-write 'none'"
        referrerPolicy="no-referrer"
        className="absolute inset-0 h-full w-full"
      /> : thumbnail ?
        // eslint-disable-next-line @next/next/no-img-element -- Ephemeral JPEG delivered by the session socket.
        <img src={`data:image/jpeg;base64,${thumbnail.jpeg_b64}`} alt={`${name}'s latest desktop thumbnail, step ${thumbnail.step}`} className="h-full w-full object-contain" /> :
        <p className="grid h-full place-items-center p-4 text-sm text-muted-foreground">{interactive ? "Interactive desktop unavailable" : "Waiting for a desktop thumbnail…"}</p>}
    </div>
    {url && <Button variant="ghost" size="sm" onClick={() => setFallback(value => !value)}>{fallback ? "Retry live stream" : "Show latest thumbnail"}</Button>}
    {interactive && (!url || fallback) && <p role="status" className="text-sm">Thumbnail is view only. Return control when finished.</p>}
  </div>;
}
