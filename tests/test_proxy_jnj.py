"""Test the JNJ Azure OpenAI proxy server.

Usage:
    # Start the JNJ proxy first in another terminal:
    python main.py proxy-jnj

    # Then run this test:
    python tests/test_proxy_jnj.py
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

PROXY_URL = f"http://localhost:{os.getenv('JNJ_PROXY_PORT', '3457')}"


async def main():
    async with httpx.AsyncClient(timeout=60.0) as client:
        print(f"Testing JNJ proxy at {PROXY_URL}")
        print("=" * 60)

        # --- 1. Health check ---
        print("\n[1] GET /health")
        try:
            resp = await client.get(f"{PROXY_URL}/health")
            data = resp.json()
            print(f"    Status: {resp.status_code}")
            for k, v in data.items():
                print(f"    {k}: {v}")
        except httpx.ConnectError:
            print(f"    ERROR: Cannot connect to {PROXY_URL}")
            print("    Make sure the proxy is running: python main.py proxy-jnj")
            sys.exit(1)

        # --- 2. Test /chat/completions (direct passthrough) ---
        print("\n[2] POST /chat/completions (OpenAI-compatible passthrough)")
        resp = await client.post(f"{PROXY_URL}/chat/completions", json={
            "model": "gpt-4o",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant. Reply in one sentence."},
                {"role": "user", "content": "What is 2+2?"},
            ],
            "temperature": 0.1,
            "max_tokens": 50,
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            print(f"    Response: {content}")
            print(f"    Tokens: prompt={usage.get('prompt_tokens', 0)} "
                  f"completion={usage.get('completion_tokens', 0)} "
                  f"total={usage.get('total_tokens', 0)}")
        else:
            print(f"    Error: {resp.text[:300]}")

        # --- 3. Test /act (Stagehand) ---
        print("\n[3] POST /act (Stagehand act)")
        resp = await client.post(f"{PROXY_URL}/act", json={
            "action": "Click on the login button on a typical website homepage",
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    success:  {data.get('success')}")
            print(f"    selector: {data.get('selector')}")
            print(f"    tokens:   {data.get('tokens')}")
        else:
            print(f"    Error: {resp.text[:300]}")

        # --- 4. Test /observe (Stagehand) ---
        print("\n[4] POST /observe (Stagehand observe)")
        page_html = "\n".join([
            "<nav>",
            '  <a href="/home" id="nav-home">Home</a>',
            '  <a href="/products" class="nav-link">Products</a>',
            '  <button data-testid="cart-btn">Cart (3)</button>',
            "</nav>",
        ])
        resp = await client.post(f"{PROXY_URL}/observe", json={
            "instruction": f"Find the cart button on this page:\n{page_html}",
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            elements = data.get("elements", [])
            print(f"    elements: {len(elements)} found")
            for el in elements:
                print(f"      - selector: {el.get('selector')}  desc: {el.get('description')}")
            print(f"    tokens:   {data.get('tokens')}")
        else:
            print(f"    Error: {resp.text[:300]}")

        # --- 5. Test /extract (Stagehand) ---
        print("\n[5] POST /extract (Stagehand extract)")
        resp = await client.post(f"{PROXY_URL}/extract", json={
            "instruction": "Extract the product name and price:\n<div><h2>Wireless Keyboard</h2><span class='price'>$49.99</span></div>",
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    text:   {data.get('text')}")
            print(f"    value:  {data.get('value')}")
            print(f"    data:   {data.get('data')}")
            print(f"    tokens: {data.get('tokens')}")
        else:
            print(f"    Error: {resp.text[:300]}")

        # --- 6. Health reset ---
        print("\n[6] POST /health/reset")
        resp = await client.post(f"{PROXY_URL}/health/reset")
        print(f"    Status: {resp.status_code} — {resp.json().get('message')}")

        # --- 7. Validation ---
        print("\n[7] POST /act (empty — should fail 422)")
        resp = await client.post(f"{PROXY_URL}/act", json={"action": ""})
        print(f"    Status: {resp.status_code} ({'OK — validation works' if resp.status_code == 422 else 'UNEXPECTED'})")

        print("\n" + "=" * 60)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
