import { useCallback, useEffect, useState } from "react";
import { api, pollJob } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";
import JsonEditor, { parseJsonObject } from "../components/JsonEditor";
import type { DeviceConfigApplyReport, SavedDeviceConfig } from "../types";

function ConfigApplyReport({ report }: { report: DeviceConfigApplyReport }) {
  return (
    <div className="report">
      <p className="hint">
        Applied to <strong>{report.target_context}</strong>
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Section</th>
            <th>Integration</th>
            <th>Devices added</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {report.applied.map((s) => (
            <tr key={s.section}>
              <td className="strong">{s.section}</td>
              <td>{s.integration_name ?? "—"}</td>
              <td>
                {s.created?.length ?? 0}
                {s.dropped && Object.keys(s.dropped).length > 0 && (
                  <span
                    className="badge badge-warn"
                    title={Object.entries(s.dropped)
                      .map(([n, k]) => `${n}: ${k.join(", ")}`)
                      .join("\n")}
                  >
                    dropped fields
                  </span>
                )}
              </td>
              <td>
                {s.error ? (
                  <span className="error-text">{s.error}</span>
                ) : (
                  "ok"
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function DeviceConfigsPage() {
  const { context, contexts } = useHub();
  const toast = useToast();

  const [configs, setConfigs] = useState<SavedDeviceConfig[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [savedText, setSavedText] = useState("");
  const [saving, setSaving] = useState(false);

  const [applyTarget, setApplyTarget] = useState("");
  const [applying, setApplying] = useState(false);
  const [applyReport, setApplyReport] = useState<DeviceConfigApplyReport | null>(
    null,
  );

  const loadList = useCallback(() => {
    api
      .listDeviceConfigs()
      .then(setConfigs)
      .catch((e) => toast.error(String(e.message)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(loadList, [loadList]);

  const select = async (name: string) => {
    try {
      const { config } = await api.getDeviceConfig(name);
      const pretty = JSON.stringify(config, null, 2);
      setSelected(name);
      setText(pretty);
      setSavedText(pretty);
      setApplyReport(null);
    } catch (e) {
      toast.error(String((e as Error).message));
    }
  };

  const createNew = async () => {
    const name = prompt(
      "New config file name (must end with .json):",
      "site.device_config.json",
    );
    if (!name) return;
    if (!name.endsWith(".json")) {
      toast.error("Name must end with .json");
      return;
    }
    try {
      await api.saveDeviceConfig(name, {});
      loadList();
      await select(name);
    } catch (e) {
      toast.error(String((e as Error).message));
    }
  };

  const dirty = text !== savedText;

  const save = async () => {
    if (!selected) return;
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      toast.error(`Invalid JSON: ${parsed.error}`);
      return;
    }
    setSaving(true);
    try {
      await api.saveDeviceConfig(selected, parsed.value);
      setSavedText(text);
      toast.success(`Saved ${selected}`);
      loadList();
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setSaving(false);
    }
  };

  const apply = async () => {
    if (!selected || !context) return;
    const parsed = parseJsonObject(text);
    if (!parsed.ok) {
      toast.error(`Invalid JSON: ${parsed.error}`);
      return;
    }
    const target = applyTarget || context;
    const sections = Object.keys(parsed.value);
    if (
      !confirm(
        `Apply '${selected}' (${sections.length} section(s): ${sections.join(
          ", ",
        )}) to ${target}? Devices are added to the matching integrations.`,
      )
    )
      return;
    setApplying(true);
    setApplyReport(null);
    try {
      const { job_id } = await api.applyDeviceConfig(target, parsed.value);
      const report = await pollJob<DeviceConfigApplyReport>(job_id);
      setApplyReport(report);
      const total = report.applied.reduce(
        (n, s) => n + (s.created?.length ?? 0),
        0,
      );
      toast.success(`Added ${total} device(s) on ${target}`);
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setApplying(false);
    }
  };

  if (!context) return <p className="hint page-pad">No hub context selected.</p>;

  return (
    <div className="page">
      <div className="page-header">
        <h2>Device configs</h2>
        <div>
          <button className="btn" onClick={loadList}>
            Refresh
          </button>{" "}
          <button className="btn btn-primary" onClick={createNew}>
            + New config
          </button>
        </div>
      </div>

      <div className="card">
        <p className="hint">
          <code>device_config.json</code> files from the server's{" "}
          <code>device-configs/</code> folder. Select one to edit it and apply
          its devices to a hub — sections are matched to integrations by name.
        </p>
        {configs === null ? (
          <p className="hint">Loading…</p>
        ) : configs.length === 0 ? (
          <p className="hint">No configs yet — create one.</p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>File</th>
                <th>Sections</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {configs.map((c) => (
                <tr
                  key={c.file_name}
                  className={c.file_name === selected ? "row-selected" : ""}
                >
                  <td className="strong">{c.file_name}</td>
                  <td>
                    {c.error ? (
                      <span className="error-text">{c.error}</span>
                    ) : (
                      Object.entries(c.sections ?? {})
                        .map(([s, n]) => `${s} (${n})`)
                        .join(", ") || "empty"
                    )}
                  </td>
                  <td>
                    {!c.error && (
                      <button
                        className="btn btn-sm"
                        onClick={() => select(c.file_name)}
                      >
                        {c.file_name === selected ? "Reload" : "Edit"}
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {selected && (
        <div className="card">
          <div className="card-title-row">
            <h3>
              {selected}
              {dirty && <span className="badge badge-warn">unsaved</span>}
            </h3>
            <button
              className="btn btn-primary"
              onClick={save}
              disabled={saving || !dirty}
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
          <JsonEditor value={text} onChange={setText} height="420px" />

          <h4>Apply to a hub</h4>
          <div className="apply-controls">
            <label className="field">
              <span>Target hub</span>
              <select
                value={applyTarget || context}
                onChange={(e) => setApplyTarget(e.target.value)}
              >
                {contexts.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </label>
            <p className="hint">
              Applies the editor contents (including unsaved changes). Each
              section's devices are added to the integration whose name
              matches; fields outside the integration's schema are dropped and
              reported.
            </p>
            <div>
              <button
                className="btn btn-warn"
                onClick={apply}
                disabled={applying}
              >
                {applying ? "Applying…" : "Apply devices"}
              </button>
            </div>
          </div>
          {applyReport && <ConfigApplyReport report={applyReport} />}
        </div>
      )}
    </div>
  );
}
