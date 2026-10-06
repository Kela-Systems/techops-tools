# Vendored protos

Copied verbatim — do not edit here; re-copy from the source and regenerate.

| Tree | Source | Revision |
| --- | --- | --- |
| `kela/**`, `buf/validate/validate.proto` | `kela` repo, `proto/` | `d564152309` (2026-10-05) |
| `grpc_health/v1/health.proto` | [grpc/grpc-proto](https://github.com/grpc/grpc-proto/blob/master/grpc/health/v1/health.proto) | upstream, Apache-2.0 |

The health proto sits under `grpc_health/` rather than upstream's `grpc/` so
the generated Python package cannot shadow the real `grpc` library; its proto
package (and so the wire service name `grpc.health.v1.Health`) is unchanged.

The `kela/**` set is the import closure of the files the ATP reads:

- `kela/system/v1alpha1/system.proto` — `SystemService.GetStatus` (S3.6, S4.1)
- `kela/asset/v1alpha1/asset_service.proto` — `AssetService.ListAssets` (S6.1, S6.2)
- `kela/media/v1alpha1/video.proto` — `VideoService.WatchStreamHealth` (S6.6)
- `kela/device/v1alpha1/device.proto` — `DeviceService.ListDevices`, setup_info, calibrations (S5)
- `kela/types/v1alpha1/types.proto` — `SensorToAssetCalibration` (S5.10)
- `kela/ext/gotcha/v1alpha1/gotcha.proto` — `ComponentHealth` (S6.2)
- `kela/config/v1alpha1/config.proto` (+ `options.proto`) — `ConfigService.GetConfig` (S5.8)
- `kela/map/v1alpha1/map_config.proto` — `hub.map` → `initial_position` (S5.8)
- `kela/projection/v1alpha1/projection_config.proto` — `hub.projection` → `dem_radius_km` (S5.8)
- `kela/video_analytics/v1alpha1/video_analytics.proto` — `ListAvailableModels.default_model`, `ListConfigs` (S4.11)

Everything else in the directory is only here because one of those imports it.
`google/protobuf/*` comes from `grpcio-tools` and is not vendored.

To refresh against a new hub release (pinned in `release.yaml` → `hub.image_tag`):

```bash
cd ~/dev/kela && git checkout <release tag>
# re-copy the files listed above (same paths), update the revision in this table, then:
cd techops-tools/gotcha-atp && uv run python scripts/gen_protos.py
```
