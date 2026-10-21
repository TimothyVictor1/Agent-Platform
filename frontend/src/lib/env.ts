/** Public runtime configuration (NEXT_PUBLIC_* variables, inlined at build time). */
export const env = {
  appName: process.env.NEXT_PUBLIC_APP_NAME || "Agent Platform",
  apiUrl: process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000",
  wsUrl: process.env.NEXT_PUBLIC_WS_URL || "ws://localhost:8000",
  apiToken: process.env.NEXT_PUBLIC_API_TOKEN || undefined,
  mock: process.env.NEXT_PUBLIC_MOCK === "1",
} as const;
