import { NextRequest, NextResponse } from "next/server";
import { SESSION_COOKIE_NAME, hasSessionCookie } from "@/lib/auth";

export function middleware(request: NextRequest) {
  const cookie = request.cookies.get(SESSION_COOKIE_NAME)?.value;
  const authed = hasSessionCookie(cookie);

  if (request.nextUrl.pathname === "/") {
    if (authed) {
      return NextResponse.redirect(new URL("/dashboard", request.url));
    }
    return NextResponse.next();
  }

  if (!authed) {
    return NextResponse.redirect(new URL("/login", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/", "/dashboard/:path*", "/profile/:path*", "/upload/:path*", "/wallet/:path*"],
};
