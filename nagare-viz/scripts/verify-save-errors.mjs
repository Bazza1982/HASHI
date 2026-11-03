import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";
import { chromium } from "@playwright/test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const artifacts = process.env.NAGARE_UI_ARTIFACT_DIR;
let server;
let browser;
let page;
const deadline = setTimeout(() => {
  console.error("Save-error UI verification exceeded its deadline.");
  process.exit(1);
}, 90000);
deadline.unref();

try {
  process.chdir(root);
  server = await createServer({
    root,
    configFile: path.join(root, "vite.config.ts"),
    server: { host: "127.0.0.1", port: 0, strictPort: false, open: false },
  });
  await server.listen();
  const address = server.httpServer.address();
  assert.ok(address && typeof address === "object");
  const source = fs.readFileSync(path.join(root, "../tests/fixtures/unknown_fields_workflow.yaml"), "utf8");
  browser = await chromium.launch({ headless: true });
  page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  page.setDefaultTimeout(12000);
  const errors = [];
  const writes = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("request", (request) => {
    if (request.method() === "POST" && new URL(request.url()).pathname === "/api/workflows/write") {
      writes.push(JSON.parse(request.postData()));
    }
  });
  const names = ["fixture.yaml", "fixture-two.yaml", "fixture-three.yaml"];
  await page.route("**/api/workflows", (route) => route.fulfill({
    json: { entries: names.map((name) => ({ name, path: name, type: "file" })) },
  }));
  await page.route("**/api/workflows/read?*", (route) => route.fulfill({ json: { content: source } }));
  await page.goto("http://127.0.0.1:" + address.port, { waitUntil: "domcontentloaded" });
  await page.getByRole("button", { name: "fixture.yaml", exact: false }).click();
  const rawMode = page.getByRole("button", { name: "Raw YAML", exact: true });
  if (await rawMode.isVisible()) await rawMode.click();
  const editor = page.locator("textarea.yaml-editor");
  await editor.fill(source + "\n# save-error fixture edit\n");
  const denied = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/workflows/write");
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const rejection = await denied;
  assert.equal(rejection.status(), 403);
  assert.equal((await rejection.json()).error, "origin denied");
  await page.getByRole("alert").filter({ hasText: "origin denied" }).waitFor({ state: "visible" });
  await page.getByText("Unsaved changes", { exact: true }).waitFor({ state: "visible" });
  assert.equal(await page.getByRole("button", { name: "Save", exact: true }).isEnabled(), true);
  assert.equal(writes.length, 1);

  await page.getByRole("button", { name: "fixture-two.yaml", exact: false }).click();
  await page.getByText("Clean draft", { exact: true }).waitFor({ state: "visible" });
  assert.equal(await page.getByRole("alert").count(), 0);
  if (await rawMode.isVisible()) await rawMode.click();
  await editor.fill(source + "\n# second document edit\n");
  let heldRequest;
  let markHeld;
  const held = new Promise((resolve) => { markHeld = resolve; });
  await page.route("**/api/workflows/write", (route) => {
    heldRequest = route;
    markHeld();
  });
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await held;
  await page.getByRole("button", { name: "fixture-three.yaml", exact: false }).click();
  await page.getByText("Clean draft", { exact: true }).waitFor({ state: "visible" });
  if (await rawMode.isVisible()) await rawMode.click();
  const newEdits = source + "\n# keep these unsaved third-document edits\n";
  await editor.fill(newEdits);
  const acknowledgment = page.waitForResponse((response) => new URL(response.url()).pathname === "/api/workflows/write");
  await heldRequest.fulfill({ json: { ok: true, path: "fixture-two.yaml" } });
  await (await acknowledgment).finished();
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await editor.inputValue(), newEdits);
  assert.equal(await page.getByRole("button", { name: "Save", exact: true }).isEnabled(), true);
  await page.getByText("Unsaved changes", { exact: true }).waitFor({ state: "visible" });
  assert.equal(await page.getByRole("alert").count(), 0);
  assert.equal(writes.length, 2);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({
    passed: true,
    visible_server_reason: true,
    rejected_save_keeps_edits: true,
    previous_document_error_hidden: true,
    late_save_cannot_clear_other_document_edits: true,
  }));
} catch (error) {
  if (artifacts && page) {
    fs.mkdirSync(artifacts, { recursive: true });
    await page.screenshot({ path: path.join(artifacts, "save-error-failure.png") }).catch(() => {});
  }
  console.error(error);
  process.exitCode = 1;
} finally {
  clearTimeout(deadline);
  await browser?.close();
  await server?.close();
}
