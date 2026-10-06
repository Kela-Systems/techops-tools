#!/usr/bin/env python3
"""Regenerate the hub gRPC stubs from the vendored proto/ into src/gotcha_atp/_pb/.

    uv run python scripts/gen_protos.py

The tool also does this by itself on first use (and whenever a .proto is
newer than the stubs), so this is only needed to check a freshly vendored set.
"""
import sys

from gotcha_atp.access.grpc import ensure_stubs, pb

if __name__ == "__main__":
    out = ensure_stubs(force=True)
    for mod in ("kela.system.v1alpha1.system_pb2_grpc", "kela.asset.v1alpha1.asset_service_pb2_grpc",
                "kela.media.v1alpha1.video_pb2_grpc", "kela.device.v1alpha1.device_pb2_grpc",
                "kela.ext.gotcha.v1alpha1.gotcha_pb2", "grpc_health.v1.health_pb2_grpc",
                "kela.video_analytics.v1alpha1.video_analytics_pb2_grpc"):
        pb(mod)
    print(f"stubs generated and importable: {out}")
    sys.exit(0)
