import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE_NAME, hasSessionCookie } from "@/lib/auth";

export function middleware(request: NextRequest) {
  const cookie = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  if (!hasSessionCookie(cookie)) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/dashboard/:path*", "/profile/:path*", "/upload/:path*"],
};
