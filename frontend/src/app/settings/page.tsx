"use client";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { ProviderHealth } from "@/components/provider-health";
import { EmptyState } from "@/components/shared/empty-state";
export default function SettingsPage() {
  const profiles = useQuery({ queryKey: ["profiles"], queryFn: api.profiles });
  return <main className="mx-auto w-full max-w-6xl space-y-8 px-6 py-10"><div><h1 className="text-3xl font-semibold">Settings</h1><p className="mt-2 text-muted-foreground">Model profiles and provider connections are managed by your platform administrator.</p></div>
    <section aria-labelledby="profiles-title" className="space-y-4"><h2 id="profiles-title" className="text-lg font-semibold">Model profiles</h2>
      {profiles.isPending && <p role="status">Loading profiles…</p>}{profiles.error && <p role="alert">Could not load profiles: {profiles.error.message}</p>}
      {profiles.data?.items.length === 0 && <EmptyState title="No model profiles configured" />}
      {profiles.data?.items.map(profile => <article key={profile.name} className="overflow-x-auto rounded-xl border bg-card p-5"><h3 className="mb-3 font-semibold">{profile.name}{profile.name === profiles.data.default_profile && " · Default"}</h3><table className="w-full text-left text-sm"><caption className="sr-only">Roles in {profile.name}</caption><thead><tr><th className="p-2">Role</th><th className="p-2">Provider</th><th className="p-2">Model</th><th className="p-2">Vision</th></tr></thead><tbody>{Object.entries(profile.roles).map(([name, role]) => role && <tr key={name} className="border-t"><td className="p-2">{name}</td><td className="p-2">{role.provider}</td><td className="p-2">{role.model}</td><td className="p-2">{role.vision ? "Yes" : "No"}</td></tr>)}</tbody></table></article>)}
    </section><ProviderHealth /><p className="text-sm text-muted-foreground">To change profiles, edit <code>config/models.yaml</code>. See <code>config/models.example.yaml</code> for an example.</p>
  </main>;
}
