/**
 * Shared Infisical fetch helper.
 *
 * Fetches every key/value pair in the project with a single recursive request
 * and returns them grouped by folder and sorted. Consumed by:
 *   - GET /api/secrets          (renders the Secrets page)
 *   - POST /api/databricks-sync (pushes the same inventory into Databricks)
 *
 * Keeping both endpoints on one code path guarantees the UI and the Databricks
 * scope stay in lockstep — whatever you see on /secrets is exactly what a
 * sync will upsert.
 */
import { fetchWithTimeout } from './fetch-with-timeout.js';
import { safeHttpsUrl } from './url.js';

async function safeJsonParse(response, label) {
  const contentType = response.headers.get('content-type') || '';
  if (!contentType.includes('application/json')) {
    const bodyPreview = (await response.text()).substring(0, 200);
    throw new Error(
      `${label} returned non-JSON response (${response.status}, content-type: ${contentType}). ` +
      `This usually means Cloudflare Access is blocking the request. Preview: ${bodyPreview}`
    );
  }
  return response.json();
}

/**
 * Fetch every secret from the Infisical project.
 *
 * @param {object} env - Pages Function env bindings
 * @returns {Promise<{
 *   configured: boolean,
 *   groups: Array<{ name: string, secrets: Array<{ key: string, value: string }> }>,
 *   warnings: string[],
 *   message?: string,
 * }>}
 *
 * When Infisical env vars are missing, returns `{ configured: false, groups: [], warnings: [], message }`
 * rather than throwing, matching the prior /api/secrets behaviour.
 *
 * `warnings` is always empty: the single request either returns the whole
 * inventory or throws. The field stays because both callers pass it through.
 * Thrown errors (Infisical unreachable, non-2xx, non-JSON) are for the caller
 * to catch and surface.
 */
export async function fetchAllInfisicalSecrets(env) {
  const token = env.INFISICAL_TOKEN;
  const projectId = env.INFISICAL_PROJECT_ID;
  const domain = env.DOMAIN;

  if (!token || !projectId || !domain) {
    return {
      configured: false,
      groups: [],
      warnings: [],
      message: 'Infisical not configured. Ensure INFISICAL_TOKEN, INFISICAL_PROJECT_ID, and DOMAIN are set.',
    };
  }

  const baseUrl = safeHttpsUrl(env.INFISICAL_URL, `https://infisical.${domain}`);
  if (!baseUrl) {
    // safeHttpsUrl returns '' if both candidate and fallback fail to parse as
    // https URLs. Without this guard, fetches below would resolve as relative
    // paths against the Control Plane origin and leak the Infisical bearer
    // token to the wrong host.
    return {
      configured: false,
      groups: [],
      warnings: [],
      message: `Infisical misconfigured: INFISICAL_URL is invalid and fallback https://infisical.${domain} did not parse as https.`,
    };
  }
  const environment = env.INFISICAL_ENV || 'dev';
  const headers = {
    'Authorization': `Bearer ${token}`,
    'Content-Type': 'application/json',
  };

  // Cloudflare Access service-token headers for machine-to-machine auth
  const cfAccessClientId = env.CF_ACCESS_CLIENT_ID;
  const cfAccessClientSecret = env.CF_ACCESS_CLIENT_SECRET;
  if (cfAccessClientId && cfAccessClientSecret) {
    headers['CF-Access-Client-Id'] = cfAccessClientId;
    headers['CF-Access-Client-Secret'] = cfAccessClientSecret;
  }

  // One recursive request for the whole project. A Pages Function may make
  // at most 50 subrequests per invocation on the Workers free plan, and a
  // stack carries one Infisical folder per service — over a hundred — so a
  // request per folder cannot work. With `recursive=true` Infisical returns
  // every secret under `/` and tags each with the `secretPath` of the folder
  // it lives in, which is what the grouping below keys on.
  //
  // It also makes the result all-or-nothing: either the whole inventory
  // arrives or this throws. A partial inventory is worse than none for
  // POST /api/databricks-sync, which deletes scope keys it does not see here.
  const params = new URLSearchParams({
    workspaceId: projectId,
    environment,
    secretPath: '/',
    recursive: 'true',
  });
  const res = await fetchWithTimeout(`${baseUrl}/api/v3/secrets/raw?${params}`, { headers });

  if (!res.ok) {
    const errText = await res.text();
    throw new Error(`Failed to fetch secrets from Infisical (${res.status}): ${errText.substring(0, 200)}`);
  }

  const data = await safeJsonParse(res, 'Secrets API');
  return { configured: true, groups: groupSecretsByFolder(data.secrets || []), warnings: [] };
}

/**
 * Group a recursive raw-secrets listing by folder.
 *
 * `/portainer` becomes the group `portainer`, a nested `/a/b` becomes `a/b`,
 * and secrets at the project root (or with no `secretPath`) go to `root`.
 * Empty values are dropped, and so is any folder left with nothing in it.
 * Groups and the keys within them are sorted.
 *
 * @param {Array<{ secretKey: string, secretValue?: string, secretPath?: string }>} secrets
 * @returns {Array<{ name: string, secrets: Array<{ key: string, value: string }> }>}
 */
export function groupSecretsByFolder(secrets) {
  const byName = new Map();
  for (const s of secrets) {
    if (s.secretValue === undefined || s.secretValue === '') continue;
    const name = (s.secretPath || '/').replace(/^\/+|\/+$/g, '') || 'root';
    if (!byName.has(name)) byName.set(name, []);
    byName.get(name).push({ key: s.secretKey, value: s.secretValue });
  }
  return [...byName.entries()]
    .map(([name, list]) => ({ name, secrets: list.sort((a, b) => a.key.localeCompare(b.key)) }))
    .sort((a, b) => a.name.localeCompare(b.name));
}
