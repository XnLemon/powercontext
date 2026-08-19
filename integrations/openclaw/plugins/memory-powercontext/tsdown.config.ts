import { defineConfig } from "tsdown";

export default defineConfig({
  entry: "index.ts",
  format: ["esm"],
  dts: false,
  fixedExtension: false,
  outDir: "dist",
  clean: true,
  external: [/^openclaw\//u, "typebox"],
});
