import {
  createContext,
  useCallback,
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
  refreshContexts: () => Promise<string[]>;
  contextsError: string | null;
}

const HubContext = createContext<HubState>({
  contexts: [],
  context: null,
  setContext: () => {},
  refreshContexts: async () => [],
  contextsError: null,
});

const STORAGE_KEY = "hub-admin-context";

export function HubProvider({ children }: { children: ReactNode }) {
  const [contexts, setContexts] = useState<string[]>([]);
  const [context, setContextState] = useState<string | null>(
    localStorage.getItem(STORAGE_KEY),
  );
  const [contextsError, setContextsError] = useState<string | null>(null);

  const refreshContexts = useCallback(async () => {
    const ctxs = await api.listContexts();
    setContexts(ctxs);
    setContextsError(null);
    return ctxs;
  }, []);

  useEffect(() => {
    refreshContexts()
      .then((ctxs) => {
        setContextState((cur) =>
          cur && ctxs.includes(cur) ? cur : (ctxs[0] ?? null),
        );
      })
      .catch((e) => setContextsError(String(e.message ?? e)));
  }, [refreshContexts]);

  const setContext = (ctx: string) => {
    localStorage.setItem(STORAGE_KEY, ctx);
    setContextState(ctx);
  };

  return (
    <HubContext.Provider
      value={{ contexts, context, setContext, refreshContexts, contextsError }}
    >
      {children}
    </HubContext.Provider>
  );
}

export const useHub = () => useContext(HubContext);
