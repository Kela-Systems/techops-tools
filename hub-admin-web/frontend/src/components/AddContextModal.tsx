import { useState } from "react";
import { api, pollJob } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";
import Modal from "./Modal";

export default function AddContextModal({ onClose }: { onClose: () => void }) {
  const { contexts, setContext, refreshContexts } = useHub();
  const toast = useToast();
  const [host, setHost] = useState("");
  const [user, setUser] = useState("kela");
  const [contextName, setContextName] = useState("");
  const [running, setRunning] = useState(false);
  const [output, setOutput] = useState<string | null>(null);

  const submit = async () => {
    const site = host.trim();
    if (!site) {
      toast.error("Site host is required");
      return;
    }
    const name = contextName.trim() || site;

    // Already registered? Just switch to it — the script is only for new sites.
    if (contexts.includes(name)) {
      setContext(name);
      toast.success(`Context ${name} already exists — selected it`);
      onClose();
      return;
    }

    setRunning(true);
    setOutput(null);
    try {
      const { job_id } = await api.addContext(
        site,
        user.trim() || "kela",
        contextName.trim() || undefined,
      );
      const result = await pollJob<{ context: string; output: string }>(
        job_id,
      );
      setOutput(result.output);
      await refreshContexts();
      setContext(result.context);
      toast.success(`Context ${result.context} added and selected`);
    } catch (e) {
      const msg = String((e as Error).message);
      setOutput(msg);
      toast.error("Adding context failed — see output");
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal title="Add hub context" onClose={onClose} wide>
      <p className="hint">
        Type a site to connect to. If its kubectl context doesn't exist yet,
        the server SSHes into the site (k3s) and registers one via{" "}
        <code>scripts/k3s_kubeconfig.sh</code>.
      </p>
      <label className="field">
        <span>Site host (hostname or IP, e.g. a tailnet name)</span>
        <input
          value={host}
          onChange={(e) => setHost(e.target.value)}
          placeholder="kela-sys-01234"
          disabled={running}
        />
      </label>
      <label className="field">
        <span>SSH user</span>
        <input
          value={user}
          onChange={(e) => setUser(e.target.value)}
          disabled={running}
        />
      </label>
      <label className="field">
        <span>
          Context name <em>(optional — defaults to the host)</em>
        </span>
        <input
          value={contextName}
          onChange={(e) => setContextName(e.target.value)}
          placeholder={host.trim() || "same as host"}
          disabled={running}
        />
      </label>
      {output !== null && <pre className="script-output">{output}</pre>}
      <div className="modal-actions">
        <button className="btn" onClick={onClose} disabled={running}>
          Close
        </button>
        <button
          className="btn btn-primary"
          onClick={submit}
          disabled={running || !host.trim()}
        >
          {running ? "Running…" : "Add / select"}
        </button>
      </div>
    </Modal>
  );
}
