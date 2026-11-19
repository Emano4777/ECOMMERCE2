"""Local HostGator timer client. Secret supplied by systemd EnvironmentFile."""
import json
import os
import urllib.request


def main():
    request = urllib.request.Request(
        os.environ.get("ML_WA_BASE_URL", "http://127.0.0.1:8002") + "/api/cron/ml-whatsapp-sao-pedro",
        data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + os.environ["ML_WA_CRON_SECRET"], "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=240) as response:
        result = json.load(response)
    print(json.dumps(result))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
