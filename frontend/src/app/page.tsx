import { Button } from "@/components/ui/button";
import { env } from "@/lib/env";

/** Placeholder home page. T05 replaces it with the real Home (SPEC §18 page 1). */
export default function HomePage() {
  return (
    <main className="mx-auto flex w-full max-w-2xl flex-1 flex-col justify-center gap-6 p-8">
      <h1 className="text-3xl font-semibold tracking-tight">{env.appName}</h1>
      <p className="text-muted-foreground">
        A team of AI agents doing real computer work together. The UI is under construction.
      </p>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
        <dt className="text-muted-foreground">API</dt>
        <dd>
          <code data-testid="api-url">{env.apiUrl}</code>
        </dd>
        <dt className="text-muted-foreground">Mock mode</dt>
        <dd>{env.mock ? "on" : "off"}</dd>
      </dl>
      <div>
        <Button disabled aria-label="Plan a new session (coming soon)">
          Plan
        </Button>
      </div>
    </main>
  );
}
