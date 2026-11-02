import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchRunSnapshot, saveWorkflowFile } from "./nagareApi";

afterEach(() => vi.unstubAllGlobals());

describe("workflow save API failures", () => {
  it.each([
    [403, { error: "workflow path denied" }, "workflow path denied"],
    [500, { message: "storage is unavailable" }, "storage is unavailable"],
    [403, { error: "denied", message: "origin denied" }, "origin denied"],
  ])("keeps the reason in a %s response", async (status, payload, reason) => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }),
    );
    vi.stubGlobal("fetch", fetcher);
    await expect(saveWorkflowFile("fixture.yaml", "workflow: {}")).rejects.toThrow(reason);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["<html>private proxy body</html>", "Save failed: 502"],
    [JSON.stringify({ error: { private: "do not display" } }), "Save failed: 502"],
    [JSON.stringify({ message: "", error: "" }), "Save failed: 502"],
  ])("uses the status if no readable reason exists", async (body, reason) => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 502 })));
    await expect(saveWorkflowFile("fixture.yaml", "workflow: {}")).rejects.toThrow(reason);
  });

  it("preserves the path and contents and completes only after the save responds", async () => {
    const fetcher = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true, path: "fixture.yaml" }), { status: 200 }),
    );
    vi.stubGlobal("fetch", fetcher);
    await saveWorkflowFile("fixture.yaml", "workflow: {}");
    const [url, request] = fetcher.mock.calls[0];
    expect(url).toBe("/api/workflows/write");
    expect(JSON.parse(request.body)).toEqual({ path: "fixture.yaml", content: "workflow: {}" });
  });

  it("keeps a lost save response unconfirmed without echoing transport data or retrying", async () => {
    const fetcher = vi.fn().mockRejectedValue(new TypeError("private-url?token=private-secret"));
    vi.stubGlobal("fetch", fetcher);
    await expect(saveWorkflowFile("fixture.yaml", "workflow: {}")).rejects.toMatchObject({
      message: "Save result is unconfirmed; check the file before saving again.",
    });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each(["<html>proxy page</html>", JSON.stringify({ ok: false }), "{}"])(
    "requires a positive acknowledgment before declaring a save complete", async (body) => {
      vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
      await expect(saveWorkflowFile("fixture.yaml", "workflow: {}")).rejects.toThrow("Save result is unconfirmed");
    },
  );

  it("uses the same readable reason rules for existing run reads", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ message: "run not found" }), { status: 404 }),
    ));
    await expect(fetchRunSnapshot("/api/nagare", "missing")).rejects.toThrow("run not found");
  });
});
