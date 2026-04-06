"""Test the LLM proxy server against the Globant GeAI endpoint.

Usage:
    # Start the proxy first in another terminal:
    python main.py proxy

    # Then run this test:
    python tests/test_proxy.py
"""

import asyncio
import os
import sys

import httpx
from dotenv import load_dotenv

load_dotenv()

PROXY_URL = os.getenv("STAGEHAND_SERVER_URL", "http://localhost:3456")


async def main():
    async with httpx.AsyncClient(timeout=30.0) as client:
        print(f"Testing proxy at {PROXY_URL}")
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
            print("    Make sure the proxy is running: python main.py proxy")
            sys.exit(1)

        # --- 2. Test /act ---
        print("\n[2] POST /act")
        resp = await client.post(f"{PROXY_URL}/act", json={
            "action": "Click on the login button on a typical website homepage",
            "modelName": None,
            "modelProvider": None,
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    success:  {data.get('success')}")
            print(f"    selector: {data.get('selector')}")
            print(f"    tokens:   {data.get('tokens')}")
        else:
            print(f"    Error: {resp.text[:200]}")

        # --- 3. Test /observe ---
        print("\n[3] POST /observe")
        resp = await client.post(f"{PROXY_URL}/observe", json={
            "instruction": "Find all navigation links on this page:\n<nav><a href='/home'>Home</a><a href='/about'>About</a><a href='/contact'>Contact</a></nav>",
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
            print(f"    Error: {resp.text[:200]}")

        # --- 4. Test /extract ---
        print("\n[4] POST /extract")
        resp = await client.post(f"{PROXY_URL}/extract", json={
            "instruction": "Extract the product name and price from this HTML:\n<div class='product'><h2>Wireless Keyboard</h2><span class='price'>$49.99</span></div>",
        })
        print(f"    Status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            print(f"    text:   {data.get('text')}")
            print(f"    value:  {data.get('value')}")
            print(f"    data:   {data.get('data')}")
            print(f"    tokens: {data.get('tokens')}")
        else:
            print(f"    Error: {resp.text[:200]}")

        # --- 5. Test validation (empty action) ---
        print("\n[5] POST /act (empty — should fail 422)")
        resp = await client.post(f"{PROXY_URL}/act", json={"action": ""})
        print(f"    Status: {resp.status_code} ({'OK — validation works' if resp.status_code == 422 else 'UNEXPECTED'})")

        print("\n" + "=" * 60)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
