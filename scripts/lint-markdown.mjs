import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { load } from "js-yaml";
import { lint, readConfig } from "markdownlint/promise";

// Git includes new files and respects .gitignore without a glob dependency.
const excluded = new Set(["node_modules", "collections", ".venv", ".claude", "references"]);
const files = [...new Set(execFileSync("git", [
  "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.md"
], { encoding: "utf8" }).split("\0").filter((file) =>
  file && existsSync(file) && !file.split("/").some((part) => excluded.has(part))
))].sort();

const results = await lint({
  files,
  config: await readConfig(".markdownlint.yml", [load])
});
let count = 0;
for (const [file, errors] of Object.entries(results)) {
  for (const error of errors) {
    count++;
    const column = error.errorRange ? `:${error.errorRange[0]}` : "";
    const detail = error.errorDetail ? ` [${error.errorDetail}]` : "";
    console.error(`${file}:${error.lineNumber}${column} ${error.ruleNames.join("/")} ${error.ruleDescription}${detail}`);
  }
}
console.log(`Markdown: ${files.length} files, ${count} errors`);
process.exitCode = count > 0 ? 1 : 0;
