import { SessionPlaceholder } from "@/components/session-placeholder";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <SessionPlaceholder key={id} sessionId={id} title="Live session" />;
}
