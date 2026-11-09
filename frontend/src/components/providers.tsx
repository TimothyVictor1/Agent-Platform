"use client";
import { createContext, useContext, useState } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toast } from "radix-ui";
import { Button } from "@/components/ui/button";

const ToastContext = createContext<(message: string) => void>(() => {});
export const useToast = () => useContext(ToastContext);
export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { staleTime: 15000, retry: 1 } } }));
  const [messages, setMessages] = useState<{ id: number; text: string }[]>([]);
  const notify = (text: string) => setMessages(items => [...items, { id: Date.now() + Math.random(), text }]);
  return <QueryClientProvider client={client}><ToastContext.Provider value={notify}><Toast.Provider duration={5000}>
    {children}
    {messages.map(message => <Toast.Root key={message.id} className="flex items-center gap-4 rounded-lg border bg-popover p-4 shadow-lg" onOpenChange={open => { if (!open) setMessages(items => items.filter(item => item.id !== message.id)); }}>
      <Toast.Description>{message.text}</Toast.Description><Toast.Close asChild><Button variant="ghost" aria-label="Dismiss notification">Close</Button></Toast.Close>
    </Toast.Root>)}
    <Toast.Viewport className="fixed right-4 bottom-4 z-50 flex w-96 max-w-[90vw] flex-col gap-2" aria-label="Notifications" />
  </Toast.Provider></ToastContext.Provider></QueryClientProvider>;
}
