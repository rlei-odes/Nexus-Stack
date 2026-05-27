// Authorization guard for admin-only endpoints.
//
// Cloudflare Access authenticates the caller but doesn't authorize per-role:
// every email on the Access whitelist (admin + users + guests) reaches every
// endpoint with the same rights. This helper restricts an endpoint to the
// single ADMIN_EMAIL identity by comparing it against the Access-authenticated
// caller (extracted from the JWT). Emails are compared case-insensitively per
// RFC 5321.
//
// Usage at the top of any admin-only handler:
//   const denial = requireAdmin(context.env, context.request);
//   if (denial) return denial;
import { getAccessUserEmail } from './cf-access-email.js';

export function requireAdmin(env, request) {
  const adminEmail = (env.ADMIN_EMAIL || '').trim().toLowerCase();
  const caller = (getAccessUserEmail(request) || '').trim().toLowerCase();

  if (!adminEmail || !caller || caller !== adminEmail) {
    return new Response(JSON.stringify({
      success: false,
      error: 'Forbidden: this endpoint requires admin access'
    }), {
      status: 403,
      headers: { 'Content-Type': 'application/json' }
    });
  }
  return null;
}
