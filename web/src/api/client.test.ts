import { afterEach, describe, expect, it, vi } from "vitest";
import { pickFolder } from "./client";

afterEach(() => vi.unstubAllGlobals());

describe("pickFolder", () => {
  it("surfaces the server's actionable timeout message", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(JSON.stringify({ nonce: "test" })))
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "The folder picker timed out after 120 seconds. Please try again.",
          }),
          { status: 504 },
        ),
      );
    vi.stubGlobal("fetch", fetch);
    await expect(pickFolder()).rejects.toThrow("timed out after 120 seconds");
    expect(fetch).toHaveBeenLastCalledWith(
      "/api/projects/pick-folder",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ nonce: "test" }),
      }),
    );
  });

  it("handles non-JSON errors from a proxy", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ nonce: "test" })))
        .mockResolvedValueOnce(new Response("Gateway timeout", { status: 504 })),
    );
    await expect(pickFolder()).rejects.toThrow("Folder picker failed (504).");
  });

  it("returns cancellation normally", async () => {
    vi.stubGlobal(
      "fetch",
      vi
        .fn()
        .mockResolvedValueOnce(new Response(JSON.stringify({ nonce: "test" })))
        .mockResolvedValueOnce(
          new Response(JSON.stringify({ cancelled: true, slug: null, source_path: null })),
        ),
    );
    await expect(pickFolder()).resolves.toMatchObject({ cancelled: true });
  });
});
