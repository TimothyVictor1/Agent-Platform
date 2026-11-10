"use client";

import { useEffect, useRef } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import type { Artifact } from "@/contracts/generated";
import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";

export function useCommand<T>(run: (value: T) => Promise<unknown>) {
  return useMutation({ mutationFn: run });
}

export function CommandFeedback({ error, success }: { error: Error | null; success: boolean }) {
  return <>{error && <p role="alert" className="text-sm text-red-600">{error.message}</p>}
    {success && <p role="status" className="text-sm text-muted-foreground">Command sent. Waiting for the session update.</p>}</>;
}

// Images and downloads go through api.ts, including authentication and mock mode.
export function FrameImage({ frameId, alt }: { frameId: string; alt: string }) {
  const frame = useQuery({ queryKey: ["frame", frameId], queryFn: () => api.frame(frameId), staleTime: Infinity });
  const image = useRef<HTMLImageElement>(null);
  useEffect(() => {
    if (!frame.data) return;
    const url = URL.createObjectURL(frame.data);
    if (image.current) image.current.src = url;
    return () => URL.revokeObjectURL(url);
  }, [frame.data]);
  if (frame.error) return <p role="alert">Screenshot unavailable: {frame.error.message}</p>;
  // eslint-disable-next-line @next/next/no-img-element -- Authenticated blob, with no image optimizer request.
  return frame.data ? <img ref={image} alt={alt} className="w-full rounded border object-contain" /> : <p role="status">Loading screenshot…</p>;
}

export function ArtifactDownload({ artifactId, artifact }: { artifactId: string; artifact?: Artifact }) {
  const download = useCommand(async () => {
    const blob = await api.downloadArtifact(artifactId);
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = artifact?.name || artifactId;
    document.body.append(link);
    link.click();
    link.remove();
    // Give the browser time to start reading the blob before releasing it.
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });
  return <div><Button variant="link" disabled={download.isPending} onClick={() => download.mutate(undefined)}>Download {artifact?.name || artifactId}</Button>
    {download.error && <p role="alert">{download.error.message}</p>}</div>;
}
