// Extract the authenticated user's email from a Cloudflare Access request.
//
// Cloudflare Access always injects `Cf-Access-Jwt-Assertion`, but only some
// app configurations also emit `Cf-Access-Authenticated-User-Email` as a
// plain header — newer apps tend not to. Decoding the JWT's email claim is
// the only universally reliable path. Origin-supplied `Cf-Access-*` headers
// are stripped and re-set at Cloudflare's edge, so values reaching the
// Worker were written by the edge — but only as long as the request
// actually passed through Access. That holds for Pages Functions on this
// project today; a misconfig or alternate route would break the assumption.
// Proper defense-in-depth would verify the JWT signature against the
// Access JWKS endpoint.
export function getAccessUserEmail(request) {
  const direct = (request.headers.get('Cf-Access-Authenticated-User-Email') || '').trim();
  if (direct) return direct;

  const jwt = request.headers.get('Cf-Access-Jwt-Assertion');
  if (!jwt) return null;
  const parts = jwt.split('.');
  if (parts.length !== 3) return null;

  try {
    const payload = JSON.parse(atob(parts[1].replace(/-/g, '+').replace(/_/g, '/')));
    return typeof payload.email === 'string' ? payload.email.trim() : null;
  } catch {
    return null;
  }
}
