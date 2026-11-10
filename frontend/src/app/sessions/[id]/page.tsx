import { LiveSession } from "@/components/live/live-session";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <LiveSession key={id} sessionId={id} />;
}
