import React, { useEffect, useRef } from "react";

/**
 * Google AdSense ad unit.
 *
 * Nothing renders here yet — this is scaffolding so that once AdSense
 * approval lands and we know which slot IDs to serve, dropping ads into
 * any page is a single JSX line:
 *
 *   <AdSlot slot="1234567890" />                       // auto-fit responsive
 *   <AdSlot slot="…" format="rectangle" />             // medium rectangle
 *   <AdSlot slot="…" format="horizontal" />            // leaderboard
 *
 * The client id (ca-pub-5934189300970505) is already loaded globally by
 * the <script> tag in public/index.html, so we only need to emit the
 * <ins> element and push the ad into the adsbygoogle queue on mount.
 *
 * Why a component instead of inlining the <ins> tag each time:
 *   - keeps the publisher id in one place (search AdSlot.js to update)
 *   - handles the push() call so you can't forget it
 *   - no-op during local dev / preview if the adsense script is blocked
 *   - re-pushes on route change / prop change so SPA navigation doesn't
 *     leave stale ad containers
 */

const PUBLISHER_ID = "ca-pub-5934189300970505";

export default function AdSlot({
  slot,
  format = "auto",
  responsive = true,
  className = "",
  style = {},
  testMode = false,
}) {
  const insRef = useRef(null);
  const pushedRef = useRef(false);

  useEffect(() => {
    // Only push once per mount. React 18 StrictMode double-invokes effects
    // in dev; pushing twice causes AdSense to log "All 'ins' elements in
    // the DOM with class=adsbygoogle already have ads in them" warnings.
    if (pushedRef.current) return;
    if (typeof window === "undefined") return;
    if (!slot) return; // nothing to render without a slot id

    try {
      // adsbygoogle is a queue the AdSense script drains. Pushing an empty
      // object tells it to scan the DOM for new <ins> tags and fill them.
      (window.adsbygoogle = window.adsbygoogle || []).push({});
      pushedRef.current = true;
    } catch (err) {
      // Script blocked by an extension / failed to load — fail silently,
      // never break the page because ads are unavailable.
      // eslint-disable-next-line no-console
      console.debug("AdSense push failed:", err);
    }
  }, [slot]);

  // Guard: without a slot, render nothing — avoids an empty <ins> that
  // would pollute the DOM and AdSense dashboards with "no slot" errors.
  if (!slot) return null;

  const insProps = {
    className: "adsbygoogle",
    style: { display: "block", ...style },
    "data-ad-client": PUBLISHER_ID,
    "data-ad-slot": slot,
    "data-ad-format": format,
  };
  if (responsive) {
    insProps["data-full-width-responsive"] = "true";
  }
  if (testMode) {
    // Google's documented flag to serve test ads during development
    // without risking policy violations.
    insProps["data-adtest"] = "on";
  }

  return (
    <div
      className={`ad-slot my-4 flex justify-center ${className}`}
      aria-label="Advertisement"
      data-testid={`ad-slot-${slot}`}
    >
      <ins ref={insRef} {...insProps} />
    </div>
  );
}
