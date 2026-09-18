"""ChatGPT login window: opens the persistent profile and keeps the window open
until the user closes it (or 20 minutes). No login detection, no typing."""
import sys, time
sys.path.insert(0, ".")
from v2r.warehouse.gpt_images import open_gpt
pw, ctx, page = open_gpt(headless=False)
print("window open; log in, then close the window when done", flush=True)
deadline = time.time() + 20 * 60
try:
    while time.time() < deadline:
        if not ctx.pages:
            break
        time.sleep(2)
finally:
    try:
        ctx.close()
    except Exception:
        pass
    pw.stop()
print("done", flush=True)
