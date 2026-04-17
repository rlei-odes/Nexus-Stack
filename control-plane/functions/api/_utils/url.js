/**
 * URL validation helpers for env-var-sourced URLs.
 *
 * Config values like INFISICAL_URL / CONTROL_PLANE_URL are Pages secrets,
 * not user-controlled, but we still validate them as defense-in-depth:
 * a typo could otherwise leak bearer tokens to the wrong host or break
 * HTML email rendering via attribute-breaking characters.
 */

function validateHttpsOrigin(url) {
  if (!url) return null;
  try {
    const u = new URL(url);
    if (u.protocol !== 'https:') return null;
    return u.origin;
  } catch {
    return null;
  }
}

/**
 * Returns the origin of `candidate` if it parses as an https: URL.
 * Falls back to `fallback` (which is also validated the same way).
 * Returns '' if neither validates — safer than leaking a malformed URL
 * into a bearer-token fetch or an HTML href.
 */
export function safeHttpsUrl(candidate, fallback) {
  return validateHttpsOrigin(candidate) || validateHttpsOrigin(fallback) || '';
}
