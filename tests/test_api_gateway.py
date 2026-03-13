"""Quick smoke test for the local API gateway."""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:18801"


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=5) as r:
        return json.loads(r.read())


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def main():
    print("── Health check ─────────────────────────")
    try:
        h = get("/health")
        print(f"  status : {h['status']}")
        print(f"  engines: {h['engines']}")
    except Exception as e:
        print(f"  FAILED: {e}")
        print("  Is the bridge running?")
        return

    print("\n── Models ───────────────────────────────")
    models = get("/v1/models")
    for m in models["data"]:
        print(f"  {m['id']}  ({m['owned_by']})")

    print("\n── Chat completion (gemini-3.1-pro-preview) ─")
    try:
        resp = post("/v1/chat/completions", {
            "model": "gemini-3.1-pro-preview",
            "messages": [{"role": "user", "content": "Say hello in exactly one sentence."}],
        })
        choice = resp["choices"][0]["message"]["content"]
        usage = resp.get("usage", {})
        print(f"  response : {choice}")
        print(f"  tokens   : {usage}")
    except Exception as e:
        print(f"  FAILED: {e}")

    print("\n── Session cache test ───────────────────")
    try:
        r1 = post("/v1/chat/completions", {
            "model": "gemini-3.1-pro-preview",
            "messages": [{"role": "user", "content": "My favourite colour is blue. Remember it."}],
            "extra_body": {"session_id": "test-session-1"},
        })
        print(f"  turn 1: {r1['choices'][0]['message']['content'][:80]}")

        r2 = post("/v1/chat/completions", {
            "model": "gemini-3.1-pro-preview",
            "messages": [{"role": "user", "content": "What is my favourite colour?"}],
            "extra_body": {"session_id": "test-session-1"},
        })
        print(f"  turn 2: {r2['choices'][0]['message']['content'][:80]}")
    except Exception as e:
        print(f"  FAILED: {e}")

    print("\n── Done ─────────────────────────────────")


if __name__ == "__main__":
    main()
