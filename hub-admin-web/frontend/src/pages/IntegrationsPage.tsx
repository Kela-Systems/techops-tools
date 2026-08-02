import { useCallback, useEffect, useMemo, useState } from "react";
import { api, computeMergePatch } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";
import Modal from "../components/Modal";
import JsonEditor, { parseJsonObject } from "../components/JsonEditor";
import type { Device, ImportResult, Integration, Manifest } from "../types";

// ── device setup_info editor ─────────────────────────────────────────────

function EditDeviceModal({
  ctx,
  device,
  onClose,
  onSaved,
}: {
  ctx: string;
  device: Device;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState(device.name);
  const [text, setText] = useState(
    JSON.stringify(device.setup_info, null, 2),
  );
  const [saving, setSaving] = useState(false);

  const save = async () => {
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      toast.error(`Invalid JSON: ${parsed.error}`);
      return;
    }
    const patch = computeMergePatch(device.setup_info, parsed.value);
    const body: { setup_info_patch?: Record<string, unknown>; name?: string } =
      {};
    if (Object.keys(patch).length > 0) body.setup_info_patch = patch;
    if (name !== device.name) body.name = name;
    if (!body.setup_info_patch && !body.name) {
      onClose();
      return;
    }
    setSaving(true);
    try {
      await api.updateDevice(ctx, device.integration_id, device.id, body);
      toast.success(`Device ${name} updated`);
      onSaved();
      onClose();
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal title={`Edit device — ${device.name}`} onClose={onClose} wide>
      <label className="field">
        <span>Name</span>
        <input value={name} onChange={(e) => setName(e.target.value)} />
      </label>
      <label className="field">
        <span>
          setup_info <em>(saved as an RFC 7396 merge-patch — untouched keys are preserved)</em>
        </span>
      </label>
      <JsonEditor value={text} onChange={setText} />
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Saving…" : "Save"}
        </button>
      </div>
    </Modal>
  );
}

// ── create device ────────────────────────────────────────────────────────

function CreateDeviceModal({
  ctx,
  integration,
  onClose,
  onSaved,
}: {
  ctx: string;
  integration: Integration;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [text, setText] = useState("{\n  \n}");
  const [saving, setSaving] = useState(false);

  const save = async () => {
    if (!name.trim()) {
      toast.error("Device name is required");
      return;
    }
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      toast.error(`Invalid JSON: ${parsed.error}`);
      return;
    }
    setSaving(true);
    try {
      const { device_id } = await api.createDevice(
        ctx,
        integration.id,
        name.trim(),
        parsed.value,
      );
      toast.success(`Device created (${device_id.slice(0, 12)}…)`);
      onSaved();
      onClose();
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  };

  const schemaKeys = useMemo(() => {
    const props = integration.device_setup_info_schema?.properties as
      | Record<string, unknown>
      | undefined;
    return props ? Object.keys(props) : [];
  }, [integration]);

  return (
    <Modal title={`New device in ${integration.name}`} onClose={onClose} wide>
      <label className="field">
        <span>Name</span>
        <input
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Radar 1000"
        />
      </label>
      <label className="field">
        <span>setup_info</span>
      </label>
      <JsonEditor value={text} onChange={setText} height="240px" />
      {schemaKeys.length > 0 && (
        <p className="hint">Schema fields: {schemaKeys.join(", ")}</p>
      )}
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>
          Cancel
        </button>
        <button className="btn btn-primary" onClick={save} disabled={saving}>
          {saving ? "Creating…" : "Create"}
        </button>
      </div>
    </Modal>
  );
}

// ── import device_config.json ────────────────────────────────────────────

function ImportDevicesModal({
  ctx,
  integration,
  onClose,
  onSaved,
}: {
  ctx: string;
  integration: Integration;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [config, setConfig] = useState<Record<string, unknown> | null>(null);
  const [fileName, setFileName] = useState("");
  const [section, setSection] = useState("");
  const [result, setResult] = useState<ImportResult | null>(null);
  const [importing, setImporting] = useState(false);

  const onFile = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text());
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("expected a JSON object of config sections");
      }
      setConfig(parsed);
      setFileName(file.name);
      const sections = Object.keys(parsed);
      // Auto-match section by manifest/integration name, like the CLI does.
      const nameLower = integration.name.toLowerCase();
      const match = sections.find(
        (s) =>
          s.toLowerCase().includes(nameLower) ||
          nameLower.includes(s.toLowerCase()),
      );
      setSection(match ?? sections[0] ?? "");
    } catch (e) {
      toast.error(`Cannot read config: ${(e as Error).message}`);
    }
  };

  const doImport = async () => {
    if (!config || !section) return;
    const devices = config[section];
    if (devices === null || typeof devices !== "object") {
      toast.error(`Section '${section}' is not an object`);
      return;
    }
    setImporting(true);
    try {
      const res = await api.importDevices(
        ctx,
        integration.id,
        devices as Record<string, unknown>,
      );
      setResult(res);
      toast.success(`Added ${res.created.length} device(s)`);
      onSaved();
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setImporting(false);
    }
  };

  return (
    <Modal
      title={`Import devices into ${integration.name}`}
      onClose={onClose}
      wide
    >
      <label className="field">
        <span>device_config.json</span>
        <input
          type="file"
          accept=".json,application/json"
          onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
        />
      </label>
      {config && (
        <>
          <p className="hint">
            {fileName}: {Object.keys(config).length} section(s)
          </p>
          <label className="field">
            <span>Section</span>
            <select
              value={section}
              onChange={(e) => setSection(e.target.value)}
            >
              {Object.keys(config).map((s) => (
                <option key={s} value={s}>
                  {s} (
                  {typeof config[s] === "object" && config[s] !== null
                    ? Object.keys(config[s] as object).length
                    : 0}{" "}
                  devices)
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      {result && (
        <div className="report">
          <h4>Created</h4>
          <ul>
            {result.created.map((c) => (
              <li key={c.device_id}>
                {c.name} <code>{c.device_id}</code>
              </li>
            ))}
          </ul>
          {Object.keys(result.dropped).length > 0 && (
            <>
              <h4>Dropped fields (not in the integration schema)</h4>
              <ul>
                {Object.entries(result.dropped).map(([name, keys]) => (
                  <li key={name}>
                    {name}: {keys.join(", ")}
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      )}
      <div className="modal-actions">
        <button className="btn" onClick={onClose}>
          {result ? "Close" : "Cancel"}
        </button>
        {!result && (
          <button
            className="btn btn-primary"
            onClick={doImport}
            disabled={!config || !section || importing}
          >
            {importing ? "Importing…" : "Import"}
          </button>
        )}
      </div>
    </Modal>
  );
}

// ── create integration ───────────────────────────────────────────────────

function CreateIntegrationModal({
  ctx,
  onClose,
  onSaved,
}: {
  ctx: string;
  onClose: () => void;
  onSaved: () => void;
}) {
  const toast = useToast();
  const [manifests, setManifests] = useState<Manifest[] | null>(null);
  const [manifestId, setManifestId] = useState("");
  const [text, setText] = useState("{}");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    api
      .listManifests(ctx)
      .then((m) => {
        setManifests(m);
        if (m.length > 0) setManifestId(m[0].manifest_id);
      })
      .catch((e) => toast.error(String(e.message)));
  }, [ctx, toast]);

  const save = async () => {
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      toast.error(`Invalid JSON: ${parsed.error}`);
      return;
    }
    setSaving(true);
    try {
      const config =
        Object.keys(parsed.value).length > 0 ? parsed.value : null;
      const { integration_id } = await api.createIntegration(
        ctx,
        manifestId,
        config,
      );
      toast.success(`Integration created (${integration_id.slice(0, 12)}…)`);
      onSaved();
      onClose();
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  };

  const selected = manifests?.find((m) => m.manifest_id === manifestId);

  return (
    <Modal title="New integration" onClose={onClose} wide>
      {manifests === null ? (
        <p className="hint">Loading manifests…</p>
      ) : (
        <>
          <label className="field">
            <span>Manifest</span>
            <select
              value={manifestId}
              onChange={(e) => setManifestId(e.target.value)}
            >
              {manifests.map((m) => (
                <option key={m.manifest_id} value={m.manifest_id}>
                  {m.name} ({m.version})
                </option>
              ))}
            </select>
          </label>
          {selected?.description && (
            <p className="hint">{selected.description}</p>
          )}
          <label className="field">
            <span>
              integration_config <em>(optional, {"{}"} = none)</em>
            </span>
          </label>
          <JsonEditor value={text} onChange={setText} height="180px" />
          <div className="modal-actions">
            <button className="btn" onClick={onClose}>
              Cancel
            </button>
            <button
              className="btn btn-primary"
              onClick={save}
              disabled={saving || !manifestId}
            >
              {saving ? "Creating…" : "Create"}
            </button>
          </div>
        </>
      )}
    </Modal>
  );
}

// ── integration row (expandable devices) ─────────────────────────────────

function IntegrationRow({
  ctx,
  integration,
  onChanged,
}: {
  ctx: string;
  integration: Integration;
  onChanged: () => void;
}) {
  const toast = useToast();
  const [expanded, setExpanded] = useState(false);
  const [devices, setDevices] = useState<Device[] | null>(null);
  const [editing, setEditing] = useState<Device | null>(null);
  const [creating, setCreating] = useState(false);
  const [importing, setImporting] = useState(false);

  const loadDevices = useCallback(() => {
    api
      .listDevices(ctx, integration.id)
      .then(setDevices)
      .catch((e) => toast.error(String(e.message)));
  }, [ctx, integration.id, toast]);

  useEffect(() => {
    if (expanded && devices === null) loadDevices();
  }, [expanded, devices, loadDevices]);

  const refresh = () => {
    loadDevices();
    onChanged();
  };

  return (
    <>
      <tr className="row-main" onClick={() => setExpanded((e) => !e)}>
        <td className="chevron">{expanded ? "▾" : "▸"}</td>
        <td className="strong">{integration.name}</td>
        <td>
          <code className="dim">{integration.manifest_id ?? "—"}</code>
        </td>
        <td>{integration.device_count}</td>
        <td onClick={(e) => e.stopPropagation()}>
          <button className="btn btn-sm" onClick={() => setCreating(true)}>
            + Device
          </button>{" "}
          <button className="btn btn-sm" onClick={() => setImporting(true)}>
            Import…
          </button>
        </td>
      </tr>
      {expanded && (
        <tr className="row-detail">
          <td colSpan={5}>
            {devices === null ? (
              <p className="hint">Loading devices…</p>
            ) : devices.length === 0 ? (
              <p className="hint">No devices.</p>
            ) : (
              <table className="subtable">
                <thead>
                  <tr>
                    <th>Device</th>
                    <th>ID</th>
                    <th>setup_info</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {devices.map((d) => (
                    <tr key={d.id}>
                      <td className="strong">{d.name}</td>
                      <td>
                        <code className="dim">{d.id}</code>
                      </td>
                      <td className="setup-preview">
                        <code>
                          {JSON.stringify(d.setup_info).slice(0, 80)}
                          {JSON.stringify(d.setup_info).length > 80 ? "…" : ""}
                        </code>
                      </td>
                      <td>
                        <button
                          className="btn btn-sm"
                          onClick={() => setEditing(d)}
                        >
                          Edit
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </td>
        </tr>
      )}
      {editing && (
        <EditDeviceModal
          ctx={ctx}
          device={editing}
          onClose={() => setEditing(null)}
          onSaved={refresh}
        />
      )}
      {creating && (
        <CreateDeviceModal
          ctx={ctx}
          integration={integration}
          onClose={() => setCreating(false)}
          onSaved={refresh}
        />
      )}
      {importing && (
        <ImportDevicesModal
          ctx={ctx}
          integration={integration}
          onClose={() => setImporting(false)}
          onSaved={refresh}
        />
      )}
    </>
  );
}

// ── page ─────────────────────────────────────────────────────────────────

export default function IntegrationsPage() {
  const { context } = useHub();
  const toast = useToast();
  const [started, setStarted] = useState(false);
  const [integrations, setIntegrations] = useState<Integration[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(() => {
    if (!context) return;
    setIntegrations(null);
    setError(null);
    api
      .listIntegrations(context)
      .then(setIntegrations)
      .catch((e) => setError(String(e.message)));
  }, [context]);

  // No automatic fetching: switching hubs resets to the disconnected state,
  // and nothing talks to a hub until the user explicitly loads.
  useEffect(() => {
    setStarted(false);
    setIntegrations(null);
    setError(null);
  }, [context]);

  const start = () => {
    setStarted(true);
    load();
  };

  const exportConfig = async () => {
    if (!context) return;
    try {
      const { config } = await api.exportIntegrations(context);
      const blob = new Blob([JSON.stringify(config, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${context}.device_config.json`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      toast.error(String((e as Error).message));
    }
  };

  if (!context) return <p className="hint page-pad">No hub context selected.</p>;

  return (
    <div className="page">
      <div className="page-header">
        <h2>Integrations</h2>
        {started && (
          <div>
            <button className="btn" onClick={load}>
              Reload
            </button>{" "}
            <button className="btn" onClick={exportConfig}>
              Export device_config
            </button>{" "}
            <button
              className="btn btn-primary"
              onClick={() => setCreating(true)}
            >
              + Integration
            </button>
          </div>
        )}
      </div>
      {!started && (
        <div className="card connect-card">
          <p className="hint">
            Not connected. Loading opens a port-forward into{" "}
            <strong>{context}</strong>.
          </p>
          <button className="btn btn-primary" onClick={start}>
            Load integrations from {context}
          </button>
        </div>
      )}
      {started && error && <p className="error-banner">{error}</p>}
      {started && integrations === null && !error && (
        <p className="hint">Connecting to {context}…</p>
      )}
      {integrations && (
        <table className="table">
          <thead>
            <tr>
              <th></th>
              <th>Name</th>
              <th>Manifest</th>
              <th>Devices</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {integrations.map((i) => (
              <IntegrationRow
                key={i.id}
                ctx={context}
                integration={i}
                onChanged={load}
              />
            ))}
          </tbody>
        </table>
      )}
      {creating && (
        <CreateIntegrationModal
          ctx={context}
          onClose={() => setCreating(false)}
          onSaved={load}
        />
      )}
    </div>
  );
}
