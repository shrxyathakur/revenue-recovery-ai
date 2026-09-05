#!/usr/bin/env python3
"""
run_pipeline.py

Single entry point for the Revenue Recovery AI pipeline.

Starts the Downtime API mock server, optionally seeds demo downtime data
and/or generates synthetic test data, then runs all six pipeline stages
in order:

    stage0_hard_decline -> stage1_detection -> stage2_diagnosis ->
    stage3_recovery -> stage4_guardrails -> stage5_audit_finalization

Usage:
    python run_pipeline.py
    python run_pipeline.py --generate-data
    python run_pipeline.py --seed-downtime
    python run_pipeline.py --generate-data --seed-downtime
    python run_pipeline.py --skip-mock-server
    python run_pipeline.py --stop-on-failure=false
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
PIPELINE_DIR = REPO_ROOT / "pipeline"
MOCK_SERVER_SCRIPT = REPO_ROOT / "mock-server" / "downtime_mock_server.py"
DATA_GEN_SCRIPT = REPO_ROOT / "data" / "synthetic_data_generator.py"
SEED_DOWNTIME_SCRIPT = REPO_ROOT / "demo_seed_downtime.py"

STAGES = [
    "stage0_hard_decline.py",
    "stage1_detection.py",
    "stage2_diagnosis.py",
    "stage3_recovery.py",
    "stage4_guardrails.py",
    "stage5_audit_finalization.py",
]

MOCK_SERVER_STARTUP_WAIT_SECONDS = 2


def run_step(label: str, script_path: Path) -> int:
    print(f"\n{'=' * 60}")
    print(f"  {label}")
    print(f"{'=' * 60}")
    if not script_path.exists():
        print(f"  [SKIP] {script_path} not found.")
        return 0
    result = subprocess.run([sys.executable, str(script_path)], cwd=REPO_ROOT)
    return result.returncode


def start_mock_server() -> subprocess.Popen | None:
    if not MOCK_SERVER_SCRIPT.exists():
        print(f"[WARN] Mock server script not found at {MOCK_SERVER_SCRIPT}; skipping.")
        return None
    print(f"\nStarting Downtime API mock server ({MOCK_SERVER_SCRIPT.name})...")
    proc = subprocess.Popen([sys.executable, str(MOCK_SERVER_SCRIPT)], cwd=REPO_ROOT)
    time.sleep(MOCK_SERVER_STARTUP_WAIT_SECONDS)
    if proc.poll() is not None:
        print("[WARN] Mock server exited immediately — check for a startup error above.")
        return None
    print(f"Mock server running (pid={proc.pid}).")
    return proc


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full revenue-recovery pipeline.")
    parser.add_argument(
        "--generate-data", action="store_true",
        help="Run data/synthetic_data_generator.py before the pipeline stages.",
    )
    parser.add_argument(
        "--seed-downtime", action="store_true",
        help="Run demo_seed_downtime.py before the pipeline stages (after the mock server starts).",
    )
    parser.add_argument(
        "--skip-mock-server", action="store_true",
        help="Don't start the Downtime API mock server (assume it's already running elsewhere).",
    )
    parser.add_argument(
        "--stop-on-failure", type=lambda v: v.lower() != "false", default=True,
        help="Stop the run if a stage exits non-zero. Pass --stop-on-failure=false to continue anyway.",
    )
    args = parser.parse_args()

    mock_server_proc = None
    try:
        if not args.skip_mock_server:
            mock_server_proc = start_mock_server()

        if args.generate_data:
            rc = run_step("Generating synthetic data", DATA_GEN_SCRIPT)
            if rc != 0 and args.stop_on_failure:
                print(f"\n[FAIL] Data generation exited with code {rc}. Stopping.")
                return rc

        if args.seed_downtime:
            rc = run_step("Seeding demo downtime event", SEED_DOWNTIME_SCRIPT)
            if rc != 0 and args.stop_on_failure:
                print(f"\n[FAIL] Downtime seeding exited with code {rc}. Stopping.")
                return rc

        for stage_file in STAGES:
            stage_path = PIPELINE_DIR / stage_file
            rc = run_step(stage_file, stage_path)
            if rc != 0:
                print(f"\n[FAIL] {stage_file} exited with code {rc}.")
                if args.stop_on_failure:
                    return rc
                print("Continuing anyway (--stop-on-failure=false).")

        print(f"\n{'=' * 60}")
        print("  Pipeline complete. See audit_log.jsonl for the full trail.")
        print(f"{'=' * 60}")
        return 0

    finally:
        if mock_server_proc is not None and mock_server_proc.poll() is None:
            print(f"\nStopping mock server (pid={mock_server_proc.pid})...")
            mock_server_proc.terminate()
            try:
                mock_server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                mock_server_proc.kill()


if __name__ == "__main__":
    sys.exit(main())
