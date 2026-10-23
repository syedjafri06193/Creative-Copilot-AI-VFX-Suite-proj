// Packages the built UI (dist/) as a standalone app for this OS in release/. On Windows it also
// builds a Squirrel installer in release/installer/. Signing is optional, from the environment:
//   Windows: SENSEI_WIN_CERT_FILE (a .pfx) and SENSEI_WIN_CERT_PASSWORD sign the app and the installer.
//   macOS:   SENSEI_MAC_SIGN_IDENTITY signs the app; with APPLE_ID, APPLE_APP_SPECIFIC_PASSWORD and
//            APPLE_TEAM_ID it is notarized too.
import { packager } from "@electron/packager";
import { readFile } from "node:fs/promises";
import path from "node:path";

const pkg = JSON.parse(await readFile(new URL("../package.json", import.meta.url), "utf8"));
const env = process.env;

const windowsSign = env.SENSEI_WIN_CERT_FILE
  ? { certificateFile: env.SENSEI_WIN_CERT_FILE, certificatePassword: env.SENSEI_WIN_CERT_PASSWORD }
  : undefined;
const osxSign = env.SENSEI_MAC_SIGN_IDENTITY ? { identity: env.SENSEI_MAC_SIGN_IDENTITY } : undefined;
const notarize = osxSign && env.APPLE_ID && env.APPLE_APP_SPECIFIC_PASSWORD && env.APPLE_TEAM_ID;
const osxNotarize = notarize
  ? { appleId: env.APPLE_ID ?? "", appleIdPassword: env.APPLE_APP_SPECIFIC_PASSWORD ?? "", teamId: env.APPLE_TEAM_ID ?? "" }
  : undefined;

const [appPath] = await packager({
  dir: ".",
  name: "SENSEI",
  appVersion: pkg.version,
  out: "release",
  overwrite: true,
  asar: true,
  // only electron/, dist/ and package.json ship: the UI is bundled, and the main process has no dependencies
  ignore: [/^\/(src|scripts|release|node_modules)($|\/)/, /^\/(index\.html|tsconfig[^/]*\.json)$/],
  windowsSign,
  osxSign,
  osxNotarize,
});
console.log(`packaged ${appPath}${windowsSign || osxSign ? " (signed)" : ""}${osxNotarize ? " (notarized)" : ""}`);

if (process.platform === "win32") {
  // The package's name (@sensei/...) isn't a valid NuGet id, so the installer is named explicitly.
  const { createWindowsInstaller } = await import("electron-winstaller");
  const outputDirectory = path.join("release", "installer");
  await createWindowsInstaller({
    appDirectory: appPath,
    outputDirectory,
    name: "sensei",
    title: "SENSEI",
    exe: "SENSEI.exe",
    setupExe: `SENSEI-Setup-${pkg.version}.exe`,
    version: pkg.version,
    authors: "SENSEI contributors",
    description: pkg.description,
    noMsi: true,
    windowsSign,
  });
  console.log(`installer ${path.join(outputDirectory, `SENSEI-Setup-${pkg.version}.exe`)}`);
}
