import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import { api } from "./api";

interface HubState {
  contexts: string[];
  context: string | null;
  setContext: (ctx: string) => void;
  contextsError: string | null;
}

const HubContext = createContext<HubState>({
  contexts: [],
  context: null,
  setContext: () => {},
  contextsError: null,
});

const STORAGE_KEY = "hub-admin-context";

export function HubProvider({ children }: { children: ReactNode }) {
  const [contexts, setContexts] = useState<string[]>([]);
  const [context, setContextState] = useState<string | null>(
    localStorage.getItem(STORAGE_KEY),
  );
  const [contextsError, setContextsError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listContexts()
      .then((ctxs) => {
        setContexts(ctxs);
        setContextState((cur) =>
          cur && ctxs.includes(cur) ? cur : (ctxs[0] ?? null),
        );
      })
      .catch((e) => setContextsError(String(e.message ?? e)));
  }, []);

  const setContext = (ctx: string) => {
    localStorage.setItem(STORAGE_KEY, ctx);
    setContextState(ctx);
  };

  return (
    <HubContext.Provider
      value={{ contexts, context, setContext, contextsError }}
    >
      {children}
    </HubContext.Provider>
  );
}

export const useHub = () => useContext(HubContext);
