"""Entry point: python run.py [--config config.json] [--host H] [--port P]"""

import argparse
import logging
import os


def main():
    parser = argparse.ArgumentParser(description="Unified Mail Aggregator")
    parser.add_argument("--config", default=os.environ.get("MAILAGG_CONFIG", "config.json"))
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S")

    os.environ["MAILAGG_CONFIG"] = args.config

    # Imported after MAILAGG_CONFIG is set because app.main reads it.
    import uvicorn
    from app.config import ConfigStore

    store = ConfigStore(args.config)
    host = args.host or store.get("server", "host", default="127.0.0.1")
    port = args.port or int(store.get("server", "port", default=8000))

    if host not in ("127.0.0.1", "localhost", "::1") and not store.get(
            "server", "dashboard_password", default=""):
        logging.getLogger("run").warning(
            "Dashboard is reachable from the network without a password. "
            "Set server.dashboard_password in %s.", args.config)

    logging.getLogger("run").info("Dashboard: http://%s:%s", host, port)
    uvicorn.run("app.main:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
