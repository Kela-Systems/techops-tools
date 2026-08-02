export interface Manifest {
  manifest_id: string;
  name: string;
  version: string;
  description: string;
}

export interface Integration {
  id: string;
  name: string;
  manifest_id: string | null;
  device_count: number;
  device_setup_info_schema: Record<string, unknown> | null;
}

export interface Device {
  id: string;
  name: string;
  integration_id: string;
  setup_info: Record<string, unknown>;
}

export interface Asset {
  id: string;
  name: string;
  asset_type: string;
  sensors: string[];
}

export interface EntityLink {
  source_id: string;
  target_id: string;
  source_name: string;
  target_name: string;
  link_type: string;
}

export interface ProfileIntegration {
  name: string;
  manifest_id: string | null;
  manifest_name: string | null;
  has_config: boolean;
  device_count: number;
}

export interface ProfileSummary {
  source_context: string;
  integrations: ProfileIntegration[];
  site_config_keys: string[];
}

export interface AppliedIntegration {
  name: string;
  manifest_name: string | null;
  integration_id: string | null;
  manifest_id: string | null;
  matched_by_name: boolean;
  devices: [string, string][];
  dropped: Record<string, string[]>;
  error: string | null;
}

export interface ProfileApplyReport {
  target_context: string;
  applied: AppliedIntegration[];
  site_config_applied: boolean;
}

export interface SavedProfile {
  file_name: string;
  source_context?: string;
  exported_at?: string | null;
  integration_count?: number;
  device_count?: number;
  has_site_config?: boolean;
  error?: string;
}

export interface SavedDeviceConfig {
  file_name: string;
  sections?: Record<string, number>;
  error?: string;
}

export interface AppliedConfigSection {
  section: string;
  integration_name?: string;
  integration_id?: string;
  created?: { name: string; device_id: string }[];
  dropped?: Record<string, string[]>;
  error?: string;
}

export interface DeviceConfigApplyReport {
  target_context: string;
  applied: AppliedConfigSection[];
}

export interface Job {
  id: string;
  kind: string;
  status: "running" | "done" | "failed";
  result: unknown;
  error: string | null;
}

export interface ImportResult {
  created: { name: string; device_id: string }[];
  dropped: Record<string, string[]>;
}
