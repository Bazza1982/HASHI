import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { resolveWorkflowFile } from "./fileServerPlugin";

const temporaryRoots: string[] = [];

function makeRoot(): string {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), "nagare-viz-path-"));
  temporaryRoots.push(root);
  return fs.realpathSync(root);
}

afterEach(() => {
  for (const root of temporaryRoots.splice(0)) {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

describe("workflow file resolution", () => {
  it("allows YAML children and rejects traversal or non-YAML files", () => {
    const root = makeRoot();
    fs.writeFileSync(path.join(root, "workflow.yaml"), "workflow: {}\n", "utf8");

    expect(resolveWorkflowFile("workflow.yaml", false, root)).toBe(
      path.join(root, "workflow.yaml"),
    );
    expect(resolveWorkflowFile("../outside.yaml", true, root)).toBeNull();
    expect(resolveWorkflowFile("notes.txt", true, root)).toBeNull();
  });

  it.runIf(process.platform !== "win32")(
    "rejects writes through a YAML symlink that targets outside the root",
    () => {
      const root = makeRoot();
      const outside = path.join(path.dirname(root), `${path.basename(root)}-outside.yaml`);
      temporaryRoots.push(outside);
      fs.writeFileSync(outside, "outside: true\n", "utf8");
      fs.symlinkSync(outside, path.join(root, "linked.yaml"));

      expect(resolveWorkflowFile("linked.yaml", true, root)).toBeNull();
    },
  );
});
