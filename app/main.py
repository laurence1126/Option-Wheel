import sys
from pathlib import Path

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

import os
from app import create_app

app = create_app()


if __name__ == "__main__":
    host = os.environ.get("OPTION_WHEEL_APP_HOST", "0.0.0.0")
    port = int(os.environ.get("OPTION_WHEEL_APP_PORT", "5001"))
    app.run(host=host, port=port)
