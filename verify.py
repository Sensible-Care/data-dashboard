"""Smoke test: proves every Graph building block works against your real drive."""
import io
import os
import sys

import config
from drive_writer import DriveWriter, budget_path, sanitize
from graph_client import GraphClient, GraphError, StaticTokenAuth

BASE = "_SmokeTest"
passed = failed = 0


def check(label, fn):
    global passed, failed
    try:
        result = fn()
        print("  \033[32mPASS\033[0m  {}{}".format(label, " -> " + result if result else ""))
        passed += 1
    except Exception as exc:
        print("  \033[31mFAIL\033[0m  {}\n          {}".format(label, exc))
        failed += 1


def main():
    client = GraphClient(StaticTokenAuth())
    writer = DriveWriter(client)

    print("\n1. Token and drive")
    def whoami():
        resp = client.request("GET", config.DRIVE_ROOT)
        GraphClient.raise_for_graph(resp)
        d = resp.json()
        free = d["quota"]["remaining"] / (1024.0 ** 3)
        return "{} ({}), {:.2f} GB free".format(d["name"], d["driveType"], free)
    check("token is valid", whoami)

    print("\n2. Folder creation")
    check("create nested path", lambda: writer.ensure_folder(BASE))
    check("re-create is idempotent (409 swallowed)",
          lambda: DriveWriter(client).ensure_folder(BASE))

    print("\n3. Small upload")
    def small():
        item = writer.upload(BASE, "probe.txt", b"hello from the pipeline")
        return "{} ({} bytes)".format(item["name"], item["size"])
    check("PUT /content", small)

    print("\n4. Chunked upload (upload session)")
    def large():
        size = config.CHUNK_SIZE * 2 + 1024
        payload = io.BytesIO(os.urandom(size))
        writer_big = DriveWriter(client)
        writer_big.ensure_folder(BASE)
        item = writer_big._upload_session(BASE, "probe-large.bin", payload, size)
        return "{} ({} bytes, {} chunks)".format(
            item.get("name", "?"), size, -(-size // config.CHUNK_SIZE))
    check("createUploadSession + ranges", large)

    print("\n5. Listing")
    def listing():
        kids = writer.list_children(BASE)
        return "{} items: {}".format(len(kids), ", ".join(k["name"] for k in kids))
    check("list children", listing)

    print("\n6. Error handling")
    def missing_parent():
        # KNOWN DIVERGENCE: OneDrive personal silently creates missing parents
        # here; SharePoint returns 404 itemNotFound. Never rely on either --
        # always call ensure_folder() first, so the code works on both.
        resp = client.request("PUT", "{}/root:/_SmokeTest/nope/missing/x.txt:/content".format(
            config.DRIVE_ROOT), data=b"x")
        if resp.status_code == 404:
            return "404 (SharePoint behaviour)"
        if resp.status_code in (200, 201):
            return "auto-created (OneDrive personal behaviour) -- ensure_folder still required"
        raise AssertionError("unexpected status {}".format(resp.status_code))
    check("implicit parent creation is NOT relied upon", missing_parent)

    print("\n7. Pure functions (no network)")
    def san():
        got = sanitize('+1555 <Sales>: "John/Doe"?')
        assert "/" not in got and ":" not in got and "?" not in got, got
        assert sanitize("CON") == "_CON", sanitize("CON")
        assert sanitize("   ") == "untitled"
        return got
    check("sanitize strips invalid chars", san)

    def budget():
        long_name = "x" * 500 + ".mp3"
        got = budget_path(BASE, long_name)
        assert got.endswith(".mp3"), got
        assert len(BASE) + 1 + len(got) <= config.MAX_PATH - config.PATH_PREFIX_RESERVE
        return "truncated to {} chars, extension kept".format(len(got))
    check("budget_path respects 400-char limit", budget)

    def chunk_alignment():
        assert config.CHUNK_SIZE % 327680 == 0, config.CHUNK_SIZE
        return "{} bytes = {} x 320 KiB".format(config.CHUNK_SIZE, config.CHUNK_SIZE // 327680)
    check("chunk size is a multiple of 320 KiB", chunk_alignment)

    print("\n{} passed, {} failed\n".format(passed, failed))
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GraphError as exc:
        if exc.status == 401:
            print("\n\033[31mToken expired.\033[0m Grab a fresh one from Graph Explorer "
                  "(Access token tab) and update GRAPH_DEV_TOKEN in .env\n")
            sys.exit(2)
        raise
