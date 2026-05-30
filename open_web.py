import os
import threading
import time
import urllib.request
import webbrowser

from app import app


def main():
    port = int(os.environ.get("PORT", "5002"))
    url = f"http://127.0.0.1:{port}/"

    server = threading.Thread(
        target=lambda: app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False),
        daemon=True,
    )
    server.start()

    for _ in range(40):
        try:
            urllib.request.urlopen(url, timeout=1)
            break
        except Exception:
            time.sleep(0.25)

    webbrowser.open(url)

    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
