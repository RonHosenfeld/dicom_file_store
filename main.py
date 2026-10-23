"""Entrypoint: web UI + DICOM receiver in one process."""

import uvicorn

from dicomstore import config as config_mod
from dicomstore.webapp import create_app


def main() -> None:
    cfg = config_mod.load()
    uvicorn.run(create_app(), host="0.0.0.0", port=cfg.web_port, log_level="info")


if __name__ == "__main__":
    main()
