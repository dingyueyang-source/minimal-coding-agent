"""Single-process, single-worker, loopback-only launcher (no --reload)."""
import logging

import uvicorn

from app.config import load_dotenv, server_bind


def main():
    load_dotenv()
    host, port = server_bind()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, workers=1, reload=False,
                access_log=False, proxy_headers=False, server_header=False)


if __name__ == "__main__":
    main()
