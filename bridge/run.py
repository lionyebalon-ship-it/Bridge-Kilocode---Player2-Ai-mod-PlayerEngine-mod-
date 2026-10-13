"""Entry point for the p2k-bridge HTTP server."""

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server
from p2k_bridge.config import load_config
from p2k_bridge.kilo import KiloClient, KiloError

LOG = logging.getLogger("p2k.bridge")
TARGET_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="p2k-bridge",
        description="Local Player2 -> Kilo Gateway bridge for Minecraft NPC mods.",
    )
    parser.add_argument(
        "--config",
        default=str(Path(__file__).resolve().parent / "config.json"),
        help="path to the bridge config.json (default: bridge/config.json)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check the Kilo Gateway model catalog and exit",
    )
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("p2k.access").setLevel(logging.WARNING)


def run_check(config_path: Path) -> int:
    cfg = load_config(str(config_path))
    cfg.config_path = str(config_path.resolve())
    client = KiloClient(cfg)
    try:
        catalog = client.models()
    except KiloError as exc:
        print(f"error: {exc}")
        return 1
    raw_models = catalog.get("data", []) if isinstance(catalog, dict) else []
    ids = [
        model.get("id")
        for model in raw_models
        if isinstance(model, dict) and isinstance(model.get("id"), str)
    ]
    nemotron = [model_id for model_id in ids if "nemotron" in model_id.lower()]
    present = TARGET_MODEL in ids
    print(f"kilo gateway models: {len(ids)} ids returned")
    print(f"target model {'PRESENT' if present else 'MISSING'}: {TARGET_MODEL}")
    if nemotron:
        print("nemotron ids: " + ", ".join(nemotron[:5]))
    else:
        print("nemotron ids: none listed")
    if present:
        print("PASS: target model available")
        return 0
    print(f"FAIL: {TARGET_MODEL} not found in the gateway catalog")
    return 1


def _banner(cfg, servers) -> None:
    labels = {label: srv for label, srv in servers}
    key_configured = bool((getattr(cfg, "kilo_api_key", "") or "").strip())

    def state(label: str) -> str:
        return "active" if label in labels else "not started"

    lines = [
        f"{server.SERVICE_NAME} {server.SERVICE_VERSION}",
        f"  plain HTTP : http://{cfg.listen_host}:{cfg.http_port} [{state('main-http')}]",
        f"  login shim : http://127.0.0.1:{cfg.local_login_port} [{state('login-shim')}]",
        f"  TLS        : https://0.0.0.0:{cfg.tls_port} (servername api.player2.game) [{state('tls')}]",
        f"  model      : {cfg.model}",
        f"  quota      : {cfg.requests_per_hour} requests/hour (local token bucket)",
        f"  kilo key   : {'configured' if key_configured else 'not configured (free tier)'}",
    ]
    print("\n".join(lines), flush=True)


def run_server(config_path: Path) -> int:
    cfg = load_config(str(config_path))
    cfg.config_path = str(config_path.resolve())
    server.init(cfg)
    try:
        servers = server.start_all(cfg)
    except OSError as exc:
        print(
            f"error: main listener could not start on {cfg.listen_host}:{cfg.http_port}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    _banner(cfg, servers)

    stop = threading.Event()

    def shutdown(signum=None, frame=None) -> None:
        for _label, srv in servers:
            try:
                srv.shutdown()
            except Exception:
                pass
        stop.set()

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while not stop.is_set():
        stop.wait(1.0)
    LOG.info("p2k-bridge stopped")
    return 0


def main(argv=None) -> int:
    configure_logging()
    args = parse_args(argv)
    if args.check:
        return run_check(Path(args.config))
    return run_server(Path(args.config))


if __name__ == "__main__":
    sys.exit(main())
