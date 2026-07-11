import { forwardRef, type ImgHTMLAttributes } from "react";

import mascotUrl from "@/assets/omnicraft-mascot.png";

// The OmniCraft mascot — a turquoise fish with a pink starfish buddy — rendered
// as an <img>. (It replaced an inline animated-SVG starfish whose eyes followed
// the cursor; the brand logo is raster art, so that per-eye animation is gone.)
// `className` still flows through; ``otto-working`` (see index.css) gives the
// status-line copy a gentle bob. Decorative by default (empty alt); callers that
// use it as a meaningful image pass their own ``aria-label``.
export const OttoIcon = forwardRef<HTMLImageElement, ImgHTMLAttributes<HTMLImageElement>>(
  function OttoIcon({ alt = "", ...props }, ref) {
    return <img ref={ref} src={mascotUrl} alt={alt} draggable={false} {...props} />;
  },
);
