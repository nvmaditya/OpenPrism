export default function Home() {
  return (
    <main className="min-h-screen bg-zinc-950 text-zinc-100 flex flex-col">
      <header className="border-b border-zinc-800 px-6 py-4 flex items-center gap-3">
        <span className="text-lg font-semibold tracking-tight">Prism</span>
        <span className="text-xs text-zinc-500">local Phase 0 shell</span>
      </header>
      <div className="flex flex-1">
        <aside className="w-56 border-r border-zinc-800 p-4 text-sm text-zinc-400">
          <p className="text-zinc-200 font-medium mb-3">Your Projects</p>
          <ul className="space-y-2">
            <li className="text-zinc-500">No projects yet (Phase 1)</li>
          </ul>
        </aside>
        <section className="flex-1 p-8">
          <h1 className="text-2xl font-semibold mb-2">Dashboard shell</h1>
          <p className="text-zinc-400 max-w-xl">
            Next.js App Router shell for the Prism reverse-engineer monorepo.
            Backend health lives at{" "}
            <code className="text-zinc-200">GET /health</code>. Agent, compile,
            and IDE panels arrive in later phases.
          </p>
        </section>
      </div>
    </main>
  );
}
