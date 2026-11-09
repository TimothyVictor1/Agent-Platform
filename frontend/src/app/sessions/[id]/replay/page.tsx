import { ReplayPage } from "@/components/replay/replay-page";
export default async function Page({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <ReplayPage key={id} sessionId={id} />;
}
