import { useCallback, useEffect, useState } from "react";
import { api, pollJob } from "../api";
import { useHub } from "../hub";
import { useToast } from "../toast";
import type {
  ProfileApplyReport,
  ProfileSummary,
  SavedProfile,
} from "../types";

function SummaryTable({ summary }: { summary: ProfileSummary }) {
  return (
    <div className="report">
      <p className="hint">
        Source: <strong>{summary.source_context}</strong>
        {summary.site_config_keys.length > 0 && (
          <>
            {" "}
            · SiteConfig keys: {summary.site_config_keys.join(", ")}
          </>
        )}
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Integration</th>
            <th>Manifest</th>
            <th>Config</th>
            <th>Devices</th>
          </tr>
        </thead>
        <tbody>
          {summary.integrations.map((i, idx) => (
            <tr key={idx}>
              <td className="strong">{i.name}</td>
              <td>{i.manifest_name ?? i.manifest_id ?? "—"}</td>
              <td>{i.has_config ? "yes" : "—"}</td>
              <td>{i.device_count}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ApplyReport({ report }: { report: ProfileApplyReport }) {
  return (
    <div className="report">
      <p className="hint">
        Applied to <strong>{report.target_context}</strong> · SiteConfig{" "}
        {report.site_config_applied ? "replaced" : "not touched"}
      </p>
      <table className="table">
        <thead>
          <tr>
            <th>Integration</th>
            <th>Manifest</th>
            <th>Devices</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          {report.applied.map((a, idx) => (
            <tr key={idx}>
              <td className="strong">{a.name}</td>
              <td>
                {a.manifest_name ?? a.manifest_id ?? "—"}
                {a.matched_by_name && (
                  <span className="badge">matched by name</span>
                )}
              </td>
              <td>
                {a.devices.length}
                {Object.keys(a.dropped).length > 0 && (
                  <span
                    className="badge badge-warn"
                    title={Object.entries(a.dropped)
                      .map(([n, k]) => `${n}: ${k.join(", ")}`)
                      .join("\n")}
                  >
                    dropped fields
                  </span>
                )}
              </td>
              <td>
                {a.error ? (
                  <span className="error-text">{a.error}</span>
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

export default function ProfilesPage() {
  const { context, contexts } = useHub();
  const toast = useToast();

  // export
  const [exportSiteConfig, setExportSiteConfig] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [exportSummary, setExportSummary] = useState<ProfileSummary | null>(
    null,
  );

  // saved profiles (server-side profiles/ folder)
  const [saved, setSaved] = useState<SavedProfile[] | null>(null);
  const loadSaved = useCallback(() => {
    api
      .listSavedProfiles()
      .then(setSaved)
      .catch((e) => toast.error(String(e.message)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(loadSaved, [loadSaved]);

  // inspect + apply — the selected bundle comes from a saved profile or an upload
  const [bundle, setBundle] = useState<Record<string, unknown> | null>(null);
  const [bundleName, setBundleName] = useState("");
  const [inspectSummary, setInspectSummary] = useState<ProfileSummary | null>(
    null,
  );
  const [applyTarget, setApplyTarget] = useState<string>("");
  const [applySiteConfig, setApplySiteConfig] = useState(true);
  const [applying, setApplying] = useState(false);
  const [applyReport, setApplyReport] = useState<ProfileApplyReport | null>(
    null,
  );

  if (!context) return <p className="hint page-pad">No hub context selected.</p>;

  const selectBundle = (
    b: Record<string, unknown>,
    name: string,
    summary: ProfileSummary | null,
  ) => {
    setBundle(b);
    setBundleName(name);
    setInspectSummary(summary);
    setApplyReport(null);
  };

  const onFile = async (file: File) => {
    try {
      const parsed = JSON.parse(await file.text());
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("expected a profile bundle object");
      }
      selectBundle(parsed, file.name, null);
    } catch (e) {
      toast.error(`Cannot read profile: ${(e as Error).message}`);
    }
  };

  const selectSaved = async (name: string) => {
    try {
      const { bundle, summary } = await api.getSavedProfile(name);
      selectBundle(bundle, name, summary);
    } catch (e) {
      toast.error(String((e as Error).message));
    }
  };

  const doExport = async () => {
    setExporting(true);
    setExportSummary(null);
    try {
      const { bundle, summary } = await api.exportProfile(
        context,
        exportSiteConfig,
      );
      setExportSummary(summary);
      const blob = new Blob([JSON.stringify(bundle, null, 2)], {
        type: "application/json",
      });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `${context}.profile.json`;
      a.click();
      URL.revokeObjectURL(a.href);
      toast.success(`Profile exported from ${context}`);
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setExporting(false);
    }
  };

  const doInspect = async () => {
    if (!bundle) return;
    try {
      setInspectSummary(await api.inspectProfile(bundle));
    } catch (e) {
      toast.error(String((e as Error).message));
    }
  };

  const doApply = async () => {
    if (!bundle) return;
    const target = applyTarget || context;
    if (
      !confirm(
        `Apply '${bundleName}' to ${target}?` +
          (applySiteConfig
            ? " This FULLY REPLACES the destination SiteConfig."
            : ""),
      )
    )
      return;
    setApplying(true);
    setApplyReport(null);
    try {
      const { job_id } = await api.applyProfile(target, bundle, applySiteConfig);
      const report = await pollJob<ProfileApplyReport>(job_id);
      setApplyReport(report);
      toast.success(
        `Profile applied to ${target} — restart the hub-server to activate`,
      );
    } catch (e) {
      toast.error(String((e as Error).message));
    } finally {
      setApplying(false);
    }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>Site profiles</h2>
      </div>

      <div className="card">
        <div className="card-title-row">
          <h3>Saved profiles</h3>
          <button className="btn btn-sm" onClick={loadSaved}>
            Refresh
          </button>
        </div>
        <p className="hint">
          Profile bundles from the server's <code>profiles/</code> folder.
          Select one to inspect and apply it.
        </p>
        {saved === null ? (
          <p className="hint">Loading…</p>
        ) : saved.length === 0 ? (
          <p className="hint">
            No saved profiles. Drop <code>*.profile.json</code> files into the
            folder and hit Refresh.
          </p>
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th>File</th>
                <th>Source</th>
                <th>Exported</th>
                <th>Integrations</th>
                <th>Devices</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {saved.map((p) => (
                <tr
                  key={p.file_name}
                  className={p.file_name === bundleName ? "row-selected" : ""}
                >
                  <td className="strong">{p.file_name}</td>
                  {p.error ? (
                    <td colSpan={4}>
                      <span className="error-text">{p.error}</span>
                    </td>
                  ) : (
                    <>
                      <td>{p.source_context}</td>
                      <td className="dim">{p.exported_at ?? "—"}</td>
                      <td>{p.integration_count}</td>
                      <td>{p.device_count}</td>
                    </>
                  )}
                  <td>
                    {!p.error && (
                      <button
                        className="btn btn-sm"
                        onClick={() => selectSaved(p.file_name)}
                      >
                        Select
                      </button>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h3>Export from {context}</h3>
        <p className="hint">
          Capture every integration (config + devices) and the SiteConfig as a
          portable <code>.profile.json</code>.
        </p>
        <label className="checkbox">
          <input
            type="checkbox"
            checked={exportSiteConfig}
            onChange={(e) => setExportSiteConfig(e.target.checked)}
          />
          Include SiteConfig
        </label>
        <div>
          <button
            className="btn btn-primary"
            onClick={doExport}
            disabled={exporting}
          >
            {exporting ? "Exporting…" : "Export & download"}
          </button>
        </div>
        {exportSummary && <SummaryTable summary={exportSummary} />}
      </div>

      <div className="card">
        <h3>Inspect / apply a profile</h3>
        <label className="field">
          <span>
            …or upload a profile bundle (<code>.profile.json</code>)
          </span>
          <input
            type="file"
            accept=".json,application/json"
            onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])}
          />
        </label>
        {bundle && (
          <>
            <p className="hint">
              Selected: <strong>{bundleName}</strong>
            </p>
            {!inspectSummary && (
              <div className="btn-row">
                <button className="btn" onClick={doInspect}>
                  Inspect
                </button>
              </div>
            )}
            {inspectSummary && <SummaryTable summary={inspectSummary} />}

            <h4>Apply</h4>
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
              <label className="checkbox">
                <input
                  type="checkbox"
                  checked={applySiteConfig}
                  onChange={(e) => setApplySiteConfig(e.target.checked)}
                />
                Replace SiteConfig{" "}
                <em>
                  (disable when deploying to a genuinely different site — entity
                  links reference site-specific asset IDs)
                </em>
              </label>
              <div>
                <button
                  className="btn btn-warn"
                  onClick={doApply}
                  disabled={applying}
                >
                  {applying ? "Applying…" : "Apply profile"}
                </button>
              </div>
            </div>
            {applyReport && <ApplyReport report={applyReport} />}
          </>
        )}
      </div>
    </div>
  );
}
