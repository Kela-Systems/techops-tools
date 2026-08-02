import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";
import type { Asset, EntityLink } from "../types";

export default function LinksPage() {
  const { context } = useHub();
  const toast = useToast();
  const [assets, setAssets] = useState<Asset[] | null>(null);
  const [links, setLinks] = useState<EntityLink[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sourceId, setSourceId] = useState("");
  const [targetId, setTargetId] = useState("");
  const [adding, setAdding] = useState(false);
  const [needsRestart, setNeedsRestart] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [started, setStarted] = useState(false);

  const load = useCallback(() => {
    if (!context) return;
    setAssets(null);
    setLinks(null);
    setError(null);
    Promise.all([api.listAssets(context), api.listLinks(context)])
      .then(([a, l]) => {
        setAssets(a);
        setLinks(l);
        if (a.length > 0) {
          setSourceId((cur) => cur || a[0].id);
          setTargetId((cur) => cur || (a[1]?.id ?? a[0].id));
        }
      })
      .catch((e) => setError(String(e.message)));
  }, [context]);

  // No automatic fetching: nothing talks to the hub until the user loads.
  useEffect(() => {
    setStarted(false);
    setAssets(null);
    setLinks(null);
    setError(null);
    setNeedsRestart(false);
  }, [context]);

  const start = () => {
    setStarted(true);
    load();
  };

  const addLink = async () => {
    if (!context || !sourceId || !targetId) return;
    if (sourceId === targetId) {
      toast.error("Source and target must differ");
      return;
    }
    setAdding(true);
    try {
      const { created } = await api.createLink(context, sourceId, targetId);
      if (created) {
        toast.success("Link added — restart the hub-server to take effect");
        setNeedsRestart(true);
        load();
      } else {
        toast.error("Link already exists");
      }
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setAdding(false);
    }
  };

  const restart = async () => {
    if (!context) return;
    if (!confirm(`Restart hub-server on ${context}?`)) return;
    setRestarting(true);
    try {
      const { message } = await api.restartServer(context);
      toast.success(message);
      setNeedsRestart(false);
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setRestarting(false);
    }
  };

  if (!context) return <p className="hint page-pad">No hub context selected.</p>;

  return (
    <div className="page">
      <div className="page-header">
        <h2>Entity links</h2>
        <div>
          {needsRestart && (
            <button
              className="btn btn-warn"
              onClick={restart}
              disabled={restarting}
            >
              {restarting ? "Restarting…" : "Restart hub-server"}
            </button>
          )}{" "}
          {started && (
            <button className="btn" onClick={load}>
              Reload
            </button>
          )}
        </div>
      </div>
      {!started && (
        <div className="card connect-card">
          <p className="hint">
            Not connected. Loading opens a port-forward into{" "}
            <strong>{context}</strong>.
          </p>
          <button className="btn btn-primary" onClick={start}>
            Load assets &amp; links from {context}
          </button>
        </div>
      )}
      {started && error && <p className="error-banner">{error}</p>}
      {started && (assets === null || links === null) && !error && (
        <p className="hint">Connecting to {context}…</p>
      )}
      {assets && links && (
        <div className="two-col">
          <section>
            <h3>Links ({links.length})</h3>
            <div className="add-link">
              <select
                value={sourceId}
                onChange={(e) => setSourceId(e.target.value)}
              >
                {assets.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.asset_type})
                  </option>
                ))}
              </select>
              <span className="arrow">→</span>
              <select
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
              >
                {assets.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} ({a.asset_type})
                  </option>
                ))}
              </select>
              <button
                className="btn btn-primary"
                onClick={addLink}
                disabled={adding || assets.length === 0}
              >
                {adding ? "Adding…" : "Add link"}
              </button>
            </div>
            {links.length === 0 ? (
              <p className="hint">No links configured.</p>
            ) : (
              <table className="table">
                <thead>
                  <tr>
                    <th>Source</th>
                    <th>Target</th>
                  </tr>
                </thead>
                <tbody>
                  {links.map((l, i) => (
                    <tr key={`${l.source_id}-${l.target_id}-${i}`}>
                      <td>{l.source_name}</td>
                      <td>{l.target_name}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </section>
          <section>
            <h3>Assets ({assets.length})</h3>
            <table className="table">
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Type</th>
                  <th>ID</th>
                </tr>
              </thead>
              <tbody>
                {assets.map((a) => (
                  <tr key={a.id}>
                    <td className="strong">{a.name}</td>
                    <td>{a.asset_type}</td>
                    <td>
                      <code className="dim">{a.id}</code>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>
        </div>
      )}
    </div>
  );
}
