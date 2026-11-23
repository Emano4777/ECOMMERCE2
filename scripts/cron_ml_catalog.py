"""HostGator-only client. No public host or Vercel URL may be used for this job."""
import json
import os
import urllib.request


def main():
    request = urllib.request.Request(
        "http://127.0.0.1:8002/api/cron/ml-national-catalog", data=b"{}", method="POST",
        headers={"Authorization": "Bearer " + os.environ["ML_WA_CRON_SECRET"],
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=90) as response:
        result = json.load(response)
    print(json.dumps(result))
    if not result.get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
