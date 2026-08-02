import { useState } from "react";
import { api } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";

export default function ServerPage() {
  const { context } = useHub();
  const toast = useToast();
  const [restarting, setRestarting] = useState(false);

  if (!context) return <p className="hint page-pad">No hub context selected.</p>;

  const restart = async () => {
    if (!confirm(`Restart hub-server on ${context}? Active sessions will drop.`))
      return;
    setRestarting(true);
    try {
      const { message } = await api.restartServer(context);
      toast.success(message);
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setRestarting(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>Server</h2>
      </div>
      <div className="card">
        <h3>Restart hub-server</h3>
        <p className="hint">
          Deletes the hub-server pod on <strong>{context}</strong>; Kubernetes
          recreates it. Needed after changing entity links or applying a site
          config.
        </p>
        <button
          className="btn btn-warn"
          onClick={restart}
          disabled={restarting}
        >
          {restarting ? "Restarting…" : `Restart on ${context}`}
        </button>
      </div>
    </div>
  );
}
