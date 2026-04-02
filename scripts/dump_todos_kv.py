"""One-off script to dump all todos from the NATS KV bucket.

Run inside the stem pod:
    kubectl exec -it <stem-pod> -- python /app/scripts/dump_todos_kv.py
"""

import asyncio
import json
import os

import nats


async def main():
    url = os.environ.get("NATS_URL", "nats://maki-nerve-nats:4222")
    token = os.environ.get("NATS_TOKEN")

    nc = await nats.connect(url, token=token)
    js = nc.jetstream()

    try:
        kv = await js.key_value("todos")
    except Exception as e:
        print(f"No 'todos' KV bucket found: {e}")
        await nc.close()
        return

    try:
        keys = await kv.keys()
    except Exception:
        print("Bucket exists but has no keys (empty).")
        await nc.close()
        return

    print(f"Found {len(keys)} todo(s):\n")
    for key in sorted(keys):
        entry = await kv.get(key)
        data = json.loads(entry.value.decode())
        title = data.get("title", "?")
        status = data.get("status", "?")
        priority = data.get("priority", "?")
        gh = data.get("github_issue", "none")
        print(f"  [{status}] {priority} | {title} (GH: #{gh})")
        print(f"    Raw: {json.dumps(data)}\n")

    await nc.close()


if __name__ == "__main__":
    asyncio.run(main())
