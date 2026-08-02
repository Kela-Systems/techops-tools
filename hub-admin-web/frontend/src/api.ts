import type {
  Asset,
  Device,
  EntityLink,
  ImportResult,
  Integration,
  Job,
  Manifest,
  ProfileSummary,
  SavedDeviceConfig,
  SavedProfile,
} from "./types";

class ApiError extends Error {
  status: number;
  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body.detail) detail = String(body.detail);
    } catch {
      // non-JSON error body
    }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

const get = <T>(path: string) => request<T>(path);
const post = <T>(path: string, body?: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body ?? {}) });
const patch = <T>(path: string, body: unknown) =>
  request<T>(path, { method: "PATCH", body: JSON.stringify(body) });

const hub = (ctx: string) => `/api/hubs/${encodeURIComponent(ctx)}`;

export const api = {
  listContexts: () =>
    get<{ contexts: string[] }>("/api/contexts").then((r) => r.contexts),

  listManifests: (ctx: string) => get<Manifest[]>(`${hub(ctx)}/manifests`),
  getManifestSchemas: (ctx: string, manifestId: string) =>
    get<{
      integration_config_schema: Record<string, unknown> | null;
      device_setup_info_schema: Record<string, unknown> | null;
    }>(`${hub(ctx)}/manifests/${manifestId}/schemas`),

  listIntegrations: (ctx: string) =>
    get<Integration[]>(`${hub(ctx)}/integrations`),
  exportIntegrations: (ctx: string) =>
    get<{ config: Record<string, unknown>; summary: unknown[] }>(
      `${hub(ctx)}/integrations/export`,
    ),
  createIntegration: (
    ctx: string,
    manifestId: string,
    config: Record<string, unknown> | null,
  ) =>
    post<{ integration_id: string }>(`${hub(ctx)}/integrations`, {
      manifest_id: manifestId,
      config,
    }),

  listDevices: (ctx: string, integrationId: string) =>
    get<Device[]>(`${hub(ctx)}/integrations/${integrationId}/devices`),
  createDevice: (
    ctx: string,
    integrationId: string,
    name: string,
    setupInfo: Record<string, unknown>,
  ) =>
    post<{ device_id: string }>(
      `${hub(ctx)}/integrations/${integrationId}/devices`,
      { name, setup_info: setupInfo },
    ),
  updateDevice: (
    ctx: string,
    integrationId: string,
    deviceId: string,
    body: { setup_info_patch?: Record<string, unknown>; name?: string },
  ) =>
    patch<{ ok: boolean }>(
      `${hub(ctx)}/integrations/${integrationId}/devices/${deviceId}`,
      body,
    ),
  importDevices: (
    ctx: string,
    integrationId: string,
    devices: Record<string, unknown>,
  ) =>
    post<ImportResult>(`${hub(ctx)}/devices/import`, {
      integration_id: integrationId,
      devices,
    }),

  listAssets: (ctx: string) => get<Asset[]>(`${hub(ctx)}/assets`),
  listLinks: (ctx: string) => get<EntityLink[]>(`${hub(ctx)}/links`),
  createLink: (ctx: string, sourceId: string, targetId: string) =>
    post<{ created: boolean }>(`${hub(ctx)}/links`, {
      source_id: sourceId,
      target_id: targetId,
    }),

  listDeviceConfigs: () => get<SavedDeviceConfig[]>("/api/device-configs"),
  getDeviceConfig: (name: string) =>
    get<{ file_name: string; config: Record<string, unknown> }>(
      `/api/device-configs/${encodeURIComponent(name)}`,
    ),
  saveDeviceConfig: (name: string, config: Record<string, unknown>) =>
    request<{ file_name: string; saved: boolean }>(
      `/api/device-configs/${encodeURIComponent(name)}`,
      { method: "PUT", body: JSON.stringify({ config }) },
    ),
  applyDeviceConfig: (
    ctx: string,
    config: Record<string, unknown>,
    sections?: string[],
  ) =>
    post<{ job_id: string }>(`${hub(ctx)}/device-config/apply`, {
      config,
      sections,
    }),

  listSavedProfiles: () => get<SavedProfile[]>("/api/profiles"),
  getSavedProfile: (name: string) =>
    get<{
      file_name: string;
      bundle: Record<string, unknown>;
      summary: ProfileSummary;
    }>(`/api/profiles/${encodeURIComponent(name)}`),

  exportProfile: (ctx: string, includeSiteConfig: boolean) =>
    post<{ bundle: Record<string, unknown>; summary: ProfileSummary }>(
      `${hub(ctx)}/profile/export`,
      { include_site_config: includeSiteConfig },
    ),
  inspectProfile: (bundle: Record<string, unknown>) =>
    post<ProfileSummary>("/api/profile/inspect", { bundle }),
  applyProfile: (
    ctx: string,
    bundle: Record<string, unknown>,
    includeSiteConfig: boolean,
  ) =>
    post<{ job_id: string }>(`${hub(ctx)}/profile/apply`, {
      bundle,
      include_site_config: includeSiteConfig,
    }),
  getJob: (jobId: string) => get<Job>(`/api/jobs/${jobId}`),

  restartServer: (ctx: string) =>
    post<{ message: string }>(`${hub(ctx)}/restart`),
};

export async function pollJob<T>(
  jobId: string,
  intervalMs = 1500,
): Promise<T> {
  for (;;) {
    const job = await api.getJob(jobId);
    if (job.status === "done") return job.result as T;
    if (job.status === "failed") throw new Error(job.error ?? "job failed");
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

/** RFC 7396 merge-patch producing `edited` when applied to `original`. */
export function computeMergePatch(
  original: Record<string, unknown>,
  edited: Record<string, unknown>,
): Record<string, unknown> {
  const patch: Record<string, unknown> = {};
  for (const key of Object.keys(original)) {
    if (!(key in edited)) patch[key] = null;
  }
  for (const [key, value] of Object.entries(edited)) {
    const prev = original[key];
    if (
      value !== null &&
      typeof value === "object" &&
      !Array.isArray(value) &&
      prev !== null &&
      typeof prev === "object" &&
      !Array.isArray(prev)
    ) {
      const sub = computeMergePatch(
        prev as Record<string, unknown>,
        value as Record<string, unknown>,
      );
      if (Object.keys(sub).length > 0) patch[key] = sub;
    } else if (JSON.stringify(prev) !== JSON.stringify(value)) {
      patch[key] = value;
    }
  }
  return patch;
}
