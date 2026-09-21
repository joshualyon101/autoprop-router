from pathlib import Path


ROOT = Path(__file__).resolve().parent


def test_deployment_forces_one_worker_and_redacts_token_bearing_access_paths():
    railway = (ROOT / "railway.toml").read_text(encoding="utf-8")
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    for launch_config in (railway, dockerfile):
        assert "--workers 1" in launch_config
        # Authentication is carried in the webhook/admin path for TradingView
        # compatibility, so Uvicorn access logs must not echo those paths.
        assert "--no-access-log" in launch_config
