"use client";

import Script from "next/script";

// Loads Razorpay's hosted Checkout script once. window.Razorpay is only
// usable after this script finishes loading — callers should guard the
// "Add Money" action on its presence rather than assuming it's ready
// immediately on mount.
export function RazorpayCheckoutScript() {
  return <Script src="https://checkout.razorpay.com/v1/checkout.js" strategy="lazyOnload" />;
}
