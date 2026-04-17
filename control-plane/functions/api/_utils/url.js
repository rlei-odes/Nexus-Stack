/**
 * URL validation helpers for env-var-sourced URLs.
 *
 * Config values like INFISICAL_URL / CONTROL_PLANE_URL are Pages secrets,
 * not user-controlled, but we still validate them as defense-in-depth:
 * a typo could otherwise leak bearer tokens to the wrong host or break
 * HTML email rendering via attribute-breaking characters.
 */

/**
 * Returns the origin of `candidate` if it parses as an https: URL.
 * Otherwise returns `fallback`. The returned value is always either
 * a trusted fallback or a scheme+host string with no path/query.
 */
export function safeHttpsUrl(candidate, fallback) {
  if (!candidate) return fallback;
  try {
    const u = new URL(candidate);
    if (u.protocol !== 'https:') return fallback;
    return u.origin;
  } catch {
    return fallback;
  }
}
