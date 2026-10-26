import asyncio
import logging
import os
from pathlib import Path
import re
import sys

import uvicorn
from dotenv import load_dotenv


class RuntimeTokenFilter(logging.Filter):
    def filter(self, record):
        record.msg = re.sub(r'([?&]token=)[^&\s"]+', r'\1[REDACTED]', record.getMessage())
        record.args = ()
        return True


def compiler_loop():
    # Windows compiler subprocesses require a Proactor loop.
    if sys.platform == 'win32':
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def main():
    load_dotenv(Path(__file__).resolve().parents[1] / '.env')
    # WebSocket connection messages use uvicorn.error, not uvicorn.access.
    logging.getLogger('uvicorn.error').addFilter(RuntimeTokenFilter())
    windows = sys.platform == 'win32'
    # Watchfiles uses a Selector loop on Windows, so reload must remain disabled.
    # Hosted deployments set WIREUP_RELOAD=0 so the process never restarts mid-compile.
    reload = not windows and os.getenv('WIREUP_RELOAD', 'true').lower() not in ('0', 'false', 'no')
    uvicorn.run('backend.app:app', host='127.0.0.1', port=int(os.getenv('BACKEND_PORT', '8000')),
                loop='backend.run:compiler_loop' if windows else 'auto', reload=reload,
                reload_dirs=None if not reload else ['backend'], access_log=False)


if __name__ == '__main__':
    main()
