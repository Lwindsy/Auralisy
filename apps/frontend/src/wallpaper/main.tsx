import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/archivo/wdth.css";
import "@fontsource/ibm-plex-mono/400.css";
import "@fontsource/ibm-plex-mono/500.css";
import "../styles/tokens.css";
import "../styles/base.css";
import "../styles/frame.css";
import "../styles/entry.css";
import "../styles/waiting.css";
import "../styles/wallpaper.css";
import { Api } from "../api/client.ts";
import { setFrameCap } from "../components/motion/Contours.tsx";
import { i18n, isLocale } from "../i18n/index.ts";
import { DiscoveryController } from "../state/controller.ts";
import { LongReviewController } from "../state/review.ts";
import { WallpaperApp } from "./WallpaperApp.tsx";

/** Frame rate when the launcher does not pass Wallpaper Engine's own limit. */
const DEFAULT_FPS = 30;

// The Wallpaper Engine launcher passes its settings in the address: they cannot reach
// this page any other way once it has navigated here.
const params = new URLSearchParams(location.search);
const fps = Number(params.get("fps"));
setFrameCap(fps > 0 ? Math.min(fps, 60) : DEFAULT_FPS);
const language = params.get("lang");
if (isLocale(language)) i18n.set(language);

const api = new Api();
const controller = new DiscoveryController(api);
const review = new LongReviewController(
  api,
  () => controller.state,
  () => controller.refresh(),
);
controller.subscribe(() =>
  i18n.adopt(controller.state?.editable_input?.language),
);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden && controller.state) void controller.refresh();
});
window.addEventListener("pagehide", () => {
  controller.dispose();
  review.dispose();
});
void controller.connect();

const root = document.getElementById("root");
if (!root) throw new Error("Missing #root");
createRoot(root).render(
  <StrictMode>
    <WallpaperApp api={api} controller={controller} review={review} />
  </StrictMode>,
);
