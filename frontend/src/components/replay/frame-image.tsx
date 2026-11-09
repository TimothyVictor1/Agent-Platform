"use client";

import { memo, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { Frame } from "@/contracts/generated";
import { api } from "@/lib/api";

export const FrameImage = memo(function FrameImage({ frame, label }: { frame: Frame; label: string }) {
  const ref = useRef<HTMLDivElement>(null);
  const [visible, setVisible] = useState(false);
  const [url, setUrl] = useState<{ blob: Blob; value: string } | null>(null);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    if (typeof IntersectionObserver === "undefined") { setVisible(true); return; }
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) { setVisible(true); observer.disconnect(); }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  const image = useQuery({ queryKey: ["replay-frame", frame.session_id, frame.id], queryFn: () => api.frame(frame.id), enabled: visible, staleTime: Infinity, gcTime: 30000 });
  useEffect(() => {
    if (!image.data) return;
    const value = URL.createObjectURL(image.data);
    setUrl({ blob: image.data, value });
    return () => URL.revokeObjectURL(value);
  }, [image.data]);
  return <div ref={ref} className="flex h-full min-h-10 w-full items-center justify-center overflow-hidden rounded bg-muted text-xs text-muted-foreground">
    {url && url.blob === image.data
      // Frame blobs require authenticated API requests and object URL cleanup.
      // eslint-disable-next-line @next/next/no-img-element
      ? <img src={url.value} alt={label} loading="lazy" className="h-full max-h-[60vh] w-full object-contain" />
      : image.isError ? <span role="status">Frame unavailable</span>
        : <span>{visible ? "Loading frame…" : "Frame preview"}</span>}
  </div>;
});
