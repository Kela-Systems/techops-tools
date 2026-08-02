import {
  createContext,
  useCallback,
  useContext,
  useState,
  type ReactNode,
} from "react";

interface Toast {
  id: number;
  kind: "success" | "error";
  message: string;
}

interface ToastApi {
  success: (message: string) => void;
  error: (message: string) => void;
}

const ToastContext = createContext<ToastApi>({
  success: () => {},
  error: () => {},
});

let nextId = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((kind: Toast["kind"], message: string) => {
    const id = nextId++;
    setToasts((t) => [...t, { id, kind, message }]);
    setTimeout(
      () => setToasts((t) => t.filter((x) => x.id !== id)),
      kind === "error" ? 8000 : 4000,
    );
  }, []);

  const apiValue: ToastApi = {
    success: (m) => push("success", m),
    error: (m) => push("error", m),
  };

  return (
    <ToastContext.Provider value={apiValue}>
      {children}
      <div className="toast-stack">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast-${t.kind}`}>
            {t.message}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export const useToast = () => useContext(ToastContext);
